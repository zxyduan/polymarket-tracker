"""F2 feature 级端口（设计 v3.5 §5.2，R-09/D16-1）。

collector 内部可组合 gamma/clob 适配器与 baseline store 端口；
用例层只经这两个 Protocol 调用，不感知实现，不 import Storage。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from framework.models.message import RenderedMessage


class SnapshotCollectorPort(Protocol):
    async def resolve_market(self, slug: str) -> tuple[str, str] | None: ...

    async def backfill_prev_bucket(self, condition_id: str) -> None: ...

    async def fetch_live_prices(self, rows: list[Any]) -> Mapping[str, float]: ...


class SnapshotRenderer(Protocol):
    def render(
        self,
        rows: list[Any],
        *,
        live_prices: Mapping[str, float],
        now_bucket: int,
    ) -> RenderedMessage | None: ...
