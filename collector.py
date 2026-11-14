#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻联播云端采集器
==================
完全独立运行于服务器：抓央视网当日条目 -> 抽取通稿全文 -> 调 MiniMax LLM 生成
A股投资参考简报(md) -> 写入看板数据目录 -> 看板自动刷新。

不依赖本机，不依赖 WorkBuddy。纯标准库 + LLM HTTP API。
"""

import os
import re
import sys
import json
import time
import html
import glob
import datetime
import urllib.request
import urllib.error
import argparse

# ---------------------------------------------------------------- 配置
DATA_DIR = os.environ.get('NEWS_DATA_DIR', '/opt/news-broadcast/data')
LOG_DIR = os.environ.get('NEWS_LOG_DIR', '/opt/news-broadcast/logs')
LLM_ENV = os.environ.get('NEWS_LLM_ENV', '/home/ubuntu/daily_stock_analysis/.env')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

# 生成时间窗：19:10 之后才允许生成当日简报（避免拿到未播出的内容）
NOT_BEFORE_HOUR = 19
NOT_BEFORE_MIN = 10

WEEKDAY_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def log(msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = '[%s] %s' % (ts, msg)
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, 'collector.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _alert(date_str, reason):
    """采集失败告警：Bark + ntfy 双通道。任何异常都不能影响采集流程本身。"""
    try:
        import notify
        title = '新闻联播采集失败 %s' % date_str
        body = ('日期: %s\n原因: %s\n主机: %s\n时间: %s' % (
            date_str, reason,
            os.environ.get('NEWS_ALERT_HOST', 'sh-43.142.99.125'),
            datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        notify.notify(title, body, level='active', priority=5,
                      tags='rotating_light,warning',
                      dedup_key='collect-fail-%s' % date_str)
    except Exception as e:
        log('告警发送异常（忽略）: %s: %s' % (type(e).__name__, str(e)[:200]))


def http_get(url, timeout=20, encoding=None):
    """抓取网页，返回 (text, final_url)。失败返回 (None, None)。"""
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            final = r.geturl()
    except Exception as e:
        log('  HTTP失败 %s -> %s: %s' % (url[:80], type(e).__name__, str(e)[:120]))
        return None, None
    if encoding:
        try:
            return raw.decode(encoding, errors='ignore'), final
        except Exception:
            pass
    # 自动判定编码
    head = raw[:2000].lower()
    if b'gb2312' in head or b'gbk' in head:
        try:
            return raw.decode('gb18030', errors='ignore'), final
        except Exception:
            pass
    return raw.decode('utf-8', errors='ignore'), final


def strip_html(fragment):
    t = re.sub(r'<script.*?</script>', '', fragment, flags=re.S | re.I)
    t = re.sub(r'<style.*?</style>', '', t, flags=re.S | re.I)
    t = re.sub(r'<br\s*/?>|</p>|</div>|</h\d>', '\n', t, flags=re.I)
    t = re.sub(r'<[^>]+>', '', t)
    t = html.unescape(t)
    lines = [x.strip() for x in t.split('\n')]
    lines = [x for x in lines if x]
    return '\n'.join(lines)


# ------------------------------------------------------- 第一步: 抓当日条目
def fetch_day_items(date_str):
    """date_str: 'YYYYMMDD'。返回 [(title, url, content_text), ...]"""
    y, m, d = date_str[:4], date_str[4:6], date_str[6:8]

    # 1) 栏目日合集页
    day_url = 'https://tv.cctv.com/lm/xwlb/day/%s.shtml' % date_str
    page, _ = http_get(day_url)
    if not page:
        # 回退：整期节目页目录
        log('  日合集页失败，尝试整期节目页')
        page, _ = http_get('https://tv.cctv.com/%s/%s/%s/' % (y, m, d))

    if not page:
        return []

    # 抽取条目链接（同一日期的 VIDE...shtml）
    # 注意：VIDE 尾缀是「两位年+月日」，如 VIDE...261114，不是 20261114
    link_re = re.compile(
        r'https://tv\.cctv\.com/%s/%s/%s/(VIDE[A-Za-z0-9]+)\.shtml' % (y, m, d))
    urls = []
    seen = set()
    for mu in link_re.finditer(page):
        u = 'https://tv.cctv.com/%s/%s/%s/%s.shtml' % (y, m, d, mu.group(1))
        if u not in seen:
            seen.add(u)
            urls.append(u)

    if not urls:
        log('  未从合集页提取到条目链接')
        return []

    log('  提取到 %d 个条目链接，开始抓正文' % len(urls))

    items = []
    for i, u in enumerate(urls, 1):
        page2, _ = http_get(u)
        if not page2:
            continue
        title = ''
        mt = re.search(r'<title>([^<]*)</title>', page2)
        if mt:
            title = mt.group(1).strip()
            title = re.sub(r'^\[视频\]', '', title).strip()
        m = re.search(r'<div class="content_area"[^>]*>(.*?)</div>\s*<!--', page2, re.S)
        if not m:
            m = re.search(r'<div class="content_area"[^>]*>(.*?)</div>', page2, re.S)
        if not m:
            continue
        text = strip_html(m.group(1))
        # 去掉尾部编辑署名/评论块
        text = re.split(r'\n编辑[：:]', text)[0]
        text = re.split(r'\n责任编辑', text)[0]
        text = re.sub(r'\n全部评论[\s\S]*$', '', text).strip()
        if len(text) < 40:
            continue
        items.append((title, u, text))
        log('    [%d/%d] %s (%d字)' % (i, len(urls), title[:36], len(text)))
        time.sleep(0.4)

    return items


def fetch_program_outline(date_str):
    """整期节目页的『本期节目主要内容』——用于补标题。"""
    y, m, d = date_str[:4], date_str[4:6], date_str[6:8]
    page, _ = http_get('https://tv.cctv.com/%s/%s/%s/' % (y, m, d))
    if not page:
        return ''
    m = re.search(r'<div class="playlist_title"[^>]*>(.*?)</div>', page, re.S)
    if not m:
        m = re.search(r'本期节目主要内容(.*?)</div>', page, re.S)
    return strip_html(m.group(1)) if m else ''


# ------------------------------------------------- 第二步: 组装 prompt
PROMPT_TEMPLATE = """你是一名资深A股财经分析师，正在撰写 {date}《新闻联播》的投资参考简报。

