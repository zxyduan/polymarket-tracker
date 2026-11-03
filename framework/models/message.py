"""L2 消息 DTO（设计 v3.5 §7.1）。

边界纪律：边界只传 DTO，禁止 dict[str, Any] 穿过 L1/L2/L3；
消息与通道解耦——消息带 body_format，通道决定如何发送，
不在消息模型里假定 Telegram HTML。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

BodyFormat = Literal["html", "text"]


@dataclass(frozen=True)
class RenderedMessage:
    body: str
    body_format: BodyFormat = "html"
    title: str = ""
    event_keys: tuple[str, ...] = ()
    buttons: tuple[Mapping[str, Any], ...] = ()
