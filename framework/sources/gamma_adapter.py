"""gamma 适配器（设计 v3.5 §4.2）。薄壳，缓存/降级语义不变。"""

from __future__ import annotations

from typing import Any

from src.sources.gamma_client import GammaClient


class GammaAdapter:
    def __init__(self, base_url: str, **kwargs: Any) -> None:
        self._inner = GammaClient(base_url, **kwargs)

    async def get_market(
        self, storage: Any, condition_id: str
    ) -> tuple[str | None, str | None]:
        return await self._inner.get_market(storage, condition_id)