以下是 {date} 当晚《新闻联播》的完整节目内容（按播出顺序，含央视网官方通稿全文）。请基于**仅这份材料**完成分析。

=== 节目内容 ===
{content}
=== 节目内容结束 ===

严格按以下 Markdown 结构输出，**不要输出任何解释性前后缀**，直接以 "# " 开头：

# {date} 新闻联播 · A股投资参考简报

- **生成日期**：{date}（{weekday}，Asia/Shanghai）
- **播出日期**：{date} 19:00《新闻联播》
- **数据来源**：央视网《新闻联播》{date} 栏目官方条目页（{item_count} 条节目，通稿全文）
- **说明**：本简报由云端自动采集央视网官方通稿后经 AI 分析生成

---

## 一、原文逐字稿（按播出顺序整理）

1. **【标题】**
   原文内容……

（逐条列出上述节目内容中的每一条，保留原文表述，不要概括、不要删减要点。若某条为快讯类（如"国内联播快讯""国际联播快讯"），其内部每则也单独编号展开。）

---

## 二、内容摘要

（8~12 条要点，凝练概括当日主线）

---

## 三、与A股相关的信息分析

| # | 新闻主题 | 相关板块 | 影响方向 | 逻辑 |
|---|----------|----------|----------|------|
| 1 | 主题 | 板块/板块 | **利好** | 逻辑说明（40~70字） |

（覆盖当日最重要的 8~12 条新闻主题；影响方向只允许 **利好**、**中性**、**利空**、**中性偏利好**、**中性偏利空**）

---

## 四、相关投资标的

| 代码 | 名称 | 所属板块 | 入选理由（紧扣当日新闻） | 关联条目 |
|------|------|----------|--------------------------|----------|

（6~10 只。代码必须是真实存在的 A 股 6 位代码，名称与代码必须严格对应。优先选当日新闻直接相关的行业龙头/细分冠军。入选理由 30~50 字，必须紧扣当日具体新闻。关联条目写条目序号，如"条目1、条目3"）

> 注：以上代码为A股常见标的，选取与当日新闻直接相关的龙头/细分冠军；仅供参考，不构成投资建议。

---

## 五、参照历史经验的投资策略

（分点：先写 3~4 条可参照的历史行情逻辑，每条说明"哪年什么事件 → 哪些板块走强 → 对应的可操作方向"；再写"**可操作策略**"，含仓位建议、方向优先级、节奏建议、风险提示）

