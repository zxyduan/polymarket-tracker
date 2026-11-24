"""白名单市场告警 DTO（T1）。

仅标准库 + framework 事件模型；DTO 不出现 `dict[str, Any]` 穿过层边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from framework.models.event import StandardEvent


@dataclass(frozen=True)
class RewardChangeEvent:
    """M1 一轮 diff 命中的奖励变化事件。

    event_code ∈ exit / floor_break / decay_pct / tier_drop / rebound；
    富化标题 / slug 缺省空串，由用例在推送前补。
    """

    cid: str
    event_code: str
    prev_rate: float | None
    cur_rate: float | None
    prev_tier: str | None
    cur_tier: str | None
    asset: str = ""
    slug: str = ""
    question: str = ""
    prev_date: str = ""
    cur_date: str = ""
    collected_ts: datetime | None = None


@dataclass(frozen=True)
class MarketTradeAlert:
    """M2 合并桶内单笔成交：持有归一事件 + 富化冗余标题 / slug。"""

    event: StandardEvent
    title: str = ""
    slug: str = ""
