# features/watchlist_alert/ 规则

## 职责
白名单市场告警：M1 清单市场奖励变化轮询告警；M2 清单市场成交实时合并推送。

## 文件清单
- `ports.py` — `RewardWatcherPort` / `AlertRendererPort` Protocol
- `renderer.py` — `RewardChangeRenderer` / `TradeAlertRenderer`（消息渲染）
- `usecase.py` — `RewardWatchUsecase` / `TradeAlertUsecase`（业务编排）

## 允许 import
- `framework/models/`（事件 / DTO / 消息 DTO）
- `framework/parsing/`（纯函数）
- `framework/delivery/`（router）
- `framework/scheduler/`（BodyHashGuard）

## 禁止 import
- `src/` — 配置类型 / 渲染辅助 / 存储实现一律不反向依赖；配置以 `Any` 注入
- 其他 feature 目录

## 注意
- 渲染辅助（转义 / 数值 / 时间格式化）在本模块内自带，不引 `src/core/renderer.py`。
- 配置 dataclass 定义在 `src/settings.py`，用例层只持有 `config: Any`。
