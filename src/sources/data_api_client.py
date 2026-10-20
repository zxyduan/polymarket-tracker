"""Data API 对账客户端（L2）。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from src.core.models import StandardEvent
from src.core.parsing import parse_data_activity

logger = logging.getLogger(__name__)


def _extract_items(payload: Any) -> tuple[list[Any], str | None]:
    """从响应中提取条目列表与下一页游标。"""
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        cursor = payload.get("next_cursor") or payload.get("cursor")
        for key in ("data", "results", "items", "activity", "trades"):
            if isinstance(payload.get(key), list):
                return payload[key], cursor
    return [], None


class DataApiClient:
    """周期性拉取 Data API 做对账兜底。"""

    def __init__(
        self,
        base_url: str,
        *,
        interval_seconds: int = 60,
        timeout_seconds: int = 10,
        max_concurrency: int = 5,
        page_limit: int = 100,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.interval = interval_seconds
        self.timeout = timeout_seconds
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.page_limit = page_limit

    async def cold_start(self, wallets: list[dict[str, Any]], storage: Any) -> None:
        """冷启动：仅记录每个地址的最新事件时间，不推送。任一失败则整体抛出由 main 重试。"""
        failures: list[str] = []
        for wallet in wallets:
            address = wallet["address"]
            try:
                items, _ = await self._fetch_page(address, cursor=None)
                events = self._parse_items(items)
            except Exception as exc:  # noqa: BLE001 - 记录后统一重试
                failures.append(address)
                logger.warning("冷启动 %s 失败: %s", address, exc)
                continue
            latest = self._latest_time(events)
            if latest is not None:
                await storage.set_state(
                    f"data_api:{address}:last_time", latest.isoformat()
                )
        if failures:
            raise RuntimeError(
                f"冷启动 {len(failures)} 个地址失败（网络/限流），需重试"
            )
        logger.info("冷启动对账基线已建立（%d 个地址）", len(wallets))

    async def run(
        self,
        event_queue: asyncio.Queue[StandardEvent],
        storage: Any,
        reconcile_event: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> None:
        """周期对账；reconcile_event 被置位时立即执行。"""
        while not stop_event.is_set():
            reconcile_event.clear()
            try:
                await self._reconcile_once(event_queue, storage)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单轮失败不影响下一轮
                logger.warning("对账轮次异常: %s", exc)

            try:
                await asyncio.wait_for(
                    reconcile_event.wait(), timeout=self.interval
                )
            except asyncio.TimeoutError:
                pass

    async def _reconcile_once(
        self,
        event_queue: asyncio.Queue[StandardEvent],
        storage: Any,
    ) -> None:
        wallets = await storage.list_wallets(active_only=True)
        if not wallets:
            return
        results = await asyncio.gather(
            *[self._reconcile_wallet(wallet["address"], storage) for wallet in wallets],
            return_exceptions=True,
        )
        new_count = 0
        for result in results:
            if isinstance(result, Exception):
                logger.warning("地址对账失败: %s", result)
                continue
            for event in result:
                try:
                    event_queue.put_nowait(event)
                except asyncio.QueueFull:
                    # 队列满：等待消费者腾出空间（不丢弃交易事件），
                    # 避免抛异常导致整轮对账被跳过。
                    await event_queue.put(event)
                new_count += 1
        await storage.set_state(
            "l2_last_run",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if new_count:
            logger.info("对账补入新事件 %d 条", new_count)

    async def _reconcile_wallet(
        self, address: str, storage: Any
    ) -> list[StandardEvent]:
        async with self.semaphore:
            items, cursor = await self._fetch_page(address, None)
        events = self._parse_items(items)

        last_time_raw = await storage.get_state(f"data_api:{address}:last_time")
        last_time = datetime.fromisoformat(last_time_raw) if last_time_raw else None

        new_events: list[StandardEvent] = []
        latest = last_time
        for event in events:
            if event.event_time is not None:
                if latest is None or event.event_time > latest:
                    latest = event.event_time
                if last_time is not None and event.event_time <= last_time:
                    continue
            new_events.append(event)

        if latest is not None:
            await storage.set_state(
                f"data_api:{address}:last_time", latest.isoformat()
            )
        return new_events

    async def _fetch_page(
        self, address: str, cursor: str | None
    ) -> tuple[list[Any], str | None]:
        params: dict[str, Any] = {"user": address, "limit": self.page_limit}
        if cursor:
            params["cursor"] = cursor
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/activity", params=params)
            response.raise_for_status()
            return _extract_items(response.json())

    def _parse_items(self, items: list[Any]) -> list[StandardEvent]:
        events: list[StandardEvent] = []
        for item in items:
            events.extend(parse_data_activity(item))
        return events

    @staticmethod
    def _latest_time(events: list[StandardEvent]) -> datetime | None:
        times = [e.event_time for e in events if e.event_time is not None]
        return max(times) if times else None

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)
