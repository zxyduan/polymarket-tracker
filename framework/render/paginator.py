"""MessagePaginator（设计 v3.5 §7.2，F3 专用）。

切分 RenderedMessage.body，保留 body_format/event_keys；
title 首段沿用、续段追加"（续 i/n）"。长度口径逐字平移 main.py 的
_daily_oversize/_fit_daily_chunk：4096 字符与 4096 字节双上限。
F1/F2 不实例化本类。
"""

from __future__ import annotations

from dataclasses import dataclass

from framework.models.message import RenderedMessage


@dataclass(frozen=True)
class LengthPolicy:
    max_chars: int = 4096
    max_bytes: int = 4096


class MessagePaginator:
    def __init__(self, policy: LengthPolicy | None = None) -> None:
        self._policy = policy or LengthPolicy()

    def _oversize(self, text: str) -> bool:
        p = self._policy
        return len(text) > p.max_chars or len(text.encode("utf-8")) > p.max_bytes

    def _fit(self, lines: list[str], title: str) -> list[list[str]]:
        if not lines:
            return [lines]
        probe = "\n".join([title, *lines])
        if not self._oversize(probe) or len(lines) == 1:
            return [lines]
        mid = max(1, len(lines) // 2)
        return self._fit(lines[:mid], title) + self._fit(lines[mid:], title)

    def paginate(self, msg: RenderedMessage) -> list[RenderedMessage]:
        lines = msg.body.split("\n") if msg.body else []
        fitted = self._fit(lines, msg.title)
        total = len(fitted)
        out: list[RenderedMessage] = []
        for i, chunk in enumerate(fitted, 1):
            head = msg.title if i == 1 else f"{msg.title}（续 {i}/{total}）"
            body = "\n".join([head, *chunk])
            out.append(RenderedMessage(
                body=body,
                body_format=msg.body_format,
                title=msg.title,
                event_keys=msg.event_keys,
                buttons=msg.buttons,
            ))
        return out
