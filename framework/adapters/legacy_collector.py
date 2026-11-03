"""把现有 src/ 组件适配成 framework/feature 端口（双轨桥接）。

只做薄委托，不改旧组件行为；P1_SWITCH 打开后由 usecase 调用。
"""

from __future__ import annotations

from typing import Any, Mapping


class LegacyCollectorAdapter:
    """SnapshotCollector → SnapshotCollectorPort 薄壳。"""

    def __init__(self, collector: Any) -> None:
        self._inner = collector

    async def resolve_market(self, slug: str) -> tuple[str, str] | None:
        return await self._inner.resolve_market(slug)

    async def backfill_prev_bucket(self, condition_id: str) -> None:
        await self._inner.backfill_prev_bucket(condition_id)

    async def fetch_live_prices(self, rows: list[Any]) -> Mapping[str, float]:
        return await self._inner.fetch_live_prices(rows)
