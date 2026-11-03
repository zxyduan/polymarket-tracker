# framework/parsing/ 规则

## 职责
外部报文 → StandardEvent 的纯函数解析层。不做网络请求、不写库。

## 文件清单
- `data_parsing.py` — Data API /v2/trades 条目解析

## 允许 import
- `framework/models/event.py`（StandardEvent）
- `src/core/parsing.py`（parse_data_activity，过渡期兼容）

## 禁止 import
- `features/`
- `src/storage` / `src/bot` / `src/snapshots/commands`

## 注意
`parse_data_activity` 目前仍在 `src/core/parsing.py`，后续可平移到本目录，但平移时需同步更新 `src/core/parsing.py` 为 re-export。