---

## 免责声明

本简报由云端系统自动采集央视网公开通稿后经 AI 分析生成，所有内容仅供研究与学习参考，**不构成任何投资建议**。据此操作风险自担。
"""


def build_prompt(date_str, items):
    parts = []
    for i, (title, url, text) in enumerate(items, 1):
        parts.append('%d. 【%s】\n%s' % (i, title or '（无标题）', text))
    content = '\n\n'.join(parts)
    d = datetime.datetime.strptime(date_str, '%Y%m%d')
    return PROMPT_TEMPLATE.format(
        date=d.strftime('%Y-%m-%d'),
        weekday=WEEKDAY_CN[d.weekday()],
        content=content,
        item_count=len(items),
    )


# ------------------------------------------------------ 第三步: 调 LLM
def load_env(path):
    env = {}
    try:
        with open(path, encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as e:
        log('  读取 env 失败 %s: %s' % (path, e))
    return env


def call_llm(prompt, max_tokens=16000, retries=3):
    env = load_env(LLM_ENV)
    key = env.get('LLM_MINIMAX_API_KEY') or os.environ.get('LLM_MINIMAX_API_KEY')
    base = env.get('LLM_MINIMAX_BASE_URL') or os.environ.get(
        'LLM_MINIMAX_BASE_URL', 'https://api.minimaxi.com/v1')
    model = env.get('LLM_MINIMAX_MODELS') or os.environ.get(
        'LLM_MINIMAX_MODELS', 'MiniMax-M2.7')
    if not key:
        log('  未找到 LLM API key (env: %s)' % LLM_ENV)
        return None

    url = base.rstrip('/') + '/chat/completions'
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
        'temperature': 0.3,
    }).encode('utf-8')

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + key,
                'User-Agent': UA,
            })
            with urllib.request.urlopen(req, timeout=600) as r:
                data = json.loads(r.read().decode('utf-8'))
            out = data['choices'][0]['message']['content']
            if not out or len(out.strip()) < 200:
                raise ValueError('返回内容过短 (%d)' % len(out or ''))
            usage = data.get('usage') or {}
            log('  LLM成功 attempt=%d len=%d usage=%s' % (
                attempt, len(out), json.dumps(usage, ensure_ascii=False)))
            return out
        except Exception as e:
            log('  LLM失败 attempt=%d/%d: %s: %s' % (
                attempt, retries, type(e).__name__, str(e)[:200]))
            try:
                detail = e.read().decode('utf-8', errors='ignore')[:300]
                if detail:
                    log('  详情: %s' % detail)
            except Exception:
                pass
            if attempt < retries:
                time.sleep(5 * attempt)
    return None


# ------------------------------------------------------ 第四步: 落盘
def save_brief(date_str, md):
    d = datetime.datetime.strptime(date_str, '%Y%m%d')
    name = '%s新闻联播元宝.md' % d.strftime('%Y-%m-%d')
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(md)
    return path


def clean_output(md):
    """清理 LLM 输出：去掉思维链、代码围栏、前后缀说明，找到正文起点。"""
    # 1) 去 <think>...</think>（含未闭合的情况）
    md = re.sub(r'<think>[\s\S]*?</think>', '', md)
    md = re.sub(r'<think>[\s\S]*$', '', md)
    md = re.sub(r'</think>', '', md)
    # 2) 去代码围栏
    md = re.sub(r'^```[a-zA-Z]*\s*\n', '', md)
    md = re.sub(r'\n```\s*$', '', md)
    # 3) 找到正文起点：第一行以 "# " 开头的行
    lines = md.split('\n')
    start = None
    for i, ln in enumerate(lines):
        if re.match(r'^#\s+\S', ln):
            start = i
            break
    if start is not None and start > 0:
        md = '\n'.join(lines[start:])
    return md.strip()


def validate(md):
    """检查结构完整性，返回问题列表。"""
    problems = []
    required = [
        '## 一、原文逐字稿', '## 二、内容摘要',
        '## 三、与A股相关的信息分析', '## 四、相关投资标的',
        '## 五、参照历史经验的投资策略', '## 免责声明',
    ]
    for r in required:
        if r not in md:
            problems.append('缺少章节: %s' % r)
    if not re.search(r'^#\s+\S', md, re.M):
        problems.append('缺少 H1 标题')
    if '## 四、相关投资标的' in md:
        seg = md.split('## 四、相关投资标的')[1].split('## 五')[0]
        codes = re.findall(r'\|\s*(\d{6})\s*\|', seg)
        if len(codes) < 5:
            problems.append('标的数量不足: %d' % len(codes))
        # 合法 A 股代码前缀：沪 60/68，深 00/30，创业 30，北 8/4
        bad = [c for c in codes if not re.match(
            r'^(600|601|603|605|688|689|000|001|002|003|300|301|200|430|83[0-9]|87[0-9]|88[0-9])', c)]
        if bad:
            problems.append('疑似非法代码: %s' % bad)
    if len(md) < 3000:
        problems.append('内容过短: %d 字符' % len(md))
    return problems


def already_exists(date_str):
    d = datetime.datetime.strptime(date_str, '%Y%m%d')
    name = '%s新闻联播元宝.md' % d.strftime('%Y-%m-%d')
    return os.path.exists(os.path.join(DATA_DIR, name))


# ------------------------------------------------------------- 主流程
def run(date_str=None, force=False):
    now = datetime.datetime.now()
    if date_str is None:
        date_str = now.strftime('%Y%m%d')

    log('=' * 62)
    log('采集启动 date=%s force=%s now=%s' % (date_str, force, now.strftime('%H:%M:%S')))

    # 时间窗保护：仅针对「今天」——未到播出时间不生成当日内容
    # 补历史日期（date_str 非今天）不受此限制
    is_today = (date_str == now.strftime('%Y%m%d'))
    if is_today and not force:
        earliest = now.replace(hour=NOT_BEFORE_HOUR, minute=NOT_BEFORE_MIN,
                               second=0, microsecond=0)
        if now < earliest:
            log('未到播出时间 %02d:%02d，放弃生成当日简报' % (NOT_BEFORE_HOUR, NOT_BEFORE_MIN))
            return 0
    # 跨零点运行场景：目标为今天时回退到昨天
    if is_today and now.hour < 3 and not force:
        y = now - datetime.timedelta(days=1)
        date_str = y.strftime('%Y%m%d')
        is_today = False
        log('跨零点，目标日期回退为 %s' % date_str)

    if already_exists(date_str) and not force:
        log('目标日期已存在简报，跳过（--force 可覆盖）')
        return 0

    # 1. 抓条目
    log('[1/3] 抓取央视网当日条目…')
    items = fetch_day_items(date_str)
    if not items:
        log('未抓取到任何条目（当日可能尚未播出）')
        _alert(date_str, '央视网未抓取到任何条目')
        return 1
    log('  共 %d 条，合计 %d 字' % (
        len(items), sum(len(t[2]) for t in items)))

    # 2. 调 LLM
    log('[2/3] 生成分析（调用 LLM，可能需数分钟）…')
    prompt = build_prompt(date_str, items)
    md = call_llm(prompt)
    if not md:
        log('LLM 失败，未写入文件')
        _alert(date_str, 'LLM 生成失败，未写入文件')
        return 2

    # 3. 校验 + 落盘
    log('[3/3] 校验并写入…')
    md = clean_output(md)
    problems = validate(md)
    if problems:
        log('  校验发现问题: %s' % '; '.join(problems))
        if len(problems) >= 3:
            log('  问题过多，放弃写入以免污染数据目录')
            _alert(date_str, '校验问题过多(%d项)，放弃写入：%s' % (
                len(problems), '; '.join(problems)[:200]))
            return 3
    else:
        log('  校验通过')

    path = save_brief(date_str, md)
    log('写入成功: %s (%d 字符)' % (path, len(md)))
    log('=' * 62)
    return 0


def main():
    ap = argparse.ArgumentParser(description='新闻联播云端采集器')
    ap.add_argument('--date', help='目标日期 YYYYMMDD，默认今天')
    ap.add_argument('--force', action='store_true', help='已存在也覆盖')
    ap.add_argument('--fetch-only', action='store_true', help='只抓取不调 LLM（调试用）')
    args = ap.parse_args()

    if args.fetch_only:
        items = fetch_day_items(args.date or datetime.datetime.now().strftime('%Y%m%d'))
        for i, (t, u, x) in enumerate(items, 1):
            print('%2d. [%s] %s (%d字) %s' % (i, t, '', len(x), u))
        return 0

    sys.exit(run(args.date, args.force))


if __name__ == '__main__':
    main()
