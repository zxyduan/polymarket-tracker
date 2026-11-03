"""FeatureModule / FeatureRuntime（设计 v3.5 §10，T11）。

每个 feature 一个独立 Module：持有自己的 router/guard/usecase，
FEATURES 注册表集中声明；bootstrap 只构造、不写业务分支。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class FeatureModule:
    name: str
    enabled: bool
    run: Callable[[], Awaitable[None]]
    health: dict = field(default_factory=dict)


class FeatureRuntime:
    def __init__(self) -> None:
        self._modules: list[FeatureModule] = []

    def register(self, module: FeatureModule) -> None:
        self._modules.append(module)

    async def run_enabled(self) -> None:
        for m in self._modules:
            if not m.enabled:
                continue
            try:
                await m.run()
            except Exception:
                logger.exception("feature %s failed", m.name)

    @property
    def modules(self) -> list[FeatureModule]:
        return list(self._modules)
