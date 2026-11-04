"""把现有 Notifier 适配成 framework Channel（双轨切换用）。"""

from __future__ import annotations

import logging
from typing import Any

from framework.delivery.router import ChannelFailure, DeliveryResult
from framework.models.message import RenderedMessage

logger = logging.getLogger(__name__)


class NotifierChannel:
    name = "legacy_notifier"

    def __init__(self, notifier: Any) -> None:
        self._n = notifier

    def describe(self) -> str:
        return getattr(self._n, "describe", lambda: "legacy")()

    async def deliver(self, msg: RenderedMessage, targets: list) -> DeliveryResult:
        try:
            await self._n.send(
                msg.body,
                buttons=list(msg.buttons),
                event_keys=list(msg.event_keys),
            )
            return DeliveryResult(outcomes=())
        except Exception as exc:  # noqa: BLE001
            logger.error("推送失败(%s): %s", self.describe(), exc)
            return DeliveryResult(
                outcomes=(),
                channel_failures=[ChannelFailure(self.name, exc, self.describe())],
            )
