"""SnapshotStore（设计 v3.5 §3.2/§3.3，T02/T03）。

windows_sum 的 SQL 逐字平移旧 volume_by_window；cid 为新增能力。
禁止在 store 内用 time.time()，窗口由调用方传入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.models.snapshot import WindowSum

if TYPE_CHECKING:
    from src.storage import Storage


class SnapshotStore:
    def __init__(self, storage: "Storage") -> None:
        self._storage = storage

    async def windows_sum(
        self, *, start_ts: int, end_ts: int, cid: str | None = None
    ) -> list[WindowSum]:
        sql = [
            "SELECT condition_id, MAX(slug) AS slug, MAX(slug_id) AS slug_id,",
            "SUM(CASE WHEN volume_shares >= 0 THEN volume_shares ELSE 0 END) AS shares,",
            "COUNT(*) AS buckets",
            "FROM market_snapshots WHERE bucket_ts >= ? AND bucket_ts < ?",
        ]
        params: list[Any] = [start_ts, end_ts]
        if cid is not None:
            sql.append("AND condition_id = ?")
            params.append(cid.lower())
        sql.append("GROUP BY condition_id")
        rows = await self._storage._fetchall(" ".join(sql), tuple(params))
        return [
            WindowSum(
                condition_id=row["condition_id"],
                slug=row["slug"] or "",
                slug_id=row["slug_id"] or "",
                shares=row["shares"] or 0.0,
                buckets=row["buckets"] or 0,
            )
            for row in rows
        ]
