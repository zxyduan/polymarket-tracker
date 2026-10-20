"""ntfy 推送通道：REST 发布到 topic（支持自建服务器 / 私有 topic 鉴权）。

ntfy 是单向推送服务（订阅端只能接收，无指令交互），无需 Bot token，
只需一个 topic 名（类似频道名）。消息体为纯文本，可选标题/优先级/标签。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.notify.base import Notifier
from src.storage import Storage

logger = logging.getLogger(__name__)


class NtfyNotifier(Notifier):
    """POST {server}/{topic} 发布文本消息。

    server 默认 https://ntfy.sh（公共服务器），可换自建地址；
    access_token 用于私有 topic / 自建服务器鉴权（可选）。
    """

    def __init__(
        self,
        server: str,
        topic: str,
        *,
        access_token: str = "",
        title_prefix: str = "",
        priority: str = "",
        tags: str = "",
        storage: Storage | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._url = f"{server.rstrip('/')}/{topic.strip().lstrip('/')}"
        headers: dict[str, str] = {"Content-Type": "text/plain"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        if title_prefix:
            headers["X-Title"] = title_prefix
        if priority:
            headers["X-Priority"] = str(priority)
        if tags:
            headers["X-Tags"] = ",".join(
                t.strip() for t in tags.split(",") if t.strip()
            )
        self._headers = headers
        self._storage = storage
        self._timeout = timeout

    async def send(
        self,
        text: str,
        *,
        buttons: list[dict[str, Any]] | None = None,
        event_keys: list[str] | None = None,
    ) -> None:
        # ntfy 为单向推送，不支持交互按钮；失败抛异常由 sender 统一降级
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=self._timeout, write=20, pool=10)
        ) as client:
            resp = await client.post(self._url, content=text, headers=self._headers)
            resp.raise_for_status()
        if self._storage is not None:
            await self._storage.record_notification(
                self._url, text, ",".join(event_keys or []), "success"
            )

    def describe(self) -> str:
        return f"ntfy@{self._url}"
