#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notion 简报归档同步脚本

用途：把服务器上 /opt/news-broadcast/data/*.md 的《新闻联播》简报，
逐期创建为 Notion 页面（父页面下建子页面，标题=日期）。

两种鉴权方式（二选一）：
  1) Notion 内部集成 Token：
     NOTION_TOKEN=secret_xxx
     NOTION_PARENT_PAGE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
  2) WorkBuddy Notion 连接器（由 WorkBuddy 侧调用本脚本时注入环境变量）

增量同步：状态文件 logs/notion_sync_state.json 记录已同步日期，重跑只补新增。

用法：
  python3 notion_sync.py              # 全量（跳过已同步）
  python3 notion_sync.py --force      # 忽略状态，重跑全部
  python3 notion_sync.py --limit 10   # 只处理最新 10 期（调试）
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.error

DATA_DIR = os.environ.get('NEWS_DATA_DIR', '/opt/news-broadcast/data')
STATE_FILE = os.environ.get('NEWS_NOTION_STATE', '/opt/news-broadcast/logs/notion_sync_state.json')
NOTION_API = 'https://api.notion.com/v1'
NOTION_VERSION = '2022-06-28'

TOKEN = os.environ.get('NOTION_TOKEN', '').strip()
PARENT_ID = os.environ.get('NOTION_PARENT_PAGE_ID', '').strip()

# 单条 Notion block 文本上限 2000 字符
BLOCK_TEXT_LIMIT = 1900
REQUEST_INTERVAL = 0.35  # 秒，避免触发 Notion 429


def log(msg):
    print('[notion] %s' % msg, flush=True)


def http(method, path, payload=None):
    url = NOTION_API + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', 'Bearer %s' % TOKEN)
    req.add_header('Notion-Version', NOTION_VERSION)
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        return e.code, body[:500]
    except Exception as e:
        return 0, str(e)


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'synced': []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def list_briefings():
    """返回 [(date_str, path)]，按日期升序"""
    out = []
    if not os.path.isdir(DATA_DIR):
        return out
    for name in os.listdir(DATA_DIR):
        if not name.endswith('.md'):
            continue
        m = re.match(r'^(\d{4}-\d{2}-\d{2})', name)
        if not m:
            continue
        out.append((m.group(1), os.path.join(DATA_DIR, name)))
    out.sort(key=lambda x: x[0])
    return out


def md_to_blocks(text):
    """把 markdown 拆成 Notion blocks（heading / paragraph / bulleted / code）"""
    blocks = []
    lines = text.split('\n')
    buf = []
    buf_kind = None

    def flush():
        if not buf:
            return
        content = '\n'.join(buf).strip()
        if not content:
            del buf[:]
            return
        # 超长段落按 1900 字符切块
        for i in range(0, len(content), BLOCK_TEXT_LIMIT):
            chunk = content[i:i + BLOCK_TEXT_LIMIT]
            blocks.append({
                'object': 'block',
                'type': 'paragraph',
                'paragraph': {'rich_text': [{'type': 'text', 'text': {'content': chunk}}]},
            })
        del buf[:]

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            buf_kind = None
            continue
        if stripped.startswith('#'):
            flush()
            level = min(len(stripped) - len(stripped.lstrip('#')), 3)
            text_h = stripped.lstrip('#').strip()
            blocks.append({
                'object': 'block',
                'type': 'heading_%d' % level,
                'heading_%d' % level: {
                    'rich_text': [{'type': 'text', 'text': {'content': text_h}}]},
            })
            continue
        if stripped.startswith(('- ', '* ', '+ ')):
            flush()
            item = stripped[2:].strip()
            blocks.append({
                'object': 'block',
                'type': 'bulleted_list_item',
                'bulleted_list_item': {
                    'rich_text': [{'type': 'text', 'text': {'content': item}}]},
            })
            continue
        if buf_kind == 'code':
            buf.append(line)
            continue
        flush()
        buf_kind = 'para'
        buf.append(line)
    flush()
    return blocks


def create_page(date_str, blocks):
    payload = {
        'parent': {'type': 'page_id', 'page_id': PARENT_ID},
        'properties': {
            'title': {'title': [{'type': 'text', 'text': {'content': '%s 新闻联播' % date_str}}]},
        },
        'children': blocks[:100],
    }
    code, body = http('POST', '/pages', payload)
    if code != 200:
        log('创建页面失败 %s: %s %s' % (date_str, code, body))
        return False
    page_id = body.get('id')
    # 剩余 blocks 分批追加
    rest = blocks[100:]
    while rest:
        code, body = http('PATCH', '/blocks/%s/children' % page_id,
                          {'children': rest[:100]})
        if code != 200:
            log('追加块失败 %s: %s %s' % (date_str, code, body))
            break
        rest = rest[100:]
        time.sleep(REQUEST_INTERVAL)
    return True


def main():
    args = sys.argv[1:]
    force = '--force' in args
    limit = None
    if '--limit' in args:
        try:
            limit = int(args[args.index('--limit') + 1])
        except Exception:
            limit = None

    if not TOKEN:
        log('未配置 NOTION_TOKEN，无法同步')
        return 1
    if not PARENT_ID:
        log('未配置 NOTION_PARENT_PAGE_ID，无法确定父页面')
        return 1

    # 校验 token 与父页面可访问
    code, body = http('GET', '/pages/%s' % PARENT_ID)
    if code != 200:
        log('父页面不可访问: %s %s' % (code, body))
        log('请确认集成已通过「连接页面」授权该父页面')
        return 1
    log('父页面可访问: %s' % body.get('url'))

    briefs = list_briefings()
    if not briefs:
        log('未找到简报文件，目录=%s' % DATA_DIR)
        return 1

    state = load_state()
    synced = set(state.get('synced', []))
    todo = [(d, p) for d, p in briefs if force or d not in synced]
    if limit:
        todo = todo[-limit:]
    log('总期数=%d 待同步=%d' % (len(briefs), len(todo)))
    if not todo:
        log('全部已是最新，无需同步')
        return 0

    ok = 0
    fail = 0
    for i, (date_str, path) in enumerate(todo, 1):
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except Exception as e:
            log('读取失败 %s: %s' % (path, e))
            fail += 1
            continue
        blocks = md_to_blocks(text)
        if not blocks:
            blocks = [{
                'object': 'block',
                'type': 'paragraph',
                'paragraph': {'rich_text': [{'type': 'text', 'text': {'content': text[:BLOCK_TEXT_LIMIT]}}]},
            }]
        if create_page(date_str, blocks):
            synced.add(date_str)
            state['synced'] = sorted(synced)
            state['last_sync'] = time.strftime('%Y-%m-%d %H:%M:%S')
            save_state(state)
            ok += 1
            log('[%d/%d] %s 成功 (blocks=%d)' % (i, len(todo), date_str, len(blocks)))
        else:
            fail += 1
            log('[%d/%d] %s 失败' % (i, len(todo), date_str))
        time.sleep(REQUEST_INTERVAL)

    log('同步完成：成功=%d 失败=%d' % (ok, fail))
    return 0 if fail == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
