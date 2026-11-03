"""F3 日报渲染适配层（§7）。"""

from __future__ import annotations

from typing import Any, Mapping

from framework.models.message import RenderedMessage


class LegacyDailyRenderer:
    def render(self, rows: list[Any], *, windows_days: list[int],
               now_bucket: int, tz_offset_hours: int,
               top_n: int) -> RenderedMessage | None:
        from src.snapshots.renderer import render_daily_volume_report

        rep = render_daily_volume_report(
            rows, windows_days=windows_days, now_bucket=now_bucket,
            tz_offset_hours=tz_offset_hours, top_n=top_n,
        )
        if rep is None:
            return None
        body = rep.title + "\n" + "\n".join(rep.market_lines)
        if rep.footer:
            body += "\n" + rep.footer
        return RenderedMessage(body=body)
