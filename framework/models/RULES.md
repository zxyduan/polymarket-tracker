# framework/models/ 规则

## 职责
纯数据模型和 Protocol 定义。不包含任何业务逻辑、IO、网络调用。

## 文件清单
- `event.py` — StandardEvent 标准事件模型
- `message.py` — RenderedMessage / NotifierChannel 消息抽象
- `ports.py` — CursorStore / HealthChecker 等端口 Protocol
- `raw.py` — 原始数据结构
- `snapshot.py` — 快照数据结构

## 允许 import
- 仅 Python 标准库

## 禁止 import
- 任何业务包（features/、src/）
- 任何第三方库（除 dataclasses/typing 等标准库）

## 新增文件放哪
- 新数据结构 → 本目录新建 `.py`
- 新 Protocol → 放 `ports.py` 或对应主题文件
