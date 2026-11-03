# features/ 规则

## 职责
业务 feature 层：每个目录是一个独立业务模块（F1 trader / F2 snapshot / F3 daily）。
包含端口定义、渲染器、用例。

## 通用规则
- **禁止 import src/** — 业务逻辑必须自己实现，不能包装旧代码
- 只 import `framework/` 里的端口和模型
- 每个 feature 目录结构统一：
  - `ports.py` — 端口 Protocol
  - `renderer.py` — 消息渲染
  - `usecase.py` — 业务用例（核心规则）

## 新增 feature
新建 `features/<新名字>/`，必须包含 ports.py / renderer.py / usecase.py 三个文件。
