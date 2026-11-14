#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻联播看板/采集看门狗
======================
定时巡检并在异常时通过 Bark / Ntfy 推送告警。

巡检项：
1. 今日简报是否存在（仅在 19:10 播出时间窗之后检查）
2. 最近 N 期简报是否连续（缺失即告警）
3. 看板服务 HTTP 是否存活（127.0.0.1:8765）
4. 行情缓存是否陈旧（超过 26 小时未刷新）

退出码：0 = 正常（或已推送告警），1 = 巡检脚本自身异常
"""

import os
import sys
import re
import json
import glob
import time
import socket
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402
import notify as N  # noqa: E402

DATA_DIR = os.environ.get('NEWS_DATA_DIR', '/opt/news-broadcast/data')
LOG_DIR = os.environ.get('NEWS_LOG_DIR', '/opt/news-broadcast/logs')
PORT = int(os.environ.get('NEWS_PORT', '8765'))
NOT_BEFORE_HOUR = 19
NOT_BEFORE_MIN = 10
CHECK_RECENT = 5          # 连续检查最近 5 期
CACHE_MAX_AGE_H = 26      # 行情缓存最大允许陈旧小时数


def log(msg):
    line = '[%s] [watchdog] %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line)
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, 'watchdog.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def existing_dates():
    out = set()
    for p in glob.glob(os.path.join(DATA_DIR, '*新闻联播元宝.md')):
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', os.path.basename(p))
        if m:
            try:
                out.add(datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                pass
    return out


def check_today(have):
    """今日简报是否已生成（未到播出时间不算问题）"""
    now = datetime.datetime.now()
    cutoff = now.replace(hour=NOT_BEFORE_HOUR, minute=NOT_BEFORE_MIN,
                         second=0, microsecond=0)
    if now < cutoff:
        log('未到 %02d:%02d，跳过今日检查' % (NOT_BEFORE_HOUR, NOT_BEFORE_MIN))
        return []
    today = now.date()
    # 周末/节假日央视不播，无简报属正常
    if today.weekday() >= 5:
        log('今日为周末，跳过今日检查')
        return []
    if today in have:
        return []
    return ['今日（%s）简报缺失' % today.isoformat()]


def check_continuity(have):
    """最近 N 期是否连续缺失"""
    problems = []
    if not have:
        return ['数据目录为空，无任何简报']
    latest = max(have)
    for i in range(1, CHECK_RECENT + 1):
        d = latest - datetime.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        if d not in have:
            problems.append('%s 简报缺失' % d.isoformat())
    return problems


def check_service():
    """看板 HTTP 是否存活"""
    try:
        s = socket.create_connection(('127.0.0.1', PORT), timeout=5)
        s.close()
        return []
    except Exception as e:
        return ['看板服务 127.0.0.1:%d 不可达: %s' % (PORT, type(e).__name__)]


def check_cache():
    """行情缓存新鲜度"""
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.price_cache')
    cands = glob.glob(os.path.join(cache_dir, '*.json'))
    if not cands:
        return ['行情缓存文件缺失 (%s)' % cache_dir]
    newest = max(os.path.getmtime(p) for p in cands)
    age_h = (time.time() - newest) / 3600.0
    if age_h > CACHE_MAX_AGE_H:
        return ['行情缓存陈旧 %.1f 小时（阈值 %d 小时）' % (age_h, CACHE_MAX_AGE_H)]
    return []


STATE_FILE = os.path.join(LOG_DIR, 'watchdog_state.json')


def _load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        log('状态保存失败: %s' % str(e)[:120])


def main():
    log('=' * 62)
    have = existing_dates()
    log('现有 %d 期简报' % len(have))
    problems = []
    problems += check_today(have)
    problems += check_continuity(have)
    problems += check_service()
    problems += check_cache()

    prev = _load_state()
    prev_problems = prev.get('problems') or []
    state = {'problems': problems,
             'ts': datetime.datetime.now().isoformat(),
             'last_ok': prev.get('last_ok')}

    if not problems:
        if prev_problems:
            log('此前存在异常，本轮已正常 → 发送恢复通知')
            N.notify_recovery(
                '新闻联播看板已恢复',
                '此前异常项已全部消除：\n%s' % '\n'.join('· ' + p for p in prev_problems))
        state['last_ok'] = datetime.datetime.now().isoformat()
        state['problems'] = []
        _save_state(state)
        log('巡检通过，一切正常')
        log('=' * 62)
        return 0

    body = '\n'.join('· ' + p for p in problems)
    log('发现 %d 项异常:\n%s' % (len(problems), body))
    title = '新闻联播看板告警 %d 项' % len(problems)
    N.notify(title, body, level='active', priority=5,
             tags='rotating_light,warning')
    _save_state(state)
    log('=' * 62)
    return 0


if __name__ == '__main__':
    sys.exit(main())
