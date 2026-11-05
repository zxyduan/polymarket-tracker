"""F3 日报端口（§7）。"""

from __future__ import annotations

from typing import Any, Protocol

from framework.models.message import RenderedMessage


class DailyRenderer(Protocol):
    def render(
        self,
        rows: list[dict[str, Any]],
        *,
        windows_days: list[int],
        now_bucket: int,
        tz_offset_hours: int,
        top_n: int,
        missing_markets: list[str] | None = None,
    ) -> RenderedMessage | None: ...
