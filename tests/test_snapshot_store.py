"""SnapshotStore.windows_sum 测试（T02/T03）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.storage import Storage
from framework.store.snapshot_store import SnapshotStore


async def _insert_row(storage, *, cid, ts, shares, slug=""):
    await storage._execute(
        "INSERT INTO market_snapshots "
        "(condition_id, slug, slug_id, bucket_ts, price_open, price_close, "
        " price_high, price_low, volume_shares, collected_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, slug, "", ts, 1.0, 1.0, 1.0, 1.0, shares, ts),
    )


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    db = Storage(str(tmp_path / "test.db"))
    await db.init()
    return db


async def test_windows_sum_basic_and_cid_filter(storage):
    s = SnapshotStore(storage)
    B = 1_000_000
    await _insert_row(storage, cid="aaa", ts=B, shares=1.5)
    await _insert_row(storage, cid="aaa", ts=B + 60, shares=-1.0)   # 未知桶贡献 0
    await _insert_row(storage, cid="bbb", ts=B, shares=2.5)
    await _insert_row(storage, cid="ccc", ts=B - 10_000, shares=9.9)  # 窗口外

    rows = await s.windows_sum(start_ts=B - 1, end_ts=B + 3600)
    by = {r.condition_id: r for r in rows}
    assert set(by) == {"aaa", "bbb"}
    assert by["aaa"].shares == 1.5 and by["aaa"].buckets == 2
    assert by["bbb"].shares == 2.5

    only_aaa = await s.windows_sum(start_ts=B - 1, end_ts=B + 3600, cid="aaa")
    assert [r.condition_id for r in only_aaa] == ["aaa"]

    # 开闭区间边界：end_ts 上界不包含
    empty = await s.windows_sum(start_ts=B, end_ts=B)
    assert empty == []


def test_snapshot_store_satisfies_query_protocol(storage):
    from framework.models.ports import SnapshotQuery
    from framework.store.snapshot_store import SnapshotStore
    assert isinstance(SnapshotStore(storage), SnapshotQuery)
