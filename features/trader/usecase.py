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

    @property
    def funnel(self) -> MutableMapping[str, Any]:
        """暴露分发漏斗计数，供健康监控读取。"""
        return self._health

    async def handle(self, event: Any) -> DeliveryResult:
        for key in ("render_none", "dedup_hit", "dispatched", "dispatch_failed"):
            self._health.setdefault(key, 0)

        msg = self._renderer.render(event)
        if msg is None:
            self._health["render_none"] += 1
            logger.warning("trader: render 返回 None，跳过")
            return DeliveryResult(outcomes=())

        # body_hash 去重：同一正文已成功推过则跳过
        # 作用：防 RTDS + Data API 双源推送同一笔交易产生两条相同告警
        dedup_key = f"alert:{getattr(event, 'dedupe_key', '') or getattr(event, 'title', '')}"
        dedup_enabled = bool(self._cfg and self._cfg.dedup)
        if dedup_enabled and self._hash is not None:
            if await self._hash.matches("trader", dedup_key, msg.body):
                self._health["dedup_hit"] += 1
                logger.info("trader: body_hash 去重命中，跳过重复推送 key=%s", dedup_key)
                return DeliveryResult(outcomes=())

        result = await self._router.dispatch("trader", msg)
        failures = len(result.channel_failures)
        self._health["trader_failures"] = failures
        if failures:
            self._health["dispatch_failed"] += 1
            logger.error("trader: %d 通道推送失败", failures)
        else:
            self._health["dispatched"] += 1

        # 仅全部通道无失败时才写指纹
        if dedup_enabled and self._hash is not None and failures == 0:
            await self._hash.record("trader", dedup_key, msg.body)

        return result
