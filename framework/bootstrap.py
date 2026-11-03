"""bootstrap 装配（设计 v3.5 §10）。

按 feature 装配独立 router/guard/usecase 并注册到 FeatureRuntime。
当前 P1：只构造、不替换旧链路（feature 开关由调用方决定是否 run_enabled）。
"""

from __future__ import annotations

import logging
from typing import Any

from framework.delivery.router import ChannelRouter
from framework.render.paginator import MessagePaginator
from framework.runtime.module import FeatureModule, FeatureRuntime
from framework.scheduler.guard import BodyHashGuard
from framework.store.state_store import StateStore

logger = logging.getLogger(__name__)


def build_runtime(*, storage: Any, cfg: Any, channels: list[Any],
                  provider: Any, health: dict) -> FeatureRuntime:
    rt = FeatureRuntime()

    cursor = StateStore(storage)
    router = ChannelRouter(channels, provider)
    guard = BodyHashGuard(cursor)

    # 占位：F2/F3 usecase 暂不注册，等 main.py 切换时接入。
    rt.register(FeatureModule(
        name="market_snapshot", enabled=False,
        run=lambda: _noop("market_snapshot"), health=health,
    ))
    rt.register(FeatureModule(
        name="daily_report", enabled=False,
        run=lambda: _noop("daily_report"), health=health,
    ))
    return rt


async def _noop(name: str) -> None:
    logger.debug("feature %s not yet wired", name)
