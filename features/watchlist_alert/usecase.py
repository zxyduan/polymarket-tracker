"""白名单市场告警用例（T5）。

- `RewardWatchUsecase`：一轮 CLOB 奖励 diff，确认状态机 + 冷却 + 指纹去重后推送；
- `TradeAlertUsecase`：清单成交入同市场合并桶，窗口 / 条数上限触发摘要推送。

feature 层只依赖 framework/ 端口与模型；配置以 `Any` 注入，不反向依赖 settings 类型。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import MutableMapping
from typing import Any

from framework.delivery.router import ChannelRouter, DeliveryResult
from framework.models.event import StandardEvent
from framework.models.ports import CursorStore
from framework.models.watchlist_alert import MarketTradeAlert, RewardChangeEvent
from framework.parsing.watchlist_parsing import (
    ParsedReward,
    diff_rewards,
    parse_gamma_reward,
)
from framework.scheduler.guard import BodyHashGuard

logger = logging.getLogger(__name__)

# StateStore 命名空间（基线 / 冷却 / 确认计数）
_STATE_NS = "watchlist_alert"
# 正文指纹命名空间
_GUARD_NS = "watchlist_market"

# 需要连续确认轮数才推送的事件码
_CONFIRM_CODES = ("exit", "floor_break")


class RewardWatchUsecase:
    """M1 奖励变化轮询用例。"""

    def __init__(
        self,
        *,
        watcher: Any,
        store: CursorStore,
        renderer: Any,
        router: ChannelRouter,
        guard: BodyHashGuard | None,
        config: Any,
        health: MutableMapping[str, Any],
        gamma: Any,
        storage: Any,
        thresholds: Any,
    ) -> None:
        self._watcher = watcher
        self._store = store
        self._renderer = renderer
        self._router = router
        self._guard = guard
        self._cfg = config
        self._health = health
        self._gamma = gamma
        self._storage = storage
        self._thresholds = thresholds

    @property
    def funnel(self) -> MutableMapping[str, Any]:
        return self._health

    async def run_round(self, cids: set[str]) -> None:
        """串行轮询一轮清单市场；单市场失败隔离，不拖垮整轮。"""
        for cid in cids:
            try:
                await self._watch_one(cid)
            except Exception as exc:  # noqa: BLE001 - 单市场异常隔离
                logger.warning("奖励轮询单市场失败 %s: %s", cid, exc)

    async def _watch_one(self, cid: str) -> None:
        th = self._thresholds
        raw = await self._watcher.get_reward(cid)
        if raw is None:
            # 本轮请求失败 / 超时隔离：跳过该市场本轮 diff，下一轮再判，不据此报退出。
            self._health["watch_fetch_failed"] = (
                self._health.get("watch_fetch_failed", 0) + 1
            )
            return
        cur = parse_gamma_reward(
            raw,
            low_min=th.reward_tier_low_min,
            mid_min=th.reward_tier_mid_min,
            high_min=th.reward_tier_high_min,
        )
        # cur=None（无有效奖励段）参与 diff，由 diff_rewards 判 exit / floor_break。

        base_key = f"reward_base:{cid}"
        base_raw = await self._store.get(_STATE_NS, base_key)
        prev = _load_base(base_raw) if base_raw is not None else None
        if base_raw is None or _is_empty_reward(prev):
            # 冷启动（含历史空基线污染）只建基线，不产生告警。
            await self._store.set(_STATE_NS, base_key, json.dumps(_base_dump(cur)))
            return

        events = diff_rewards(
            prev, cur, cid=cid,
            min_daily_rate=th.reward_min_daily_rate,
            decay_pct_threshold=self._cfg.decay_pct_threshold,
        )

        confirmed: list[RewardChangeEvent] = []
        for ev in events:
            if ev.event_code == "rebound":
                logger.info("watchlist 奖励恢复 cid=%s", cid)
                await self._clear_cooldown(cid, ev.event_code)
                continue
            if not self._alert_on(ev.event_code):
                continue
            if ev.event_code in _CONFIRM_CODES:
                count = await self._bump_confirm(cid, ev.event_code)
                if count < self._cfg.confirm_rounds:
                    continue
            if await self._in_cooldown(cid, ev.event_code):
                continue
            confirmed.append(ev)

        cur_online = (
            cur is not None
            and cur.rate is not None
            and cur.rate >= th.reward_min_daily_rate
        )
        if not confirmed:
            # 恢复在线则清零未达轮数的确认计数。
            if cur_online:
                for ev in events:
                    await self._clear_confirm(cid, ev.event_code)
            return

        question, slug = await self._enrich(cid)
        confirmed = [
            dataclasses.replace(ev, question=question or ev.question, slug=slug or ev.slug)
            for ev in confirmed
        ]

        msg = self._renderer.render_batch(confirmed, question=question, slug=slug)
        if msg is None:
            self._health["render_none"] = self._health.get("render_none", 0) + 1
            return

        guard_key = f"reward:{confirmed[0].event_code}:{cid}"
        if self._guard is not None and await self._guard.matches(
            _GUARD_NS, guard_key, msg.body
        ):
            self._health["dedup_hit"] = self._health.get("dedup_hit", 0) + 1
            logger.info("watchlist M1 指纹去重命中 cid=%s", cid)
            return

        result = await self._router.dispatch("watchlist_market", msg)
        failures = len(result.channel_failures)
        if failures:
            self._health["dispatch_failed"] = self._health.get("dispatch_failed", 0) + 1
            logger.error("watchlist M1 %d 个通道推送失败 cid=%s", failures, cid)
            return

        self._health["dispatched"] = self._health.get("dispatched", 0) + 1
        # 仅全部通道无失败才写指纹 / 推进基线 / 冷却 / 确认计数。
        if self._guard is not None:
            await self._guard.record(_GUARD_NS, guard_key, msg.body)
        await self._store.set(_STATE_NS, base_key, json.dumps(_base_dump(cur)))
        now_ts = str(time.time())
        for ev in confirmed:
            await self._store.set(
                _STATE_NS, f"alert:{cid}:{ev.event_code}", now_ts
            )
            await self._clear_confirm(cid, ev.event_code)

    def _alert_on(self, code: str) -> bool:
        cfg = self._cfg
        return {
            "exit": cfg.alert_on_exit,
            "floor_break": cfg.alert_on_floor_break,
            "decay_pct": cfg.alert_on_decay_pct,
            "tier_drop": cfg.alert_on_tier_drop,
            "rebound": cfg.alert_on_rebound,
        }.get(code, False)

    async def _enrich(self, cid: str) -> tuple[str, str]:
        if self._gamma is None:
            return "", ""
        question, slug = await self._gamma.get_market(self._storage, cid)
        return question or "", slug or ""

    async def _bump_confirm(self, cid: str, code: str) -> int:
        key = f"confirm:{cid}:{code}"
        raw = await self._store.get(_STATE_NS, key)
        try:
            count = int(raw or "0") + 1
        except ValueError:
            count = 1
        await self._store.set(_STATE_NS, key, str(count))
        return count

    async def _clear_confirm(self, cid: str, code: str) -> None:
        await self._store.set(_STATE_NS, f"confirm:{cid}:{code}", "0")

    async def _in_cooldown(self, cid: str, code: str) -> bool:
        raw = await self._store.get(_STATE_NS, f"alert:{cid}:{code}")
        if not raw:
            return False
        try:
            last_ts = float(raw)
        except ValueError:
            return False
        return (time.time() - last_ts) <= self._cfg.cooldown_seconds

    async def _clear_cooldown(self, cid: str, code: str) -> None:
        await self._store.set(_STATE_NS, f"alert:{cid}:{code}", "0")


class TradeAlertUsecase:
    """M2 清单市场成交推送用例（同市场分桶合并）。"""

    def __init__(
        self,
        *,
        renderer: Any,
        router: ChannelRouter,
        guard: BodyHashGuard | None,
        config: Any,
        health: MutableMapping[str, Any],
        gamma: Any,
        storage: Any,
    ) -> None:
        self._renderer = renderer
        self._router = router
        self._guard = guard
        self._cfg = config
        self._health = health
        self._gamma = gamma
        self._storage = storage
        self._buckets: dict[str, list[MarketTradeAlert]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}

    @property
    def funnel(self) -> MutableMapping[str, Any]:
        return self._health

    async def handle(self, event: StandardEvent) -> DeliveryResult:
        for key in ("render_none", "dedup_hit", "dispatched", "dispatch_failed",
                    "cross_link_hit", "bucket_flushed"):
            self._health.setdefault(key, 0)

        # 跨链路去重：已成功推过的同 key 成交不再重复抑制。
        if self._cfg.cross_link_dedup and await self._storage.notification_event_key_exists(
            event.dedupe_key
        ):
            self._health["cross_link_hit"] += 1
            logger.info("watchlist M2 跨链路去重命中 key=%s", event.dedupe_key)
            return DeliveryResult(outcomes=())

        title = event.title or ""
        slug = event.slug or ""
        if self._gamma is not None and (not title or not slug) and event.condition_id:
            question, cached_slug = await self._gamma.get_market(
                self._storage, event.condition_id
            )
            title = title or question or ""
            slug = slug or cached_slug or ""

        alert = MarketTradeAlert(event=event, title=title, slug=slug)
        cid = (event.condition_id or "").strip().lower() or event.dedupe_key
        await self._add_to_bucket(cid, alert)
        return DeliveryResult(outcomes=())

    async def _add_to_bucket(self, cid: str, alert: MarketTradeAlert) -> None:
        bucket = self._buckets.get(cid)
        if bucket is None:
            bucket = []
            self._buckets[cid] = bucket
            self._timers[cid] = asyncio.create_task(self._wait_window(cid))
        bucket.append(alert)
        if len(bucket) >= self._cfg.max_events_per_message:
            await self._flush(cid, reason="threshold")

    async def _wait_window(self, cid: str) -> None:
        try:
            await asyncio.sleep(self._cfg.merge_window_seconds)
        except asyncio.CancelledError:
            return
        await self._flush(cid, reason="window", trigger=asyncio.current_task())

    async def _flush(
        self,
        cid: str,
        *,
        reason: str,
        trigger: asyncio.Task[None] | None = None,
    ) -> None:
        # 先 pop 桶再渲染：定时器与满笔两路不会重复发。
        bucket = self._buckets.pop(cid, None)
        timer = self._timers.pop(cid, None)
        # 非自身回调触发（满笔 / 停机补发）时，取消待触发的窗口定时器；
        # 自身定时器回调则保留，交由协程自然返回，避免 cancel 自己。
        if timer is not None and timer is not trigger:
            timer.cancel()
        if not bucket:
            return

        msg = self._renderer.render(bucket, reason=reason)
        if msg is None:
            self._health["render_none"] += 1
            return

        first_key = bucket[0].event.dedupe_key
        if self._guard is not None and await self._guard.matches(
            _GUARD_NS, f"trade:{first_key}", msg.body
        ):
            self._health["dedup_hit"] += 1
            logger.info("watchlist M2 指纹去重命中 cid=%s", cid)
            return

        result = await self._router.dispatch("watchlist_market", msg)
        failures = len(result.channel_failures)
        if failures:
            self._health["dispatch_failed"] += 1
            logger.error("watchlist M2 %d 个通道推送失败 cid=%s", failures, cid)
            return

        self._health["dispatched"] += 1
        self._health["bucket_flushed"] += 1
        if self._guard is not None:
            await self._guard.record(_GUARD_NS, f"trade:{first_key}", msg.body)

    async def flush_all(self) -> None:
        """停机：把未到期桶尽量补发一次，未发事件不静默丢弃。"""
        for cid in list(self._buckets):
            await self._flush(cid, reason="shutdown")
        await self.close()

    async def close(self) -> None:
        """摘除并取消全部残留定时器并等待其退出（停机/测试收尾调用）。"""
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)


def _base_dump(reward: ParsedReward | None) -> dict[str, Any]:
    if reward is None:
        return {"rate": None, "asset": "", "tier": None, "date": ""}
    return {
        "rate": reward.rate,
        "asset": reward.asset,
        "tier": reward.tier,
        "date": reward.end_date,
    }


def _load_base(base_raw: str) -> ParsedReward:
    try:
        data = json.loads(base_raw)
    except (json.JSONDecodeError, ValueError):
        return ParsedReward(rate=None, asset="", start_date="", end_date="", tier=None)
    return ParsedReward(
        rate=data.get("rate"),
        asset=str(data.get("asset") or ""),
        start_date="",
        end_date=str(data.get("date") or ""),
        tier=data.get("tier"),
    )


def _is_empty_reward(reward: ParsedReward | None) -> bool:
    """基线是否为全空（冷启动 / 历史空基线污染），可安全重建。"""
    return reward is None or (
        reward.rate is None
        and not reward.asset
        and reward.tier is None
        and not reward.end_date
    )
