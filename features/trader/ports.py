"""F1 交易员告警端口（§6）。"""

from __future__ import annotations

from typing import Any, Protocol

from framework.models.message import RenderedMessage


class AlertRenderer(Protocol):
    def render(self, event: Any) -> RenderedMessage | None: ...
