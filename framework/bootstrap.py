"""bootstrap 装配（设计 v3.5 §10）。

按 feature 装配独立 router/guard/usecase 并注册到 FeatureRuntime。
P1 双轨：switch=False（默认）时两个 feature 仍 disabled，旧链路继续跑；
switch=True 时构造真实 usecase 并 enabled=True。
"""

from __future__ import annotations

import logging
from typing import Any

from framework.adapters.legacy_collector import LegacyCollectorAdapter
from framework.delivery.router import ChannelRouter
from framework.render.paginator import MessagePaginator
from framework.runtime.module import FeatureModule, FeatureRuntime
from framework.scheduler.guard import BodyHashGuard
from framework.store.snapshot_store import SnapshotStore
from framework.store.state_store import StateStore
from features.daily_report.usecase import DailyReportConfig, DailyReportUsecase
from features.market_snapshot.usecase import SnapshotPushConfig, SnapshotPushUsecase

logger = logging.getLogger(__name__)


def build_runtime(*, storage: Any, channels: list[Any], provider: Any,
                  health: dict, collector: Any = None, catalog: Any = None,
                  renderer: Any = None, daily_renderer: Any = None,
                  switch: bool = False) -> FeatureRuntime:
    rt = FeatureRuntime()

    cursor = StateStore(storage)
    router = ChannelRouter(channels, provider)
    guard = BodyHashGuard(cursor)

    snap_run = _noop("market_snapshot")
    daily_run = _noop("daily_report")

    if switch and collector is not None:
        from framework.store.snapshot_store import SnapshotStore  # noqa: F401
        snap_collector = LegacyCollectorAdapter(collector)
        query = SnapshotStore(storage)
        uc = SnapshotPushUsecase(
            catalog=catalog, collector=snap_collector, query=query,
            renderer=renderer, router=router, body_hash=guard,
            config=SnapshotPushConfig(), health=health,
        )
        snap_run = uc.run

    if switch and daily_renderer is not None:
        query = SnapshotStore(storage)
        uc = DailyReportUsecase(
            catalog=catalog, query=query, renderer=daily_renderer,
            router=router, paginator=MessagePaginator(), body_hash=guard,
            config=DailyReportConfig(), health=health,
        )
        daily_run = uc.run

    rt.register(FeatureModule(name="market_snapshot", enabled=switch,
                              run=snap_run, health=health))
    rt.register(FeatureModule(name="daily_report", enabled=switch,
                              run=daily_run, health=health))
    return rt


async def _noop(name: str) -> None:
    logger.debug("feature %s not yet wired", name)
