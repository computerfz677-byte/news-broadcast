# 新闻联播 · A股投资参考看板

每日《新闻联播》A 股简报的自动生成 + 看板展示 + 多通道失败告警系统。

## 在线看板
https://news.chenghaha.club/#xinwenlianbo

## 功能
- **每日简报自动采集**：服务器自采集（systemd timer），无需本机在线
- **Web 看板**：文件清单 / 跟踪标的 / 历史合集 / 实时价格
- **多通道失败告警**：Bark、ntfy、飞书、QQ、企业微信
- **健康巡检**：简报缺失、期数连续性、看板存活、行情缓存新鲜度

## 目录结构
```
app.py                  # 后端（纯标准库，零 pip 依赖，ThreadingHTTPServer）
web/index.html          # 前端 SPA
collector.py            # 每日采集 + 简报生成（含失败告警埋点）
catchup.py              # 历史补漏（失败时聚合告警）
notify.py               # 统一告警推送模块（5 类通道 + 降级链 + 去重）
watchdog.py             # 健康巡检（异常告警 / 恢复通知）
im_queue_consumer.py    # IM 队列消费者（供 hermes cron --script 调用）
news-broadcast.service  # 看板 systemd 单元（开机自启 + 崩溃自愈）
news-collector.service  # 每日采集 oneshot
news-collector.timer   # 每日采集定时器
news-catchup.service    # 补漏 oneshot
news-watchdog.service   # 巡检 oneshot
news-watchdog.timer     # 巡检定时器（白天 30min，18-23 点 10min）
nginx-news.conf         # nginx 反代配置（127.0.0.1:8765）
setup.sh                # 一键部署脚本
sync.sh                 # 本机 → 云端增量同步（scp）
git_sync.sh             # 服务器 → GitHub 自动同步（每日 22:00）
notion_sync.py          # 简报 → Notion 归档（增量，markdown → Notion blocks）
docs/                   # 每日简报 markdown（318 期历史归档）
```

## 部署
```bash
sudo bash setup.sh
```

## 自动化流水线（服务器自持，无需本机在线）
```
news-collector.timer  每日 19:30  → collector.py 采集生成简报
news-catchup.service  每日 20:30  → catchup.py 补漏
news-gitsync.timer    每日 22:00  → git_sync.sh 推送简报+代码到 GitHub
news-notion.timer     每日 22:30  → notion_sync.py 归档到 Notion
news-watchdog.timer   白天 30min / 18-23 点 10min → watchdog.py 巡检告警
```

## 告警链路

```
notify.notify(title, body)
  ├─ Bark 网关  127.0.0.1:8091  → 6 台 iPhone 设备故障转移 → api.day.app
  ├─ ntfy       127.0.0.1:8092  (topic: cfz-alert，upstream ntfy.sh)
  ├─ 飞书       开放平台直发 (tenant_access_token → im/v1/messages)
  ├─ 企业微信   群机器人 webhook
  └─ QQ / 企微  hermes send → 失败则入 im_queue → hermes cron 进程内投递
```

去重：跨进程持久化到 `logs/alert_dedup.json`，默认窗口 1800s。

告警环境变量写在 `/opt/news-broadcast/alert.env`（权限 600），service 通过
`EnvironmentFile=-` 加载。

### 注意事项
- Bark 网关会**静默丢弃**含「恢复 / recovered / is up」的消息，恢复通知走
  `notify_recovery()`，绕开网关直接推 ntfy + Bark + 飞书 + 企微。
- ntfy 中文标题必须用 **JSON 发布到根路径 `/`**（HTTP 头不能放非 ASCII），
  且 `tags` 必须是数组。
- QQ / 企业微信的 adapter 只存在于 hermes gateway 进程内，外部进程调用
  `hermes send` 会报 `No live adapter`，必须走 `hermes cron --no-agent
  --script` 队列投递。

## Notion 归档

`notion_sync.py` 把 `/opt/news-broadcast/data/*.md` 逐期创建为 Notion 子页面
（标题 = `YYYY-MM-DD 新闻联播`），markdown 自动转 Notion blocks（heading /
paragraph / bulleted_list_item）。

配置 `/opt/news-broadcast/alert.env`：
```ini
NOTION_TOKEN=secret_xxx
NOTION_PARENT_PAGE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```
集成需在 Notion 中通过「连接页面」授权父页面。

增量同步：状态文件 `logs/notion_sync_state.json` 记录已同步日期，重跑只补新增。
```bash
python3 notion_sync.py             # 增量
python3 notion_sync.py --limit 10  # 只处理最新 10 期（调试）
python3 notion_sync.py --force     # 全量重跑
```

## GitHub 同步

服务器 `/opt/news-broadcast/repo` 是本仓库的 clone，`git_sync.sh` 每次运行会
把生产目录的代码 + `data/*.md` 同步进仓库并推送 `main`。
凭据存 `~/.git-credentials`（权限 600），`~/.gitconfig` 配置 `credential.helper=store`。

## 行情数据源
腾讯行情，前复权日 K：
- 主源 `https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get`
- 备源 `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get`
（云服务器 IP 被腾讯云 WAF 拦截时自动降级）

## 免责声明
本项目所有内容仅供研究与学习参考，不构成任何投资建议。
