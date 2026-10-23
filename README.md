# market 交易员监控与消息推送

监控一个或多个 market 交易员地址，交易员发生成交后，实时将市场、方向、价格、数量、金额推送至 Telegram（安卓手机）。RTDS 实时通道 + Data API 对账兜底，断线不遗漏、不重复。

## 工作原理

- market 采用"链下撮合、链上结算"：成交由官方中继批量提交，交易员地址不在交易 `from/to`，因此**不能**用常规链上地址监控，必须走官方索引数据；
- L1 实时层：RTDS WebSocket（`wss://ws-live-data.polymarket.com`）订阅活动流，按 `proxyWallet` 过滤；
- L2 兜底层：Data API 每 60 秒对账，补齐漏发与断线缺口；
- 统一去重键 `txHash:logIndex`，同笔结算多笔成交合并为一条；
- Telegram Bot 长轮询，纯出站连接，服务器无需开放任何入站端口。

## 快速开始

### 1. 创建 Telegram Bot

在 Telegram 找 `@BotFather`，`/newbot` 创建 Bot，获取 Bot Token。

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入 BOT_TOKEN；ALLOWED_CHAT_IDS 可留空（首个 /start 用户自动成为管理员）
```

编辑 `config/config.yaml`，在 `watchlist` 中配置一个或多个交易员地址（推荐填 market 个人主页/排行榜上的地址）：

```yaml
watchlist:
  - address: "0x…"
    label: "交易员A"
  - address: "0x…"
    label: "交易员B"
```

### 3. 本地运行

```bash
pip install -r requirements.txt
python -m src.main --config config/config.yaml
```

然后在 Telegram 给 Bot 发 `/start`，再发 `/test` 验证链路。

### 4. Docker 部署（EC2）

```bash
docker compose up -d --build
docker compose ps          # 查看健康状态
docker compose logs -f     # 查看日志
```

## Telegram 指令

| 指令 | 作用 |
| --- | --- |
| `/add 0x地址 名称` | 新增监控（可加多个） |
| `/remove 0x地址` | 移除监控 |
| `/list` | 监控名单 |
| `/status` | 运行状态 |
| `/test` | 链路测试 |
| `/help` | 帮助 |

## 消息样例

```text
🔵 交易员A · 买入 YES
Will X happen before 2027?
价格 0.632 · 数量 1,500 · 金额 ≈$948
时间 2026-10-19 21:33
[查看市场] [Polygonscan]
```

## EC2 部署要点

- 实例：t4g.micro / t3.micro 即可；安全组入站全部拒绝，出站 443；建议用 SSM 维护；
- 区域：选择可正常访问 market API 的区域，部署前验证；
- 数据持久化：`data/` 目录（SQLite），注意备份。

## 目录结构

```text
config/config.yaml      监控名单与运行参数
src/
  main.py               服务入口
  settings.py           配置加载
  storage.py            SQLite 存储
  sources/              RTDS / Data API / Gamma 客户端
  core/                 事件模型、解析、流水线、渲染
  bot/                  Telegram API、鉴权、指令
tests/                  单元与端到端测试
docs/                   需求技术方案、校验报告、开发方案
```

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
