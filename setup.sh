#!/usr/bin/env bash
# 新闻联播看板 · 云端一键部署(修订版)
# 关键安全点：
#   1) news.chenghaha.club 在服务器上已有 nginx 配置 + Let's Encrypt 证书，
#      本脚本先备份旧配置，再复用既有证书，仅把反代目标改为 127.0.0.1:8765，
#      不申请新证书、不破坏其他站点。
# 用法（服务器上，以 ubuntu 用户执行）：
#   sudo bash /tmp/news-deploy/setup.sh
set -e
APP=/opt/news-broadcast
HERE="$(cd "$(dirname "$0")" && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"

echo "== [1/5] 安装 nginx + certbot(已装则跳过) =="
sudo apt-get update -y
sudo apt-get install -y nginx python3-certbot-nginx

echo "== [2/5] 部署应用文件 =="
sudo mkdir -p "$APP/data" "$APP/.price_cache"
sudo cp "$HERE/app.py" "$APP/app.py"
sudo cp -r "$HERE/web" "$APP/web"
sudo cp "$HERE/data/"*.md "$APP/data/" 2>/dev/null || true
sudo cp "$HERE/.price_cache/kline.json" "$APP/.price_cache/kline.json" 2>/dev/null || true
sudo chown -R ubuntu:ubuntu "$APP"

echo "== [3/5] 注册 systemd 服务 =="
sudo cp "$HERE/news-broadcast.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now news-broadcast
sleep 2
sudo systemctl status news-broadcast --no-pager | head -n 6 || true

echo "== [4/5] 配置 nginx(复用既有 news.chenghaha.club 证书) =="
# 备份既有配置，避免误改导致不可逆
if [ -f /etc/nginx/sites-available/news.chenghaha.club ]; then
  sudo cp /etc/nginx/sites-available/news.chenghaha.club "/etc/nginx/sites-available/news.chenghaha.club.bak-$TS"
  echo "已备份 available 配置 -> .bak-$TS"
fi
if [ -f /etc/nginx/sites-enabled/news.chenghaha.club ]; then
  sudo cp /etc/nginx/sites-enabled/news.chenghaha.club "/etc/nginx/sites-enabled/news.chenghaha.club.bak-$TS"
  echo "已备份 enabled 配置 -> .bak-$TS"
fi
sudo cp "$HERE/nginx-news.conf" /etc/nginx/sites-available/news.chenghaha.club
sudo rm -f /etc/nginx/sites-enabled/news.chenghaha.club
sudo ln -sf /etc/nginx/sites-available/news.chenghaha.club /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

echo "== [5/5] 校验证书有效期(已存在则跳过申请) =="
if sudo test -f /etc/letsencrypt/live/news.chenghaha.club/fullchain.pem; then
  EXPIRE=$(sudo openssl x509 -enddate -in /etc/letsencrypt/live/news.chenghaha.club/fullchain.pem -noout 2>/dev/null | cut -d= -f2)
  echo "news.chenghaha.club 证书到期: $EXPIRE (自动续期由 certbot timer 负责)"
else
  echo "未找到证书，尝试申请..."
  sudo certbot --nginx -d news.chenghaha.club --non-interactive --agree-tos --register-unsafely-without-email
fi

echo "============================================"
echo " 部署完成 ✅  访问 https://news.chenghaha.club/#xinwenlianbo"
echo " 状态: sudo systemctl status news-broadcast"
echo " 日志: sudo journalctl -u news-broadcast -f"
echo "============================================"
