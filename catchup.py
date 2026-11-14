#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻联播补漏脚本
================
开机时（以及可手动触发）扫描数据目录，找出缺失日期并补齐。

逻辑：
1. 列出 data 目录已有的简报日期
2. 从「最近有简报的日期」往前回溯 N 天（默认 7），找出缺失日
3. 对每个缺失日调用 collector 的抓取+生成流程
4. 单日失败不影响其他日期

注意：只补「已经播出过」的日期。今天若还没播出（19:10 前），会跳过。
"""

import os
import re
import sys
import glob
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collector as C

LOOKBACK_DAYS = int(os.environ.get('NEWS_CATCHUP_LOOKBACK', '7'))


def existing_dates():
    """返回已有简报日期集合（date 对象）"""
    out = set()
    for p in glob.glob(os.path.join(C.DATA_DIR, '*.md')):
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', os.path.basename(p))
        if m:
            try:
                out.add(datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                pass
    return out


def main():
    C.log('=' * 62)
    C.log('补漏任务启动 lookback=%d 天 data=%s' % (LOOKBACK_DAYS, C.DATA_DIR))

    have = existing_dates()
    if not have:
        C.log('数据目录为空，跳过补漏')
        return 0
    latest = max(have)
    C.log('现有 %d 期，最新 %s' % (len(have), latest))

    today = datetime.date.today()
    # 今天若还没到播出时间，不纳入补漏范围
    now = datetime.datetime.now()
    cutoff = now.replace(hour=C.NOT_BEFORE_HOUR, minute=C.NOT_BEFORE_MIN,
                          second=0, microsecond=0)
    today_in_scope = now >= cutoff

    candidates = []
    for i in range(1, LOOKBACK_DAYS + 1):
        d = latest - datetime.timedelta(days=i)
        if d > today:
            continue
        if d == today and not today_in_scope:
            continue
        if d not in have:
            candidates.append(d)

    if not candidates:
        C.log('无需补漏（近 %d 天内无缺失）' % LOOKBACK_DAYS)
        return 0

    C.log('发现 %d 个缺失日期: %s' % (
        len(candidates), ', '.join(d.isoformat() for d in sorted(candidates))))

    ok, fail = 0, 0
    failed_dates = []
    for d in sorted(candidates):
        ds = d.strftime('%Y%m%d')
        C.log('--- 补齐 %s ---' % ds)
        try:
            rc = C.run(ds, force=False)
            if rc == 0:
                ok += 1
            else:
                fail += 1
                failed_dates.append('%s(rc=%d)' % (ds, rc))
                C.log('  %s 生成失败 rc=%d' % (ds, rc))
        except Exception as e:
            fail += 1
            failed_dates.append('%s(%s)' % (ds, type(e).__name__))
            C.log('  %s 异常: %s: %s' % (ds, type(e).__name__, str(e)[:200]))

    C.log('补漏完成：成功 %d，失败 %d' % (ok, fail))
    if fail:
        try:
            import notify
            notify.notify(
                '新闻联播补漏失败 %d 期' % fail,
                '失败日期: %s\n主机: sh-43.142.99.125' % ', '.join(failed_dates),
                level='active', priority=5, tags='rotating_light,warning',
                dedup_key='catchup-fail-%s' % datetime.date.today().isoformat())
        except Exception as e:
            C.log('告警发送异常（忽略）: %s' % str(e)[:160])
    C.log('=' * 62)
    return 0


if __name__ == '__main__':
    sys.exit(main())
