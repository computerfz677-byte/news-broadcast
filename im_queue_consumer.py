#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IM 队列消费者（由 hermes cron 调用，--no-agent 模式）

用途：QQ(lightclawbot) / 企业微信(weixin) 的 adapter 只存在于 hermes gateway 进程内，
外部进程无法直接投递。本脚本作为 hermes cron 的 --script，在 gateway 进程内运行，
读取 /opt/news-broadcast/logs/im_queue/<target>.jsonl 中待投递的消息，
把最新一条输出到 stdout，由 cron 的 --deliver 发送到对应平台。

用法：
  python3 im_queue_consumer.py <target_file_stem>
  例：python3 im_queue_consumer.py lightclawbot_616601857

stdout 为空 → cron 静默（不打扰）。每次只投递一条，避免刷屏。
"""

import os
import sys
import json

QUEUE_DIR = os.environ.get('NEWS_IM_QUEUE_DIR', '/opt/news-broadcast/logs/im_queue')


def main():
    if len(sys.argv) < 2:
        return
    stem = sys.argv[1].strip()
    if not stem:
        return
    path = os.path.join(QUEUE_DIR, stem + '.jsonl')
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding='utf-8') as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception:
        return
    if not lines:
        return
    try:
        obj = json.loads(lines[0])
    except Exception:
        # 坏行：丢弃后重写
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines[1:]) + ('\n' if lines[1:] else ''))
        except Exception:
            pass
        return
    text = obj.get('text') or ''
    if not text:
        return
    print(text)
    # 标记已投递：删除这条，剩余留待下次
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines[1:]) + ('\n' if lines[1:] else ''))
    except Exception:
        pass


if __name__ == '__main__':
    main()
