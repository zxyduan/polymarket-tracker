"""L2 解析：Data API /v2/trades 条目 → StandardEvent。"""

from __future__ import annotations

from typing import Any

from framework.models.event import StandardEvent


def parse_data_trades(items: list[Any]) -> list[StandardEvent]:
    from src.core.parsing import parse_data_activity

    events: list[StandardEvent] = []
    for item in items:
        events.extend(parse_data_activity(item))
    return events
