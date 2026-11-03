"""rtds 适配器（设计 v3.5 §4.2，按 10 §4.2 平移）。

WebSocket 重连/心跳/退避/本地过滤语义逐字保留（红线）；
本类为薄壳，后续 L2 路由补 RawRecord.kind=EVENT。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from src.core.models import StandardEvent
from src.sources.rtds_client import RtdsClient

if TYPE_CHECKING:
    from src.storage import Storage


class RtdsAdapter:
    def __init__(self, url: str, *, volume_tracker: Any | None = None, **kwargs: Any) -> None:
        self._inner = RtdsClient(url, volume_tracker=volume_tracker, **kwargs)

    @property
    def connected(self) -> bool:
        return self._inner.connected

    async def run(
        self,
        event_queue: "asyncio.Queue[StandardEvent]",
        storage: "Storage",
        reconcile_event: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> None:
        await self._inner.run(event_queue, storage, reconcile_event, stop_event)
