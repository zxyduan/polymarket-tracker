"""price_watch 特性层端口协议。

feature 层只依赖这些抽象端口，不感知具体 HTTP 客户端 / 存储实现；
`features/price_watch` 禁止 import `src/`。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from framework.models.message import RenderedMessage


@runtime_checkable
class PriceFeedPort(Protocol):
    """行情报价端口：按标的类别取最新报价。"""

    async def get_quote(
        self, symbol: str, category: str, *, contract: str = ""
    ) -> object: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class PriceRendererPort(Protocol):
    """消息渲染端口：把告警事件渲染为 L2 消息 DTO。"""

    def render(self, event: object) -> RenderedMessage | None: ...
