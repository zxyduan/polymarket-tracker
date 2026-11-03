# framework/store/ 规则

## 职责
存储端口抽象（Protocol）。运行时不 import 具体实现，只依赖接口。

## 文件清单
- `snapshot_port.py` — 快照存储端口
- `state_port.py` — 状态存储端口
- `p1_state_store.py` — P1 状态存储实现

## 允许 import
- `framework/models/`（数据结构）
- `src/storage.py` — 具体 SQLite 实现（通过 TYPE_CHECKING）

## 禁止 import
- `features/`
- `src/bot` / `src/snapshots/commands`

## 注意
`p1_state_store.py` 里 import src.storage 是 `TYPE_CHECKING`，运行时不依赖。
