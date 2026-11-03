# features/trader/ 规则

## 职责
F1 交易员：交易信号推送。

## 文件清单
- `ports.py` — TraderRenderer Protocol
- `renderer.py` — LegacyTraderRenderer（交易信号消息渲染）
- `usecase.py` — 交易用例

## 允许 import
- `framework/models/message.py`（RenderedMessage）
- `framework/store/`（端口）

## 禁止 import
- `src/` — 新功能不要 import src
- 其他 feature 目录

## 注意
目前 trader renderer 还叫 LegacyTraderRenderer，后续应重写为纯 features 实现，去掉 Legacy 前缀。
