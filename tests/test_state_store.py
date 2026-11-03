"""StateStore 门面测试（T01）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.storage import Storage
from framework.store.state_store import StateStore


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    db = Storage(str(tmp_path / "test.db"))
    await db.init()
    return db


async def test_state_store_roundtrip(storage):
    ss = StateStore(storage)
    assert await ss.get("data_api", "l2_last_run") is None
    await ss.set("data_api", "0xabc:last_time", "2026-11-03T00:00:00+00:00")
    assert await ss.get("data_api", "0xabc:last_time") == "2026-11-03T00:00:00+00:00"
    # 底层仍是旧平铺键，与旧读路径互通（迁移前行为不变）
    assert await storage.get_state("data_api:0xabc:last_time") == "2026-11-03T00:00:00+00:00"


async def test_state_store_overwrite(storage):
    ss = StateStore(storage)
    await ss.set("snapshot", "push:label", "a")
    await ss.set("snapshot", "push:label", "b")
    assert await ss.get("snapshot", "push:label") == "b"
