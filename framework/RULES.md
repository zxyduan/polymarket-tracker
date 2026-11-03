# framework/ 规则

## 职责
框架层：定义端口（Protocol）、实现适配器、提供运行时基础设施。不包含业务规则。

## 允许 import
- `framework/*` 内部
- `src/` — 仅在 `sources/`、`parsing/`、`store/` 适配器层允许（包装旧客户端）

## 禁止 import
- `features/` — 框架不认识业务 feature
- `tests/`

## 新增文件放哪
- 新端口接口 → `framework/models/ports.py` 或对应子目录
- 新适配器实现 → 对应子目录（sources/parsing/store/render/delivery/scheduler）
- 新装配逻辑 → `framework/app.py`
- 新运行时循环 → `framework/runtime/loops.py`
