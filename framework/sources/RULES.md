# framework/sources/ 规则

## 职责
外部数据源适配器：HTTP 客户端、WebSocket 客户端的薄壳封装。只做协议翻译，不做业务判断。

## 文件清单
- `data_api_adapter.py` — Data API /v2/trades 适配器
- `rtds_adapter.py` — RTDS WebSocket 适配器

## 允许 import
- `framework/models/`（StandardEvent、CursorStore 等）
- `framework/parsing/`（parse_data_trades）
- `src/sources/`（旧客户端，适配器包装它们）

## 禁止 import
- `features/` — sources 不认识业务
- `src/storage` — 适配器不直接写库
- `src/snapshots/collector` — 采集逻辑在 runtime

## 新增文件放哪
- 新外部 API 接入 → 本目录新建 `xxx_adapter.py`，实现对应的 Source Protocol
