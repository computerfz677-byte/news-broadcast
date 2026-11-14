#!/bin/bash
# 新闻联播 → GitHub 自动同步
# 每日采集完成后运行：把最新简报与代码变更提交并推送到 GitHub。
#
# 依赖：
#   - /opt/news-broadcast/repo 为 git 仓库副本（clone 自 GitHub）
#   - ~/.git-credentials 保存 GitHub 凭据（权限 600）
#   - ~/.hermes/.env 里的 MINIMAX 等凭据由采集器自行读取

set -u
REPO=/opt/news-broadcast/repo
DATA_SRC=/opt/news-broadcast/data
BRANCH=main

cd "$REPO" || { echo "[git-sync] 仓库目录不存在: $REPO"; exit 1; }

# 1. 同步代码文件（生产目录为准）
for f in app.py collector.py catchup.py notify.py watchdog.py im_queue_consumer.py notion_sync.py; do
  [ -f "/opt/news-broadcast/$f" ] && cp -f "/opt/news-broadcast/$f" "$REPO/$f"
done
for f in news-broadcast.service news-collector.service news-catchup.service news-collector.timer news-watchdog.service news-watchdog.timer news-notion.service news-notion.timer nginx-news.conf; do
  [ -f "/etc/systemd/system/$f" ] && cp -f "/etc/systemd/system/$f" "$REPO/$f"
done

# 2. 同步简报 markdown
if [ -d "$DATA_SRC" ]; then
  mkdir -p "$REPO/docs"
  cp -f "$DATA_SRC"/*.md "$REPO/docs/" 2>/dev/null
fi

# 3. 提交
git config user.name "${GIT_USER_NAME:-CFZ}" 2>/dev/null
git config user.email "${GIT_USER_EMAIL:-cyhzzz@users.noreply.github.com}" 2>/dev/null
git add -A
if git diff --cached --quiet; then
  echo "[git-sync] 无变更，无需推送"
  exit 0
fi
DATE=$(date +%Y-%m-%d)
git commit -q -m "chore: 同步 $DATE 简报与代码" || exit 1

# 4. 推送
git push origin "$BRANCH" 2>&1 | tail -3
if [ ${PIPESTATUS[0]} -eq 0 ]; then
  echo "[git-sync] 推送成功 $(git rev-parse --short HEAD)"
else
  echo "[git-sync] 推送失败"
  exit 1
fi
