"""Telegram 通道（设计 v3.5 §7.4，按 10 §4.7 平移）。

逐 chat 投递：target 级失败只落痕、不抛（error_kind="target"）；
限速/重试/落库语义与 src/notify/telegram.py 逐字一致。
"""

from __future__ import annotations

import asyncio
import logging

from framework.delivery.router import Channel, DeliveryResult, TargetOutcome
from framework.delivery.targets import DeliveryTarget
from framework.models.message import RenderedMessage

logger = logging.getLogger(__name__)


class TelegramChannel:
    name = "telegram"

    def __init__(self, api, storage, auth, *, rate_limit_seconds: float = 0.4) -> None:
        self._api = api
        self._storage = storage
        self._auth = auth
        self._rate = rate_limit_seconds

    def describe(self) -> str:
        return f"telegram@{len(self._auth.chats())} chats"

    async def deliver(
        self, msg: RenderedMessage, targets: list[DeliveryTarget]
    ) -> DeliveryResult:
        reply_markup = {"inline_keyboard": list(msg.buttons)} if msg.buttons else None
        keys = ",".join(msg.event_keys)
        outcomes: list[TargetOutcome] = []
        for t in targets:
            chat_id = t.address
            try:
                await self._api.send_message(chat_id, msg.body, reply_markup)
                await self._storage.record_notification(chat_id, msg.body, keys, "success")
                outcomes.append(TargetOutcome(target=t, status="success"))
            except Exception as exc:  # 单 chat 失败不影响其他
                logger.error("推送给 %s 失败: %s", chat_id, exc)
                await self._storage.record_notification(chat_id, msg.body, keys, "failed")
                outcomes.append(TargetOutcome(target=t, status="failed", error=str(exc),
                                              error_kind="target"))
            await asyncio.sleep(self._rate)
        return DeliveryResult(outcomes=tuple(outcomes))
