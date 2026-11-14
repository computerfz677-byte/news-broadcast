#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻联播统一告警推送模块
========================
复用腾讯云服务器上已有的推送设施，按优先级降级：

1. 本地 Bark 统一网关   POST http://127.0.0.1:8091/push
   网关内部会推全部已注册设备 + 按分类发 ntfy（channels 可显式指定双通道）
2. 本地 ntfy 服务       POST http://127.0.0.1:8092/{topic}
   （自建 ntfy，upstream 为 ntffy.sh，无需 token）
3. 直连公网 Bark        POST https://api.day.app/{key}
   兜底：网关不可用时，用 devices.json 里的 key 直推
4. 直连公网 ntfy        POST https://ntfy.sh/{topic}
   最后兜底

设计要点：
- 任何通道失败都**不抛异常**，只写日志（告警通道不能拖垮采集流程）
- 同一事件在去重窗口内只推一次，避免 systemd 反复重试刷屏
- 网关 8091 与 ntfy 8092 均由 systemd 常驻，本模块只做客户端

环境变量（可选，均有默认值）：
  NEWS_GATEWAY_URL   默认 http://127.0.0.1:8091
  NEWS_NTFY_SERVER   默认 http://127.0.0.1:8092
  NEWS_NTFY_TOPIC    默认 cfz-alert
  NEWS_BARK_SERVER   默认 https://api.day.app
  NEWS_BARK_KEY      直连兜底用的 Bark key（不填则读网关 devices.json）
  NEWS_GATEWAY_GROUP 默认 部署验收
  NEWS_ALERT_DEDUP_SEC 默认 1800

多平台通道（IM）：
  NEWS_FEISHU_APP_ID / NEWS_FEISHU_APP_SECRET / NEWS_FEISHU_CHAT_ID
      飞书开放平台自建应用直发（不依赖 gateway 进程）
  NEWS_WECOM_WEBHOOK  企业微信群机器人 webhook（qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx）
  NEWS_QQ_WEBHOOK     QQ 群机器人 webhook（可选，走 hermes send 或自定义）
  NEWS_HERMES_SEND    是否允许调用 `hermes send` 推送（默认 1）
