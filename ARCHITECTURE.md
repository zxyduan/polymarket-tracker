# polymarket-tracker 架构约束（红线）

> 本文档是全局依赖规则，任何 PR 不得违反。新增代码前先读本文件。

## 依赖方向

```
src/（旧代码，strangler 过渡）
  ↑
  │  src/ 可以 import framework/ 和自己
  │
framework/（框架层：端口 + 适配器）
  ↑
  │  framework/ 可以 import 自己和 src/（仅适配器层允许）
  │
features/（业务 feature：用例 + 渲染）
  ↑
  │  features/ 可以 import framework/，禁止 import src/
  │
tests/
     可以 import 任何层，但必须直取目标模块，不走 main.py re-export
```

## 分层职责

| 层 | 职责 | 能不能有业务逻辑 |
|---|---|---|
| `framework/models/` | 纯数据模型（dataclass、DTO、Protocol） | 不能，只放结构 |
| `framework/store/` | 存储端口（抽象接口） | 不能 |
| `framework/sources/` | 外部数据源适配器（HTTP/WebSocket） | 薄壳，只做协议翻译 |
| `framework/parsing/` | 报文 → 标准事件解析 | 纯函数 |
| `framework/runtime/` | 运行时循环（heartbeat / snapshot / daily） | 调度逻辑，不含业务规则 |
| `framework/app.py` | 应用装配（build_app） | 只做依赖注入，不含逻辑 |
| `features/*/ports.py` | feature 端口（Protocol） | 接口定义 |
| `features/*/renderer.py` | feature 渲染（消息格式化） | 纯函数 |
| `features/*/usecase.py` | feature 业务用例 | 核心业务规则在这 |
| `src/` | 旧代码保留区 | 逐步淘汰，新功能不放这里 |

## 红线

1. **features/ 禁止 import src/** — 渲染逻辑、业务规则必须在 features/ 里自己实现
2. **framework/models/ 禁止 import 任何业务包** — 只能 import 标准库
3. **tests/ 禁止 `from src.main import`** — 直接从 `framework.runtime.loops` 或 `framework.app` import
4. **main.py 只做命令行入口** — 不写业务逻辑，不 re-export 给测试
5. **bootstrap 失败直接 crash** — 不允许 try/except fallback 回旧逻辑
6. **P1_SWITCH 开关已删除** — 不要再加 env 开关做双跑

## 新增代码放哪

| 你要写的东西 | 放哪 |
|---|---|
| 新的市场规则计算 | `features/<对应feature>/usecase.py` |
| 新的消息格式 | `features/<对应feature>/renderer.py` |
| 新的外部 API 接入 | `framework/sources/` |
| 新的数据解析逻辑 | `framework/parsing/` |
| 新的存储表结构 | `framework/store/`（接口）+ `src/storage.py`（实现） |
| 新的运行时定时任务 | `framework/runtime/loops.py` |
| 配置项 | `src/settings.py` + `config/config.yaml` |
