"""DataApiAdapter（设计 v3.5 §4.2，T04）。

与旧 data_api_client 的行为差异只在依赖方向：
- 构造注入 CursorStore（namespace=data_api），不再持有 storage；
- _parse_items 上移到 framework/parsing；
- 端点固定 /v2/trades（v1 /activity 已 410 Gone，fc815af）。
sources 包不 import store 实现类，只依赖 models 协议。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from framework.models.ports import CursorStore
from framework.parsing.data_parsing import parse_data_trades
from src.core.models import StandardEvent

logger = logging.getLogger(__name__)


class WalletsProvider(Protocol):
    def active_addresses(self) -> list[str]: ...


def _extract_items(payload: Any) -> tuple[list[Any], str | None]:
    """逐字平移旧 client。"""
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        cursor = payload.get("next_cursor") or payload.get("cursor")
        for key in ("data", "results", "items", "activity", "trades"):
            if isinstance(payload.get(key), list):
                return payload[key], cursor
    return [], None


class DataApiAdapter:
    namespace = "data_api"

    def __init__(
        self,
        base_url: str,
        cursor_store: CursorStore,
        wallets: WalletsProvider,
        *,
        timeout_seconds: int = 10,
        page_limit: int = 100,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v2/trades"
        self._states = cursor_store
        self._wallets = wallets
        self._timeout = timeout_seconds
        self._page_limit = page_limit

    async def fetch_trades(
        self, address: str, cursor: str | None
    ) -> tuple[list[Any], str | None]:
        params: dict[str, Any] = {"user": address, "limit": self._page_limit}
        if cursor:
            params["cursor"] = cursor
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(self._url, params=params)
            resp.raise_for_status()
            return _extract_items(resp.json())

    async def cold_start(self, addresses: list[str]) -> None:
        failures: list[str] = []
        for address in addresses:
            try:
                items, _ = await self.fetch_trades(address, None)
                events = parse_data_trades(items)
            except Exception as exc:  # 记录后由外层重试
                failures.append(address)
                logger.warning("冷启动 %s 失败: %s", address, exc)
                continue
            latest = self._latest_time(events)
            if latest is not None:
                await self._states.set(
                    self.namespace, f"{address}:last_time", latest.isoformat()
                )
        if failures:
            raise RuntimeError(f"冷启动 {len(failures)} 个地址失败，需重试")
        logger.info("冷启动对账基线已建立（%d 个地址）", len(addresses))

    async def reconcile_wallet(self, address: str) -> list[StandardEvent]:
        items, cursor = await self.fetch_trades(address, None)
        events = parse_data_trades(items)

        last_raw = await self._states.get(self.namespace, f"{address}:last_time")
        last_time = datetime.fromisoformat(last_raw) if last_raw else None

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
            await self._states.set(
                self.namespace, f"{address}:last_time", latest.isoformat()
            )
        return new_events

    async def mark_last_run(self) -> None:
        await self._states.set(
            self.namespace,
            "l2_last_run",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    @staticmethod
    def _latest_time(events: list[StandardEvent]) -> datetime | None:
        times = [e.event_time for e in events if e.event_time is not None]
        return max(times) if times else None
