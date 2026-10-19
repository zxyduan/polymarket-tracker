"""存储层测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.parsing import parse_rtds_message
from src.settings import WatchlistSeed
from src.storage import Storage
from tests.fixtures import samples as s


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    db = Storage(str(tmp_path / "test.db"))
    await db.init()
    return db


async def test_seed_and_list(storage: Storage) -> None:
    added = await storage.seed_wallets(
        [WatchlistSeed(address=s.W1, label="A")]
    )
    assert added == 1
    # 重复种子不新增
    added_again = await storage.seed_wallets(
        [WatchlistSeed(address=s.W1, label="A")]
    )
    assert added_again == 0
    wallets = await storage.list_wallets()
    assert len(wallets) == 1
    assert s.W1 in await storage.active_addresses()


async def test_add_remove(storage: Storage) -> None:
    ok, _ = await storage.add_wallet(s.W2, "B")
    assert ok
    ok2, _ = await storage.add_wallet(s.W2, "B")
    assert not ok2
    removed, _ = await storage.remove_wallet(s.W2)
    assert removed
    assert len(await storage.list_wallets()) == 0


async def test_event_dedup(storage: Storage) -> None:
    events = parse_rtds_message(s.RTDS_TRADE)
    assert await storage.insert_event(events[0]) is True
    assert await storage.insert_event(events[0]) is False


async def test_market_cache(storage: Storage) -> None:
    assert await storage.get_market_cache("0xaaa") is None
    await storage.set_market_cache("0xaaa", "Q?", "slug")
    cached = await storage.get_market_cache("0xaaa")
    assert cached is not None
    assert cached["question"] == "Q?"


async def test_state(storage: Storage) -> None:
    assert await storage.get_state("k") is None
    await storage.set_state("k", "v")
    assert await storage.get_state("k") == "v"


async def test_notification_record(storage: Storage) -> None:
    await storage.record_notification("123", "text", "key", "success")
    assert await storage.notified_count_today() == 1


async def test_prune_notifications(storage: Storage) -> None:
    from datetime import datetime, timedelta, timezone

    # 直接插入一条 40 天前的推送记录
    old = (
        datetime.now(timezone.utc) - timedelta(days=40)
    ).isoformat(timespec="seconds")
    await storage._execute(
        "INSERT INTO notifications(chat_id, message_text, event_keys, status, sent_at) "
        "VALUES(?,?,?,?,?)",
        ("123", "old", "k1", "success", old),
    )
    await storage.record_notification("123", "new", "k2", "success")
    await storage.prune_notifications(30)
    rows = await storage._fetchall("SELECT * FROM notifications")
    assert len(rows) == 1
    assert rows[0]["message_text"] == "new"


async def test_vacuum(storage: Storage) -> None:
    await storage.set_state("k", "v")
    await storage.vacuum()
    assert await storage.get_state("k") == "v"
