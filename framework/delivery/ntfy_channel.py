"""ntfy 通道（设计 v3.5 §7.4，按 10 §4.7 平移）。

POST {server}/{topic} 纯文本；失败抛异常由 router 捕获为 channel 级失败；
成功才落痕（保持 09 §3.6.1 不对称现状，不压平）。
"""

from __future__ import annotations

import logging

import httpx

from framework.delivery.router import DeliveryResult
from framework.delivery.targets import DeliveryTarget
from framework.models.message import RenderedMessage

logger = logging.getLogger(__name__)


class NtfyChannel:
    name = "ntfy"

    def __init__(
        self,
        server: str,
        *,
        access_token: str = "",
        title_prefix: str = "",
        priority: str = "",
        tags: str = "",
        storage=None,
        timeout: float = 15.0,
    ) -> None:
        self._server = server.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "text/plain"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if title_prefix:
            headers["X-Title"] = title_prefix
        if priority:
            headers["X-Priority"] = str(priority)
        if tags:
            headers["X-Tags"] = ",".join(t.strip() for t in tags.split(",") if t.strip())
        self._headers = headers
        self._storage = storage
        self._timeout = timeout

    def describe(self) -> str:
        return f"ntfy@{self._server}"

    async def deliver(
        self, msg: RenderedMessage, targets: list[DeliveryTarget]
    ) -> DeliveryResult:
        keys = ",".join(msg.event_keys)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=self._timeout, write=20, pool=10)
        ) as client:
            for t in targets:
                url = f"{self._server}/{t.address.strip('/')}"
                resp = await client.post(url, content=msg.body, headers=self._headers)
                resp.raise_for_status()
                if self._storage is not None:
                    await self._storage.record_notification(url, msg.body, keys, "success")
        return DeliveryResult(outcomes=())
