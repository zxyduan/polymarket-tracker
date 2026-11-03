"""守卫测试（T08）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.storage import Storage
from framework.models.ports import CursorStore
from framework.scheduler.guard import BodyHashGuard, FireOnceGuard, CommitCondition
from framework.store.state_store import StateStore


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    db = Storage(str(tmp_path / "test.db"))
    await db.init()
    return db


@pytest.fixture
async def states(storage) -> StateStore:
    return StateStore(storage)


def test_state_store_satisfies_cursorstore_protocol(states):
    assert isinstance(states, CursorStore)


async def test_bodyhash_guard_dedup(states):
    g = BodyHashGuard(states)
    assert not await g.matches("snapshot", "push:d:label", "body-A")
    await g.record("snapshot", "push:d:label", "body-A")
    assert await g.matches("snapshot", "push:d:label", "body-A")       # 相同正文命中
    assert not await g.matches("snapshot", "push:d:label", "body-B")  # 不同正文不命中


def test_fireonce_guard_has_no_bodyhash():
    g = FireOnceGuard(
        commit_when=CommitCondition.RETURN_OK,
        commit_state_on=frozenset({"ok"}),
        retry_poll_seconds=30,
        cooldown_seconds=60,
    )
    assert not hasattr(g, "body_hash")
