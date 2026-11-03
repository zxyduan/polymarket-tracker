"""F2 快照渲染适配层（§5.2）。

把旧 render_snapshot_summary 包成 SnapshotRenderer 协议；
live_prices 注入与 shares_display 归一在这里完成。
"""

from __future__ import annotations

from typing import Any, Mapping

from framework.models.message import RenderedMessage


class LegacySnapshotRenderer:
    def __init__(self, *, tz_offset_hours: int = 8,
                 bucket_seconds: int = 7200) -> None:
        self._tz = tz_offset_hours
        self._bs = bucket_seconds

    def render(self, rows: list[Any], *, live_prices: Mapping[str, float],
               now_bucket: int) -> RenderedMessage | None:
        from src.snapshots.renderer import render_snapshot_summary

        for r in rows:
            cid = str(r.get("condition_id") or "")
            if cid in live_prices:
                r["current_price"] = live_prices[cid]
            sh = r.get("volume_shares")
            r["volume_shares_display"] = float(sh) if sh is not None else -1.0
        alert = render_snapshot_summary(
            rows, tz_offset_hours=self._tz,
            bucket_seconds=self._bs, now_bucket=now_bucket,
        )
        if alert is None:
            return None
        return RenderedMessage(body=alert.html)
