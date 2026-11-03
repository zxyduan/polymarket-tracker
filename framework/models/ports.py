"""models 层端口协议（设计 v3.5 §4.1 / §8.3）。

只定义协议，不定义实现；scheduler/guard/usecase 只依赖这里，
不得 import src.storage 等 L3 实现（09 §1.2 分层矩阵）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CursorStore(Protocol):
    """StateStore 结构化实现需满足的协议（T01）。"""

    async def get(self, namespace: str, key: str) -> str | None: ...

    async def set(self, namespace: str, key: str, value: str) -> None: ...
