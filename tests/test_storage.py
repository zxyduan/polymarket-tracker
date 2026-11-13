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
    ok, _, _ = await storage.add_wallet(s.W2, "B")
    assert ok
    ok2, _, _ = await storage.add_wallet(s.W2, "B")
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


# ── 配置对账与保留期（数据生命周期）──────────────────────────────────────


async def test_sync_three_ways(storage: Storage) -> None:
    """三态对账：插入、对齐更新、停用各覆盖一次。"""
    await storage.add_wallet(s.W1, "原标签")
    # 第一轮：W1 改标签（updated）+ W2 新插入（inserted）
    r = await storage.sync_wallets_from_config(
        [WatchlistSeed(s.W1, "改标签"), WatchlistSeed(s.W2, "新乙")]
    )
    assert r == {"inserted": 1, "updated": 1, "deactivated": 0}
    w1 = next(
        w for w in await storage.list_wallets(active_only=False)
        if w["address"] == s.W1
    )
    assert w1["active"] == 1 and w1["label"] == "改标签"
    # 第二轮：目标不含 W1 → 停用（deactivated）
    r2 = await storage.sync_wallets_from_config([WatchlistSeed(s.W2, "新乙")])
    assert r2 == {"inserted": 0, "updated": 1, "deactivated": 1}
    w1b = next(
        w for w in await storage.list_wallets(active_only=False)
        if w["address"] == s.W1
    )
    assert w1b["active"] == 0


async def test_sync_reactivates_inactive(storage: Storage) -> None:
    """配置仍含已停用地址时，对账重新激活。"""
    await storage.add_wallet(s.W1, "甲")
    await storage.remove_wallet(s.W1)
    await storage.sync_wallets_from_config([WatchlistSeed(s.W1, "甲")])
    w = next(
        x for x in await storage.list_wallets(active_only=False)
        if x["address"] == s.W1
    )
    assert w["active"] == 1


async def test_add_reactivates_removed(storage: Storage) -> None:
    """D9：remove 后重新 add 同地址应成功并重新激活。"""
    await storage.add_wallet(s.W1, "甲")
    await storage.remove_wallet(s.W1)
    ok, _msg, action = await storage.add_wallet(s.W1, "甲回")
    assert ok and action == "reactivated"
    assert s.W1 in await storage.active_addresses()


async def test_prune_events_24h(storage: Storage) -> None:
    """成交事件保留 1 天：25h 前删除、当前保留。"""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    new_t = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    old_t = (now - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    for key, ts in (("k1", old_t), ("k2", new_t)):
        await storage._execute(
            "INSERT INTO events(dedupe_key,source,wallet,event_type,event_time,"
            "payload_json,first_seen) VALUES(?,?,?,?,?,?,?)",
            (key, "rtds", s.W1, "TRADE", ts, "{}", new_t),
        )
    await storage.prune_events(1)
    rows = await storage._fetchall("SELECT dedupe_key FROM events")
    assert [r["dedupe_key"] for r in rows] == ["k2"]


async def test_prune_snapshots_30d(storage: Storage) -> None:
    """快照保留 30 天：31d 前桶删除、当前桶保留。"""
    import time

    now = int(time.time())
    cols = (
        "condition_id", "bucket_ts", "price_open", "price_close", "price_high",
        "price_low", "trend", "trend_pct", "trend_streak", "trend_direction",
        "drawdown_from_high", "price_percentile", "bid_depth_top5",
        "ask_depth_top5", "bid_wall_count", "bid_depth_change", "vol_short",
        "vol_long", "vol_regime", "our_fv", "market_fv", "collected_ts",
    )

    def row(cid: str, ts: int) -> tuple:
        return (
            cid, ts, 0.5, 0.5, 0.6, 0.4, "flat", 0.0, 0, "flat", 0.0, 0.5,
            0.0, 0.0, 0, 0.0, 0.0, 0.0, "normal", 0.0, 0.0, float(now),
        )

    sql = f"INSERT INTO market_snapshots({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    await storage._execute(sql, row("0xold", now - 31 * 86400))
    await storage._execute(sql, row("0xnew", now))
    await storage.prune_snapshots(30)
    rows = await storage._fetchall("SELECT condition_id FROM market_snapshots")
    assert [r["condition_id"] for r in rows] == ["0xnew"]
