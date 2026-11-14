"""models 层端口协议（设计 v3.5 §4.1 / §8.3）。

只定义协议，不定义实现；scheduler/guard/usecase 只依赖这里，
不得 import src.storage 等 L3 实现（09 §1.2 分层矩阵）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from framework.models.snapshot import WindowSum


@runtime_checkable
class CursorStore(Protocol):
    """StateStore 结构化实现需满足的协议（T01）。"""

    async def get(self, namespace: str, key: str) -> str | None: ...

    async def set(self, namespace: str, key: str, value: str) -> None: ...


@runtime_checkable
class SnapshotQuery(Protocol):
    """F2/F3 用例依赖的快照查询端口（§3.2）。

    latest_complete_buckets 暂时仍返回 dict 行（Snapshot 26 字段 DTO
    待 P5 落地）；windows_sum 已返回 WindowSum DTO。
    """

    async def latest_complete_buckets(
        self, now_bucket: int, condition_ids: list[str] | None = None
    ) -> list[dict]: ...

    async def windows_sum(
        self, *, start_ts: int, end_ts: int, cid: str | None = None
    ) -> list["WindowSum"]: ...
