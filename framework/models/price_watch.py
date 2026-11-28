"""price_watch 关键标的价格突破告警 DTO。

仅标准库 + framework 模型；DTO 不出现 `dict[str, Any]` 穿过层边界
（`raw` 作为原始响应留存，不参与判定）。数值统一 `float`，与仓库现状一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

PriceCategory = Literal["crypto", "stock", "tokenized_stock"]
PriceKind = Literal["crypto", "stock", "dex"]
PriceEventCode = Literal["break_above", "break_below"]

# 预留事件码（本期不触发）：pct_up / pct_down


@dataclass(frozen=True)
class Quote:
    """一轮采集归一后的标的行情报价。

    `price` 为有限正数 USD 价格；解析失败不产出 Quote，由调用方记 None。
    `fetched_at` 必须是 aware UTC 时间。
    """

    symbol: str
    label: str
    category: PriceCategory
    price: float
    price_kind: PriceKind
    source: str
    fetched_at: datetime
    liquidity_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PriceAlertEvent:
    """价位突破命中后交由渲染 / 推送的告警事件。"""

    symbol: str
    label: str
    category: PriceCategory
    event_code: PriceEventCode
    price: float
    threshold: float
    price_kind: PriceKind
    source: str
    event_time: datetime
    liquidity_usd: float | None = None
