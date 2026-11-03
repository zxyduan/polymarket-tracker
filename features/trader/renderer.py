"""F1 交易员渲染适配层。"""

from __future__ import annotations

from framework.models.message import RenderedMessage


class LegacyTraderRenderer:
    def render(self, event):
        return RenderedMessage(
            body=event.html,
            body_format="html",
            title=getattr(event, "title", ""),
            event_keys=tuple(getattr(event, "event_keys", [])),
            buttons=getattr(event, "buttons", []),
        )
