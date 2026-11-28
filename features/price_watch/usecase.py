"""price_watch 价格突破告警用例。

一轮串行检查配置清单：报价 → 首轮建基线 → 穿越判定 → 冷却 / 指纹去重 →
渲染 → 路由推送。单标的失败隔离，不拖垮整轮。

feature 层只依赖 framework/ 端口与模型；配置以 `Any` 注入，不反向依赖 settings 类型。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import MutableMapping
from typing import Any

from framework.delivery.router import ChannelRouter
from framework.models.ports import CursorStore
from framework.models.price_watch import PriceAlertEvent
from framework.scheduler.guard import BodyHashGuard

logger = logging.getLogger(__name__)

# StateStore 命名空间（基线 / armed / 冷却）
_STATE_NS = "price_watch"
# 正文指纹命名空间
_GUARD_NS = "price_watch_msg"


class PriceWatchUsecase:
    """价位突破告警用例。"""

    def __init__(
        self,
        *,
        feed: Any,
        store: CursorStore,
        renderer: Any,
        router: ChannelRouter,
        guard: BodyHashGuard | None,
        config: Any,
        health: MutableMapping[str, Any],
    ) -> None:
        self._feed = feed
        self._store = store
        self._renderer = renderer
        self._router = router
        self._guard = guard
        self._cfg = config
        self._health = health

    @property
    def funnel(self) -> MutableMapping[str, Any]:
        return self._health

    async def run_round(self, symbols: list[Any]) -> None:
        """串行检查一轮配置标的；单标的异常隔离。"""
        for cfg in symbols:
            try:
                await self._check_one(cfg)
            except Exception as exc:  # noqa: BLE001 - 单标的异常隔离
                logger.warning(
                    "price_watch 单标的失败 %s: %s",
                    getattr(cfg, "symbol", "?"), exc,
                )

    async def _check_one(self, cfg: Any) -> None:
        quote = await self._feed.get_quote(
            cfg.symbol, cfg.category, contract=cfg.contract
        )
        if quote is None:
            fails = self._health.setdefault("failures", {})
            fails[cfg.symbol] = fails.get(cfg.symbol, 0) + 1
            self._health["fetch_failed"] = self._health.get("fetch_failed", 0) + 1
            self._health.setdefault("last_error", {})[cfg.symbol] = "fetch_none"
            return

        self._health.setdefault("last_seen_at", {})[cfg.symbol] = time.time()
        self._health.setdefault("last_price", {})[cfg.symbol] = quote.price

        base_key = f"last:{cfg.symbol}"
        base_raw = await self._store.get(_STATE_NS, base_key)
        if base_raw is None:
            # 冷启动只建基线，不产生告警。
            await self._store.set(
                _STATE_NS, base_key,
                json.dumps({"price": quote.price, "armed": None}),
            )
            return

        event_code, threshold = self._judge(cfg, quote.price)
        if event_code is None:
            # 未穿越：回到区间内，清 armed 并刷新基线。
            await self._store.set(
                _STATE_NS, base_key,
                json.dumps({"price": quote.price, "armed": None}),
            )
            return

        if await self._in_cooldown(cfg.symbol, event_code):
            return

        event = PriceAlertEvent(
            symbol=cfg.symbol,
            label=cfg.label,
            category=quote.category,
            event_code=event_code,  # type: ignore[arg-type]
            price=quote.price,
            threshold=threshold,  # type: ignore[arg-type]
            price_kind=quote.price_kind,
            source=quote.source,
            event_time=quote.fetched_at,
            liquidity_usd=quote.liquidity_usd,
        )
        msg = self._renderer.render(event)
        if msg is None:
            self._health["render_none"] = self._health.get("render_none", 0) + 1
            return

        guard_key = f"{event_code}:{cfg.symbol}"
        if self._guard is not None and await self._guard.matches(
            _GUARD_NS, guard_key, msg.body
        ):
            self._health["dedup_hit"] = self._health.get("dedup_hit", 0) + 1
            logger.info("price_watch 指纹去重命中 %s", cfg.symbol)
            return

        result = await self._router.dispatch("price_watch", msg)
        if len(result.channel_failures):
            self._health["dispatch_failed"] = (
                self._health.get("dispatch_failed", 0) + 1
            )
            logger.error(
                "price_watch %d 个通道推送失败 %s",
                len(result.channel_failures), cfg.symbol,
            )
            return

        self._health["dispatched"] = self._health.get("dispatched", 0) + 1
        self._health.setdefault("last_event", {})[cfg.symbol] = event_code
        if self._guard is not None:
            await self._guard.record(_GUARD_NS, guard_key, msg.body)
        await self._store.set(
            _STATE_NS, base_key,
            json.dumps({"price": quote.price, "armed": event_code}),
        )
        await self._store.set(
            _STATE_NS, f"cooldown:{cfg.symbol}:{event_code}", str(time.time())
        )

    @staticmethod
    def _judge(cfg: Any, price: float) -> tuple[str | None, float | None]:
        """返回 (事件码, 阈值)；未穿越返回 (None, None)。"""
        if cfg.price_above is not None and price >= cfg.price_above:
            return "break_above", cfg.price_above
        if cfg.price_below is not None and price <= cfg.price_below:
            return "break_below", cfg.price_below
        return None, None

    async def _in_cooldown(self, symbol: str, code: str) -> bool:
        raw = await self._store.get(_STATE_NS, f"cooldown:{symbol}:{code}")
        if not raw:
            return False
        try:
            last_ts = float(raw)
        except ValueError:
            return False
        return (time.time() - last_ts) <= self._cfg.cooldown_seconds

    async def close(self) -> None:
        """本用例不自管定时器；保留收口接口供装配调用，停机/测试收尾。"""
        return
