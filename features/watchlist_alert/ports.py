"""白名单市场告警端口协议（T3.1）。

特性层只依赖这些抽象端口，不感知具体 HTTP 客户端 / 存储实现；
`features/watchlist_alert` 禁止 import `src/`。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from framework.models.message import RenderedMessage


@runtime_checkable
class RewardWatcherPort(Protocol):
    """奖励查询端口：按 condition_id 拉取 CLOB 原始奖励段。"""

    async def get_reward(self, condition_id: str) -> dict[str, Any] | None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class AlertRendererPort(Protocol):
    """消息渲染端口：把用例层事件 / 桶渲染为 L2 消息 DTO。"""

    def render(self, event: Any) -> RenderedMessage | None: ...
