# framework/runtime/ 规则

## 职责
运行时循环：定时调度、heartbeat、快照采集、快照推送、日报推送。只做调度，不含业务规则。

## 文件清单
- `loops.py` — 所有 async 循环函数（daily_report_loop、snapshot_collect_loop、snapshot_push_loop 等）

## 允许 import
- `framework/models/`
- `framework/store/`（端口）
- `framework/sources/`（适配器）
- `features/*/usecase.py`（业务用例）

## 禁止 import
- `src/bot` / `src/snapshots/commands`
- 具体存储实现（只依赖端口）

## 测试 import 规范
测试直接 `from framework.runtime.loops import xxx`，不要 `from src.main import xxx`。
