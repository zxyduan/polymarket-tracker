# features/daily_report/ 规则

## 职责
F3 日报：每日成交份额汇总推送。

## 文件清单
- `ports.py` — DailyRenderer Protocol
- `renderer.py` — DailyReportRenderer（日报消息渲染，含份额格式化、排序、截断）
- `usecase.py` — 日报用例（窗口计算、top_n 截断、分段）

## 允许 import
- `framework/models/message.py`（RenderedMessage）
- `framework/store/`（端口）
- 标准库（html、datetime 等）

## 禁止 import
- `src/` — 日报渲染逻辑已完全在本目录实现
- `features/market_snapshot/` — feature 之间不互相依赖

## 渲染规则
- 序号 1-9 用键帽 emoji（1️⃣ 2️⃣ ...），≥10 用普通数字
- 份额显示千分位（128,540），不带 $ 符号
- HTML 转义用标准库 `html.escape()`
