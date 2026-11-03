# src/ 规则（旧代码保留区）

## 职责
strangler 过渡区：保留仍在被运行时使用的旧实现。**新功能不要放这里。**

## 子目录
- `src/bot/` — Telegram bot 指令处理
- `src/core/` — 核心模型和解析（models.py 已 re-export 到 framework/）
- `src/notify/` — 通知通道（ntfy / Telegram）
- `src/snapshots/` — 旧快照采集、命令、渲染（只保留 /list、/vol 两个 bot 指令用函数）
- `src/sources/` — 旧 HTTP / WebSocket 客户端
- `src/utils/` — 工具函数
- `src/storage.py` — SQLite 存储实现
- `src/settings.py` — 配置加载
- `src/main.py` — 命令行入口（35 行，只做启动）

## 允许 import
- `framework/` — 旧代码可以用框架层的东西
- `src/` 内部互相 import

## 禁止
- 不要在 src/ 里写新的业务 feature
- 不要在 src/ 里加新的渲染逻辑（放 features/）
- 不要在 src/ 里加新的运行时循环（放 framework/runtime/）

## 淘汰路线
逐步把 src/ 里的逻辑迁到 framework/ 或 features/，每迁一个就删对应的旧文件。
