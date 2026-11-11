"""Market Screen 端口协议（设计 v3.5 §8，dev §4.6）。

特性层只依赖这些抽象端口，不感知具体 HTTP 客户端 / 存储实现；
`features/market_screen` 禁止 import `src/`。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from framework.models.market_screen import CandidateMarket, ScreenQueryResult, ScreenRunMeta


@runtime_checkable
class ScreenLister(Protocol):
    """市场发现层端口：翻页拉取原始 Gamma 条目。"""

    async def list_active_markets(
        self, *, page_limit: int, max_pages: int
    ) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ScreenStore(Protocol):
    """存储端口：写入当日分区 + 只读持续性查询。"""

    async def replace_screen_day(
        self,
        run_date: str,
        rows: list[CandidateMarket],
        meta: ScreenRunMeta,
    ) -> None: ...

    async def query_persistent_candidates(
        self, **kwargs: Any
    ) -> ScreenQueryResult: ...
