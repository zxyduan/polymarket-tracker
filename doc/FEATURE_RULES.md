# FEATURE_RULES.md

## F1 交易员告警
- 输入：RTDS activity 成交事件
- 流程：AlertMessage → LegacyTraderRenderer → ChannelRouter
- 节奏：事件驱动，无定时器

## F2 快照推送
- 输入：snapshot_collector 落库
- 流程：定时触发 → SnapshotPushUsecase → ChannelRouter
- 节奏：按 snapshot_push.schedule 整点
- 去重：BodyHashGuard sha1

## F3 日报
- 输入：当日成交份额窗口
- 流程：定时触发 → DailyReportUsecase → MessagePaginator → ChannelRouter
- 节奏：每日 daily_report.schedule
- 分页：超 4096 字符自动续发
