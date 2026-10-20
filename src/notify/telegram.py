"""Telegram 推送通道：授权 chat 列表循环发送 + 限速 + 推送记录落库。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.bot.auth import AuthManager
from src.bot.telegram import TelegramApi
from src.notify.base import Notifier
from src.storage import Storage
from src.utils.retry import with_retry

logger = logging.getLogger(__name__)


class TelegramNotifier(Notifier):
    """向授权 chat 列表推送（保留原有限速/重试/落库语义）。

    auth.chats() 为运行期白名单（可被 /start 首用户等动态扩展），
    因此每次 send 都实时读取，不在构造时固化。
    """

    def __init__(
        self,
        api: TelegramApi,
        storage: Storage,
        auth: AuthManager,
        *,
        rate_limit_seconds: float = 0.4,
    ) -> None:
        self._api = api
        self._storage = storage
        self._auth = auth
        self._rate = rate_limit_seconds

    async def send(
        self,
        text: str,
        *,
        buttons: list[dict[str, Any]] | None = None,
        event_keys: list[str] | None = None,
    ) -> None:
        reply_markup = {"inline_keyboard": buttons} if buttons else None
        keys = ",".join(event_keys or [])
        for chat_id in self._auth.chats():
            try:
                await with_retry(
                    lambda chat_id=chat_id, reply_markup=reply_markup: (
                        self._api.send_message(chat_id, text, reply_markup)
                    ),
                    attempts=3,
                    description="telegram 推送",
                )
                await self._storage.record_notification(
                    str(chat_id), text, keys, "success"
                )
            except Exception as exc:  # noqa: BLE001 - 单 chat 失败不影响其他
                logger.error("推送给 %s 失败: %s", chat_id, exc)
                await self._storage.record_notification(
                    str(chat_id), text, keys, "failed"
                )
            await asyncio.sleep(self._rate)

    def describe(self) -> str:
        return f"telegram@{len(self._auth.chats())} chats"