"""

import os
import sys
import json
import time
import glob
import shutil
import hashlib
import datetime
import urllib.request

LOG_DIR = os.environ.get('NEWS_LOG_DIR', '/opt/news-broadcast/logs')
GATEWAY_URL = os.environ.get('NEWS_GATEWAY_URL', 'http://127.0.0.1:8091').rstrip('/')
NTFY_SERVER = os.environ.get('NEWS_NTFY_SERVER', 'http://127.0.0.1:8092').rstrip('/')
NTFY_TOPIC = os.environ.get('NEWS_NTFY_TOPIC', 'cfz-alert')
BARK_SERVER = os.environ.get('NEWS_BARK_SERVER', 'https://api.day.app').rstrip('/')
BARK_KEY = os.environ.get('NEWS_BARK_KEY', '').strip()
GATEWAY_GROUP = os.environ.get('NEWS_GATEWAY_GROUP', '部署验收')
DEVICES_FILE = os.environ.get('NEWS_BARK_DEVICES', '/opt/bark-gateway/devices.json')
DEDUP_SEC = int(os.environ.get('NEWS_ALERT_DEDUP_SEC', '1800'))
DEDUP_FILE = os.environ.get('NEWS_ALERT_DEDUP_FILE',
                            os.path.join(LOG_DIR, 'alert_dedup.json'))

# ---- IM 通道配置 ----
FEISHU_APP_ID = os.environ.get('NEWS_FEISHU_APP_ID', '').strip()
FEISHU_APP_SECRET = os.environ.get('NEWS_FEISHU_APP_SECRET', '').strip()
FEISHU_CHAT_ID = os.environ.get('NEWS_FEISHU_CHAT_ID', '').strip()
WECOM_WEBHOOK = os.environ.get('NEWS_WECOM_WEBHOOK', '').strip()
HERMES_SEND = os.environ.get('NEWS_HERMES_SEND', '1').strip() not in ('0', 'false', 'no')
HERMES_BIN = os.environ.get('NEWS_HERMES_BIN', '/usr/local/bin/hermes')
IM_QUEUE_DIR = os.environ.get('NEWS_IM_QUEUE_DIR', '/opt/news-broadcast/logs/im_queue')
# hermes 平台目标：platform:chat_id
HERMES_TARGETS = [
    t.strip() for t in os.environ.get(
        'NEWS_HERMES_TARGETS',
        'feishu:oc_d45c9e16f9c9f99eadd65c00539753a9,'
        'lightclawbot:616601857,'
        'weixin:o9cq803lrfB_S8JoETZTOgYC9l8E@im.wechat').split(',')
    if t.strip()]

_STATE = {}


def _load_dedup():
    """读取跨进程持久化去重状态"""
    try:
        with open(DEDUP_FILE, encoding='utf-8') as f:
            data = json.load(f)
        now = time.time()
        return {k: v for k, v in data.items() if now - v < DEDUP_SEC}
    except Exception:
        return {}


def _save_dedup(state):
    try:
        d = os.path.dirname(DEDUP_FILE)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = DEDUP_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f)
        os.replace(tmp, DEDUP_FILE)
    except Exception as e:
        log('去重状态保存失败: %s' % str(e)[:120])


def log(msg):
    line = '[%s] [alert] %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line)
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, 'alert.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _post(url, data=None, headers=None, timeout=12):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method='POST' if data is not None else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode('utf-8', 'ignore')


def _dedup(key):
    """True 表示去重窗口内已推送过（跨进程持久化，避免定时器反复重试刷屏）"""
    now = time.time()
    state = _load_dedup()
    _STATE.update(state)
    last = state.get(key)
    if last and now - last < DEDUP_SEC:
        return True
    state[key] = now
    _STATE.update(state)
    _save_dedup(state)
    return False


# --------------------------------------------------------------- 通道实现
def send_gateway(title, body, group=None, level='active', channels=None):
    """通道1：本地 Bark 统一网关（一次调用即完成 bark + ntfy 双通道）"""
    payload = {
        'title': title,
        'body': body,
        'level': level,
        'group': group or GATEWAY_GROUP,
    }
    if channels:
        payload['channels'] = list(channels)
    try:
        code, text = _post('%s/push' % GATEWAY_URL,
                           data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                           headers={'Content-Type': 'application/json; charset=utf-8'})
        ok = code == 200
        log('网关 %s code=%s resp=%s' % ('OK' if ok else 'FAIL', code, text[:160]))
        return ok
    except Exception as e:
        log('网关发送异常: %s: %s' % (type(e).__name__, str(e)[:200]))
        return False


def _hdr(value):
    """HTTP 头只能放 latin-1；非 ASCII 走 UTF-8 百分号编码（ntfy / Bark 均支持）"""
    try:
        value.encode('latin-1')
        return value
    except UnicodeEncodeError:
        from urllib.parse import quote
        return quote(value, safe='')


def _tags_list(tags):
    """ntfy JSON 发布要求 tags 为数组"""
    if not tags:
        return ['rotating_light']
    if isinstance(tags, (list, tuple)):
        return [str(t).strip() for t in tags if str(t).strip()]
    return [t.strip() for t in str(tags).split(',') if t.strip()]


def send_ntfy(title, body, priority=5, tags=None):
    """通道2：本地 ntfy 服务（JSON 发布，中文标题/正文不需转义）"""
    payload = {
        'topic': NTFY_TOPIC,
        'title': title,
        'message': body,
        'priority': int(priority),
        'tags': _tags_list(tags),
    }
    try:
        code, text = _post(NTFY_SERVER + '/',
                           data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                           headers={'Content-Type': 'application/json; charset=utf-8'})
        ok = code in (200, 201)
        log('ntfy %s code=%s resp=%s' % ('OK' if ok else 'FAIL', code, text[:160]))
        return ok
    except Exception as e:
        log('ntfy 发送异常: %s: %s' % (type(e).__name__, str(e)[:200]))
        return False


def _device_keys():
    """从网关 devices.json 取可用 Bark key（仅直连兜底用）"""
    keys = []
    if BARK_KEY:
        keys.append((BARK_KEY, BARK_SERVER))
    try:
        with open(DEVICES_FILE, encoding='utf-8') as f:
            for d in json.load(f):
                if d.get('enabled', True) and d.get('key'):
                    keys.append((d['key'], d.get('sh_url') or d.get('sv_url') or BARK_SERVER))
    except Exception as e:
        log('读取 devices.json 失败: %s' % str(e)[:120])
    return keys


def send_bark_direct(title, body, level='timeSensitive', group=None):
    """通道3：直连 Bark（每个设备独立尝试，任意一个成功即算成功）"""
    keys = _device_keys()
    if not keys:
        log('无可用 Bark key，跳过直连')
        return False
    payload = {
        'title': title,
        'body': body,
        'level': level,
        'group': group or GATEWAY_GROUP,
        'isArchive': 1,
    }
    ok_any = False
    for key, base in keys:
        url = '%s/%s' % (base.rstrip('/'), key)
        try:
            code, text = _post(url,
                               data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                               headers={'Content-Type': 'application/json; charset=utf-8'})
            if code == 200:
                log('Bark直连 OK %s...' % key[:6])
                ok_any = True
        except Exception as e:
            log('Bark直连失败 %s...: %s' % (key[:6], str(e)[:120]))
    return ok_any


def send_ntfy_public(title, body, priority=5, tags=None):
    """通道4：直连公网 ntfy.sh 兜底"""
    payload = {
        'topic': NTFY_TOPIC,
        'title': title,
        'message': body,
        'priority': int(priority),
        'tags': _tags_list(tags),
    }
    try:
        code, text = _post('https://ntfy.sh/',
                           data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                           headers={'Content-Type': 'application/json; charset=utf-8'},                           timeout=20)
        ok = code in (200, 201)
        log('ntfy公网 %s code=%s' % ('OK' if ok else 'FAIL', code))
        return ok
    except Exception as e:
        log('ntfy公网 发送异常: %s: %s' % (type(e).__name__, str(e)[:160]))
        return False


# --------------------------------------------------------------- IM 通道
def send_feishu(title, body, chat_id=None):
    """IM 通道：飞书开放平台自建应用直发（不依赖 hermes gateway 进程）"""
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET and (chat_id or FEISHU_CHAT_ID)):
        log('飞书凭据未配置，跳过')
        return False
    try:
        code, text = _post(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            data=json.dumps({'app_id': FEISHU_APP_ID,
                             'app_secret': FEISHU_APP_SECRET}).encode('utf-8'),
            headers={'Content-Type': 'application/json; charset=utf-8'})
        tok = json.loads(text).get('tenant_access_token')
        if not tok:
            log('飞书取 token 失败: %s' % text[:160])
            return False
        content = json.dumps({'text': '%s\n%s' % (title, body)}, ensure_ascii=False)
        code, text = _post(
            'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
            data=json.dumps({'receive_id': chat_id or FEISHU_CHAT_ID,
                             'msg_type': 'text',
                             'content': content}).encode('utf-8'),
            headers={'Content-Type': 'application/json; charset=utf-8',
                     'Authorization': 'Bearer %s' % tok})
        ok = json.loads(text).get('code') == 0
        log('飞书 %s code=%s resp=%s' % ('OK' if ok else 'FAIL', code, text[:160]))
        return ok
    except Exception as e:
        log('飞书发送异常: %s: %s' % (type(e).__name__, str(e)[:200]))
        return False


def send_wecom_webhook(title, body):
    """IM 通道：企业微信群机器人 webhook"""
    if not WECOM_WEBHOOK:
        log('企业微信 webhook 未配置，跳过')
        return False
    payload = {'msgtype': 'text', 'text': {'content': '%s\n%s' % (title, body)}}
    try:
        code, text = _post(WECOM_WEBHOOK,
                           data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                           headers={'Content-Type': 'application/json; charset=utf-8'})
        ok = code == 200 and json.loads(text).get('errcode') == 0
        log('企业微信 %s code=%s resp=%s' % ('OK' if ok else 'FAIL', code, text[:160]))
        return ok
    except Exception as e:
        log('企业微信发送异常: %s: %s' % (type(e).__name__, str(e)[:200]))
        return False


def send_hermes(target, title, body):
    """IM 通道：调用 hermes send（走 gateway 已配置的 QQ / 企微 / 飞书通道）"""
    if not HERMES_SEND or not shutil.which(HERMES_BIN):
        log('hermes send 不可用，跳过 %s' % target)
        return False
    import subprocess
    msg = '%s\n%s' % (title, body)
    try:
        p = subprocess.run([HERMES_BIN, 'send', '--to', target, msg],
                           capture_output=True, text=True, timeout=90)
        out = (p.stdout or '').strip() + (p.stderr or '').strip()
        ok = p.returncode == 0 and 'sent' in (p.stdout or '').lower()
        log('hermes→%s %s: %s' % (target.split(':')[0], 'OK' if ok else 'FAIL', out[:160]))
        return ok
    except Exception as e:
        log('hermes→%s 异常: %s: %s' % (target.split(':')[0], type(e).__name__, str(e)[:160]))
        return False


def queue_im(target, title, body):
    """
    把消息写入 IM 队列，由 hermes cron（gateway 进程内）投递。

    原因：QQ(lightclawbot) / 企业微信(weixin) 的 adapter 只存在于 gateway 进程内，
    外部进程调用 hermes send 会因找不到 live adapter 而失败；
    而 hermes cron 的 --deliver 在 gateway 进程内执行，可以直接用 live adapter。
    """
    if not IM_QUEUE_DIR:
        log('IM 队列未启用，跳过 %s' % target)
        return False
    try:
        if not os.path.isdir(IM_QUEUE_DIR):
            os.makedirs(IM_QUEUE_DIR, exist_ok=True)
        fname = os.path.join(IM_QUEUE_DIR, target.replace(':', '_') + '.jsonl')
        payload = {'ts': time.time(),
                   'title': title,
                   'body': body,
                   'text': '%s\n%s' % (title, body)}
        with open(fname, 'a', encoding='utf-8') as f:
            f.write(json.dumps(payload, ensure_ascii=False) + '\n')
        log('IM 队列写入 %s: %s' % (os.path.basename(fname), title[:40]))
        return True
    except Exception as e:
        log('IM 队列写入失败: %s: %s' % (type(e).__name__, str(e)[:160]))
        return False


def send_hermes_all(title, body):
    """
    对所有 hermes target 推送。
    优先直连 hermes send；失败（QQ/企微无 live adapter）则入队由 cron 投递。
    """
    ok = 0
    queued = 0
    for t in HERMES_TARGETS:
        if send_hermes(t, title, body):
            ok += 1
        elif queue_im(t, title, body):
            queued += 1
    if queued:
        log('IM 队列待投递: %d 条（由 hermes cron 消费）' % queued)
    return ok + queued


# --------------------------------------------------------------- 统一入口
def notify(title, body, level='active', priority=5, tags=None,
           dedup_key=None, group=None, dual=True, im=True):
    """
    统一推送入口。
    主链（bark + ntfy）：网关 → 本地ntfy → 直连bark → 公网ntfy
    IM 链（飞书 / 企微 / QQ）：飞书直连 → hermes send
    dual=True 时要求 bark 与 ntfy 都至少送达一次。
    返回成功推送的通道数。
    """
    key = dedup_key or hashlib.md5(('%s|%s' % (title, body)).encode('utf-8')).hexdigest()
    if _dedup(key):
        log('去重窗口内，跳过重复告警: %s' % title)
        return 0

    bark_ok = False
    ntfy_ok = False

    # 通道1：网关（显式要求双通道，兼容网关 channels 参数）
    if send_gateway(title, body, group=group, level=level,
                    channels=['bark', 'ntfy'] if dual else None):
        bark_ok = True
        ntfy_ok = True

    if not ntfy_ok:
        ntfy_ok = send_ntfy(title, body, priority=priority, tags=tags)

    if not bark_ok:
        bark_ok = send_bark_direct(title, body, level=level, group=group)

    if not ntfy_ok:
        ntfy_ok = send_ntfy_public(title, body, priority=priority, tags=tags)

    sent = int(bark_ok) + int(ntfy_ok)

    # ---- IM 通道 ----
    if im:
        im_ok = 0
        if send_feishu(title, body):
            im_ok += 1
        if WECOM_WEBHOOK and send_wecom_webhook(title, body):
            im_ok += 1
        if send_hermes_all(title, body):
            im_ok += 1
        sent += im_ok
        log('IM 通道成功数: %d' % im_ok)

    if sent == 0:
        log('所有通道均未送达: %s' % title)
    else:
        log('送达汇总 bark=%s ntfy=%s total=%d' % (bark_ok, ntfy_ok, sent))
    return sent


def notify_recovery(title, body, **kw):
    """
    正常状态通知。
    注意：网关对含「恢复 / recovered / is up」的标题会静默丢弃，
    因此恢复类通知统一走本地 ntfy + 直连 Bark，不经过网关。
    """
    kw.setdefault('dedup_key', 'recovery-%s' % int(time.time() // 300))
    ok_ntfy = send_ntfy(title, body, priority=4, tags='white_check_mark')
    ok_bark = send_bark_direct(title, body, level='active', group=GATEWAY_GROUP)
    im_ok = 0
    if send_feishu(title, body):
        im_ok += 1
    if WECOM_WEBHOOK and send_wecom_webhook(title, body):
        im_ok += 1
    if send_hermes_all(title, body):
        im_ok += 1
    log('恢复通知送达 ntfy=%s bark=%s im=%d' % (ok_ntfy, ok_bark, im_ok))
    return int(ok_ntfy) + int(ok_bark) + im_ok


if __name__ == '__main__':
    t = sys.argv[1] if len(sys.argv) > 1 else '测试标题'
    b = sys.argv[2] if len(sys.argv) > 2 else '这是一条测试告警'
    sys.exit(0 if notify(t, b, dedup_key='selftest-%d' % int(time.time() // 30)) else 1)
