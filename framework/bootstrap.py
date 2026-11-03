"""bootstrap 装配（设计 v3.5 §10）。

集中装配三个 feature 的 router/guard/usecase，main.py 只调用 build_runtime。
"""

from __future__ import annotations

import logging
from typing import Any

from framework.adapters.legacy_collector import LegacyCollectorAdapter
from framework.adapters.notifier_channel import NotifierChannel
from framework.delivery.router import ChannelRouter
from framework.delivery.targets import DeliveryTarget
from framework.render.paginator import MessagePaginator
from framework.runtime.module import FeatureModule, FeatureRuntime
from framework.scheduler.guard import BodyHashGuard
from framework.store.snapshot_store import SnapshotStore
from framework.store.state_store import StateStore
from features.daily_report.usecase import DailyReportUsecase
from features.market_snapshot.usecase import SnapshotPushUsecase
from features.trader.usecase import TraderAlertUsecase, TraderConfig

logger = logging.getLogger(__name__)


class _LegacyProvider:
    """适配旧 NotifierChannel：返回默认 target 使 router dispatch 不跳过。"""
    def targets(self):
        return [DeliveryTarget(channel="legacy_notifier", address="")]


def build_runtime(*, storage: Any, collector: Any, catalog: Any,
                  snap_notifiers: list, daily_notifiers: list, trader_notifiers: list,
                  snap_renderer: Any, daily_renderer: Any, trader_renderer: Any,
                  snap_cfg, daily_cfg,
                  snap_health: dict, daily_health: dict) -> tuple[FeatureRuntime, Any]:
    rt = FeatureRuntime()
    cursor = StateStore(storage)
    guard = BodyHashGuard(cursor)

    snap_router = ChannelRouter([NotifierChannel(n) for n in snap_notifiers], _LegacyProvider())
    daily_router = ChannelRouter([NotifierChannel(n) for n in daily_notifiers], _LegacyProvider())
    trader_router = ChannelRouter([NotifierChannel(n) for n in trader_notifiers], _LegacyProvider())

    snap_uc = SnapshotPushUsecase(
        catalog=catalog, collector=LegacyCollectorAdapter(collector),
        query=SnapshotStore(storage), renderer=snap_renderer, router=snap_router,
        body_hash=guard, config=snap_cfg, health=snap_health,
    )
    daily_uc = DailyReportUsecase(
        catalog=catalog, query=SnapshotStore(storage), renderer=daily_renderer,
        router=daily_router, paginator=MessagePaginator(), body_hash=guard,
        config=daily_cfg, health=daily_health,
    )
    trader_uc = TraderAlertUsecase(
        renderer=trader_renderer, router=trader_router, guard=None,
        config=TraderConfig(), health={},
    )

    rt.register(FeatureModule(name="market_snapshot", enabled=True, run=snap_uc.run))
    rt.register(FeatureModule(name="daily_report", enabled=True, run=daily_uc.run))
    rt.register(FeatureModule(name="trader", enabled=True, run=_noop))
    return rt, trader_uc


async def _noop() -> None: ...
