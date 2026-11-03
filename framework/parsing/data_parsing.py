"""L2 解析：Data API /v2/trades 条目 → StandardEvent（逐字平移旧 client._parse_items）。"""

from __future__ import annotations

from typing import Any

from src.core.models import StandardEvent
from src.core.parsing import parse_data_activity


def parse_data_trades(items: list[Any]) -> list[StandardEvent]:
    events: list[StandardEvent] = []
    for item in items:
        events.extend(parse_data_activity(item))
    return events
