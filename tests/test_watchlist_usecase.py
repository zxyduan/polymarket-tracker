"""RewardWatchUsecase / TradeAlertUsecase 用例测试（fake 端口 + 真实 router）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from features.watchlist_alert.usecase import (
    RewardWatchUsecase,
    TradeAlertUsecase,
)
from framework.delivery.router import ChannelRouter, DeliveryResult
from framework.delivery.targets import LegacyProvider
from framework.models.event import StandardEvent
from framework.models.message import RenderedMessage
from framework.scheduler.guard import BodyHashGuard
from framework.store.state_store import StateStore
from src.storage import Storage

USDC = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"


def _raw(rate: str) -> dict[str, object]:
    return {
        "rewards_config": [
            {
                "rate_per_day": rate,
                "asset_address": USDC,
                "start_date": "2026-11-01",
                "end_date": "2026-12-01",
            }
        ]
    }


def _thresholds() -> SimpleNamespace:
    return SimpleNamespace(
        reward_min_daily_rate=30.0,
        reward_tier_low_min=30.0,
        reward_tier_mid_min=100.0,
        reward_tier_high_min=300.0,
    )


def _reward_cfg(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "enabled": True, "interval_seconds": 900,
        "alert_on_exit": True, "alert_on_floor_break": True,
        "alert_on_decay_pct": True, "decay_pct_threshold": 0.30,
        "alert_on_tier_drop": True, "alert_on_rebound": False,
        "confirm_rounds": 1, "cooldown_seconds": 86400,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _trade_cfg(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "enabled": True, "merge_window_seconds": 0.1,
        "max_events_per_message": 10, "cross_link_dedup": True,
    }
    base.update(over)
    return SimpleNamespace(**base)


class _GoodChannel:
    name = "legacy_notifier"

    def describe(self) -> str:
        return "legacy"

    async def deliver(self, msg: RenderedMessage, targets: list[Any]) -> DeliveryResult:
        return DeliveryResult(outcomes=())


class _FailChannel:
    name = "legacy_notifier"

    def describe(self) -> str:
        return "legacy-fail"

    async def deliver(self, msg: RenderedMessage, targets: list[Any]) -> DeliveryResult:
        return DeliveryResult(outcomes=(), channel_failures=["boom"])


class _Watcher:
    def __init__(self, responses: list[dict[str, object] | None]) -> None:
        self.responses = responses
        self.calls = 0

    async def get_reward(self, condition_id: str) -> dict[str, object] | None:
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]

    async def aclose(self) -> None:
        return None


class _Gamma:
    async def get_market(self, _storage: Any, cid: str) -> tuple[str, str]:
        return ("问题标题", "some-slug")


class _M1Renderer:
    def render_batch(self, events: list[Any], **kw: Any) -> RenderedMessage:
        return RenderedMessage(body="m1-body", event_keys=("m1-key",))

    def render(self, event: Any) -> RenderedMessage:
        return RenderedMessage(body="m1-body")


class _M1RendererNone:
    def render_batch(self, events: list[Any], **kw: Any) -> RenderedMessage | None:
        return None


class _M2Renderer:
    def __init__(self) -> None:
        self.batches: list[list[Any]] = []

    def render(self, alerts: list[Any], *, reason: str = "window") -> RenderedMessage | None:
        self.batches.append(alerts)
        keys = [a.event.dedupe_key for a in alerts]
        return RenderedMessage(body="m2-body", event_keys=tuple(keys))


def _event(dedupe_key: str, cid: str, **over: Any) -> StandardEvent:
    base: dict[str, Any] = {
        "source": "rtds", "wallet": "0xwallet", "event_type": "TRADE",
        "tx_hash": "0xabc123", "condition_id": cid, "side": "BUY", "outcome": "YES",
        "price": Decimal("0.5"), "size": Decimal(10), "notional": Decimal(5),
        "event_time": datetime.now(timezone.utc),
    }
    base.update(over)
    return StandardEvent(dedupe_key=dedupe_key, **base)


@pytest.fixture
async def wired_storage(tmp_path: Any) -> Storage:
    storage = Storage(str(tmp_path / "w.db"))
    await storage.init()
    return storage


# ── M1 RewardWatchUsecase ───────────────────────────────────────────────


async def test_m1_cold_start_only_builds_base(wired_storage: Storage) -> None:
    watcher = _Watcher([_raw("120")])
    guard = BodyHashGuard(StateStore(wired_storage))
    uc = RewardWatchUsecase(
        watcher=watcher, store=StateStore(wired_storage), renderer=_M1Renderer(),
        router=ChannelRouter([_GoodChannel()], LegacyProvider()), guard=guard,
        config=_reward_cfg(), health={}, gamma=_Gamma(), storage=wired_storage,
        thresholds=_thresholds(),
    )
    await uc.run_round({"c1"})
    assert uc.funnel.get("dispatched", 0) == 0


async def test_m1_exit_alert_routes_and_persists(wired_storage: Storage) -> None:
    watcher = _Watcher([_raw("120"), {"rewards_config": []}])
    guard = BodyHashGuard(StateStore(wired_storage))
    uc = RewardWatchUsecase(
        watcher=watcher, store=StateStore(wired_storage), renderer=_M1Renderer(),
        router=ChannelRouter([_GoodChannel()], LegacyProvider()), guard=guard,
        config=_reward_cfg(), health={}, gamma=_Gamma(), storage=wired_storage,
        thresholds=_thresholds(),
    )
    await uc.run_round({"c1"})          # 冷启动建基线
    await uc.run_round({"c1"})          # 第二轮退出
    assert uc.funnel["dispatched"] == 1


async def test_m1_confirm_rounds_two_rounds(wired_storage: Storage) -> None:
    watcher = _Watcher([_raw("120"), {"rewards_config": []}, {"rewards_config": []}])
    guard = BodyHashGuard(StateStore(wired_storage))
    uc = RewardWatchUsecase(
        watcher=watcher, store=StateStore(wired_storage), renderer=_M1Renderer(),
        router=ChannelRouter([_GoodChannel()], LegacyProvider()), guard=guard,
        config=_reward_cfg(confirm_rounds=2), health={}, gamma=_Gamma(),
        storage=wired_storage, thresholds=_thresholds(),
    )
    await uc.run_round({"c1"})          # 冷启动
    await uc.run_round({"c1"})          # 第 1 次确认，不推送
    assert uc.funnel.get("dispatched", 0) == 0
    await uc.run_round({"c1"})          # 第 2 次确认，推送
    assert uc.funnel["dispatched"] == 1


async def test_m1_channel_failure_does_not_persist(wired_storage: Storage) -> None:
    watcher = _Watcher([_raw("120"), {"rewards_config": []}])
    guard = BodyHashGuard(StateStore(wired_storage))
    uc = RewardWatchUsecase(
        watcher=watcher, store=StateStore(wired_storage), renderer=_M1Renderer(),
        router=ChannelRouter([_FailChannel()], LegacyProvider()), guard=guard,
        config=_reward_cfg(), health={}, gamma=_Gamma(), storage=wired_storage,
        thresholds=_thresholds(),
    )
    await uc.run_round({"c1"})
    await uc.run_round({"c1"})
    assert uc.funnel["dispatch_failed"] == 1
    assert uc.funnel.get("dispatched", 0) == 0


async def test_m1_render_none_counted(wired_storage: Storage) -> None:
    watcher = _Watcher([_raw("120"), {"rewards_config": []}])
    uc = RewardWatchUsecase(
        watcher=watcher, store=StateStore(wired_storage), renderer=_M1RendererNone(),
        router=ChannelRouter([_GoodChannel()], LegacyProvider()), guard=None,
        config=_reward_cfg(), health={}, gamma=_Gamma(), storage=wired_storage,
        thresholds=_thresholds(),
    )
    await uc.run_round({"c1"})
    await uc.run_round({"c1"})
    assert uc.funnel["render_none"] == 1


# ── M2 TradeAlertUsecase ────────────────────────────────────────────────


async def test_m2_window_flush_routes_batch(wired_storage: Storage) -> None:
    renderer = _M2Renderer()
    uc = TradeAlertUsecase(
        renderer=renderer, router=ChannelRouter([_GoodChannel()], LegacyProvider()),
        guard=BodyHashGuard(StateStore(wired_storage)), config=_trade_cfg(),
        health={}, gamma=_Gamma(), storage=wired_storage,
    )
    await uc.handle(_event("tx:1", "c1"))
    await uc.handle(_event("tx:2", "c1"))
    await asyncio.sleep(0.25)
    assert uc.funnel["bucket_flushed"] == 1
    assert len(renderer.batches) == 1
    assert len(renderer.batches[0]) == 2
    await uc.close()


async def test_m2_threshold_flush_when_max_events(wired_storage: Storage) -> None:
    renderer = _M2Renderer()
    uc = TradeAlertUsecase(
        renderer=renderer, router=ChannelRouter([_GoodChannel()], LegacyProvider()),
        guard=None, config=_trade_cfg(max_events_per_message=2),
        health={}, gamma=_Gamma(), storage=wired_storage,
    )
    await uc.handle(_event("tx:1", "c1"))
    await uc.handle(_event("tx:2", "c1"))
    assert uc.funnel["bucket_flushed"] == 1
    assert len(renderer.batches[0]) == 2
    await uc.close()


async def test_m2_cross_link_dedup_suppresses(wired_storage: Storage) -> None:
    await wired_storage.record_notification(
        "tg", "text", "tx:999", "success"
    )
    renderer = _M2Renderer()
    uc = TradeAlertUsecase(
        renderer=renderer, router=ChannelRouter([_GoodChannel()], LegacyProvider()),
        guard=None, config=_trade_cfg(), health={}, gamma=_Gamma(),
        storage=wired_storage,
    )
    await uc.handle(_event("tx:999", "c1"))
    assert uc.funnel["cross_link_hit"] == 1
    assert renderer.batches == []
    await uc.close()


async def test_m2_flush_all_drains_remaining(wired_storage: Storage) -> None:
    renderer = _M2Renderer()
    uc = TradeAlertUsecase(
        renderer=renderer, router=ChannelRouter([_GoodChannel()], LegacyProvider()),
        guard=None, config=_trade_cfg(merge_window_seconds=300),
        health={}, gamma=_Gamma(), storage=wired_storage,
    )
    await uc.handle(_event("tx:1", "c1"))
    await uc.flush_all()
    assert uc.funnel["bucket_flushed"] == 1
    assert len(renderer.batches[0]) == 1
