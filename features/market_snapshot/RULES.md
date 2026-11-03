# features/market_snapshot/ 规则

## 职责
F2 快照总览：每 2 小时推送一次市场量、波动率、现价。

## 文件清单
- `ports.py` — SnapshotRenderer Protocol
- `renderer.py` — SnapshotSummaryRenderer（快照总览消息渲染）
- `usecase.py` — 快照推送用例（去重、延迟告警、多桶对齐）

## 允许 import
- `framework/models/message.py`（RenderedMessage）
- `framework/store/`（端口）
- 标准库（html、datetime 等）

## 禁止 import
- `src/` — 快照渲染逻辑已完全在本目录实现
- `features/daily_report/` — feature 之间不互相依赖

## 渲染规则
- REGIME_CN: low=低 / normal=正常 / high=高
- TREND_ICON: up=📈 / down=📉 / flat=➡️
- 现价显示 4 位小数（0.6300）
- 数据延迟 ≥2 个桶时显示告警
- 不同市场桶时间不一致时，落后的行末尾标注桶时段
