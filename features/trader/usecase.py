"""F1 交易员告警用例（§6）。

事件驱动：消费 AlertMessage 队列 → 渲染 → router 分发。
当前 P1 只建壳，旧 run_sender 仍在用；后续替换 queue consumer。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, MutableMapping

from framework.delivery.router import ChannelRouter
from framework.scheduler.guard import FireOnceGuard
from features.trader.ports import AlertRenderer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraderConfig:
    dedup: bool = True


class TraderAlertUsecase:
    def __init__(self, *, renderer: AlertRenderer, router: ChannelRouter,
                 guard: FireOnceGuard, config: TraderConfig,
                 health: MutableMapping[str, Any]) -> None:
        self._renderer = renderer
        self._router = router
        self._guard = guard
        self._cfg = config
        self._health = health

    async def handle(self, event: Any) -> None:
        msg = self._renderer.render(event)
        if msg is None:
            logger.warning("trader: render 返回 None，跳过")
            return
        result = await self._router.dispatch("trader", msg)
        self._health["trader_failures"] = len(result.channel_failures)
        if result.channel_failures:
            logger.error("trader: %d 通道推送失败", len(result.channel_failures))
