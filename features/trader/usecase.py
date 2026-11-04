"""F1 交易员告警用例（§6）。

事件驱动：消费 AlertMessage 队列 → 渲染 → body_hash 去重 → router 分发。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, MutableMapping

from framework.delivery.router import ChannelRouter, DeliveryResult
from framework.scheduler.guard import BodyHashGuard
from features.trader.ports import AlertRenderer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraderConfig:
    dedup: bool = True


class TraderAlertUsecase:
    def __init__(self, *, renderer: AlertRenderer, router: ChannelRouter,
                 guard: BodyHashGuard | None, config: TraderConfig,
                 health: MutableMapping[str, Any]) -> None:
        self._renderer = renderer
        self._router = router
        self._hash = guard
        self._cfg = config
        self._health = health

    async def handle(self, event: Any) -> DeliveryResult:
        msg = self._renderer.render(event)
        if msg is None:
            logger.warning("trader: render 返回 None，跳过")
            return DeliveryResult(outcomes=())

        # body_hash 去重：同一正文已成功推过则跳过
        # 作用：防 RTDS + Data API 双源推送同一笔交易产生两条相同告警
        dedup_key = f"alert:{getattr(event, 'dedupe_key', '') or getattr(event, 'title', '')}"
        if self._cfg.dedup and self._hash is not None:
            if await self._hash.matches("trader", dedup_key, msg.body):
                logger.debug("trader: body_hash 命中，跳过重复推送 key=%s", dedup_key)
                return DeliveryResult(outcomes=())

        result = await self._router.dispatch("trader", msg)
        self._health["trader_failures"] = len(result.channel_failures)
        if result.channel_failures:
            logger.error("trader: %d 通道推送失败", len(result.channel_failures))

        # 仅全部通道无失败时才写指纹
        if self._cfg.dedup and self._hash is not None and not result.channel_failures:
            await self._hash.record("trader", dedup_key, msg.body)

        return result
