#!/usr/bin/env bash
# 新闻联播看板 · 本机 → 云端同步（Windows Git Bash 可直接跑）
# 作用：把本机今日新简报 md 推送到服务器 /opt/news-broadcast/data，并重启看板服务
# 依赖：~/.workbuddy/keys/news-deploy_ed25519（已加入服务器 authorized_keys）
set -e

SERVER=ubuntu@43.142.99.125
REMOTE_DATA=/opt/news-broadcast/data
KEY="$HOME/.workbuddy/keys/news-deploy_ed25519"
KH="$HOME/.workbuddy/keys/known_hosts"
SRC_DESKTOP="/c/Users/CFZ/Desktop/新闻联播"
SRC_CLAW="/c/Users/CFZ/WorkBuddy/Claw/新闻联播"
TMP="/c/Users/CFZ/WorkBuddy/news-broadcast/.sync_tmp"

SSH="ssh -i $KEY -o UserKnownHostsFile=$KH -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=15"
SCP="scp -i $KEY -o UserKnownHostsFile=$KH -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=15"

mkdir -p "$TMP"
find "$TMP" -maxdepth 1 -name '*.md' -delete 2>/dev/null || true

echo "== [1/3] 收集待同步的 md =="
N=0
TODAY=$(date +%Y-%m-%d)
for D in "$SRC_DESKTOP" "$SRC_CLAW"; do
  [ -d "$D" ] || continue
  for f in "$D/$TODAY"*.md; do
    [ -f "$f" ] || continue
    cp -f "$f" "$TMP/" && N=$((N+1))
  done
done
echo "待同步 $N 个文件"

if [ "$N" -gt 0 ]; then
  echo "== [2/3] 上传到服务器 =="
  $SCP "$TMP"/*.md "$SERVER:$REMOTE_DATA/"
else
  echo "   （今日无新文件，跳过上传）"
fi

echo "== [3/3] 重启云端看板服务 =="
$SSH "$SERVER" 'sudo systemctl restart news-broadcast; for i in 1 2 3 4 5 6 7 8 9 10; do sleep 2; if curl -sS -m 5 http://127.0.0.1:8765/api/status >/dev/null 2>&1; then break; fi; done; curl -sS -m 10 http://127.0.0.1:8765/api/status | head -c 220'
echo
echo "完成 ✅  https://news.chenghaha.club/#xinwenlianbo"
