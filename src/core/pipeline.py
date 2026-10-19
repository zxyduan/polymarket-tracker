"""处理流水线：去重 → 过滤 → 聚合 → 富化 → 渲染。"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

from src.core.models import StandardEvent
from src.core.renderer import AlertMessage, render_alert
from src.sources.gamma_client import GammaClient
from src.storage import Storage

logger = logging.getLogger(__name__)


class Aggregator:
    """按 tx_hash 聚合同一笔结算内的多笔成交，窗口到期后统一回调。"""

    def __init__(
        self,
        window_seconds: float,
        on_flush: Any,
    ) -> None:
        self.window = window_seconds
        self.on_flush = on_flush
        self._groups: dict[str, list[StandardEvent]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}

    async def add(self, event: StandardEvent) -> None:
        key = event.tx_hash or event.dedupe_key
        is_new = key not in self._groups
        self._groups.setdefault(key, []).append(event)
        if is_new:
            old = self._timers.pop(key, None)
            if old:
                old.cancel()
            self._timers[key] = asyncio.create_task(self._schedule(key))

    async def _schedule(self, key: str) -> None:
        try:
            await asyncio.sleep(self.window)
            await self.flush(key)
        except asyncio.CancelledError:
            pass

    async def flush(self, key: str) -> None:
        events = self._groups.pop(key, None)
        timer = self._timers.pop(key, None)
        if timer:
            timer.cancel()
        if events:
            events.sort(
                key=lambda e: (e.log_index is None, e.log_index or 0)
            )
            await self.on_flush(events)

    async def flush_all(self) -> None:
        for key in list(self._groups):
            await self.flush(key)


async def load_labels(storage: Storage) -> dict[str, str]:
    """地址 → 名称映射（含 proxy 地址）。"""
    wallets = await storage.list_wallets(active_only=False)
    result: dict[str, str] = {}
    for wallet in wallets:
        label = wallet.get("label") or ""
        result[wallet["address"]] = label
        if wallet.get("proxy_address"):
            result[wallet["proxy_address"].lower()] = label
    return result


class PipelineRunner:
    """主处理循环，持有聚合器以便停机时 flush。"""

    def __init__(
        self,
        in_queue: asyncio.Queue[StandardEvent],
        out_queue: asyncio.Queue[AlertMessage],
        storage: Storage,
        gamma: GammaClient,
        settings: Any,
    ) -> None:
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.storage = storage
        self.gamma = gamma
        self.settings = settings
        self.aggregator: Aggregator | None = None

    async def run(self) -> None:
        cfg = self.settings.section("filters")
        allowed_types = {
            str(t).upper() for t in cfg.get("event_types", ["TRADE"])
        }
        min_notional = Decimal(str(cfg.get("min_notional_usd", 0)))
        window = float(cfg.get("aggregate_window_seconds", 3))
        tz_offset = int(self.settings.get("display.timezone_offset_hours", 8))
        polygonscan = self.settings.get(
            "endpoints.polygonscan", "https://polygonscan.com"
        )

        label_map = await load_labels(self.storage)

        async def on_flush(events: list[StandardEvent]) -> None:
            for event in events:
                if not event.title and event.condition_id:
                    question, slug = await self.gamma.get_market(
                        self.storage, event.condition_id
                    )
                    if question:
                        event.title = question
                    if slug and not event.slug:
                        event.slug = slug
            label = label_map.get(events[0].wallet, "")
            alert = render_alert(
                events,
                label=label,
                tz_offset_hours=tz_offset,
                polygonscan_base=polygonscan,
            )
            await self.out_queue.put(alert)

        self.aggregator = Aggregator(window, on_flush)

        while True:
            event = await self.in_queue.get()
            try:
                inserted = await self.storage.insert_event(event)
                if not inserted:
                    continue
                if event.event_type not in allowed_types:
                    continue
                if (
                    event.event_type == "TRADE"
                    and event.notional is not None
                    and event.notional < min_notional
                ):
                    continue
                if event.wallet not in label_map:
                    label_map.update(await load_labels(self.storage))
                await self.aggregator.add(event)
            finally:
                self.in_queue.task_done()

    async def shutdown(self) -> None:
        """停机时把尚未到期的聚合组立即产出。"""
        if self.aggregator is not None:
            await self.aggregator.flush_all()
