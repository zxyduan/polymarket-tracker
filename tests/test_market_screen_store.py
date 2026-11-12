"""Market Screen 存储层与门面单测（设计 §10.1 #6–14、#16、#17）。"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from framework.models.market_screen import CandidateMarket, ScreenRunMeta
from framework.store.market_screen_store import MarketScreenStore
from src.storage import Storage


def _row(cid: str, run_date: str, rank: int, vol: float, prob: float,
         remain: int, passed: int = 1,
         reward_rate: float | None = None, reward_asset: str = "") -> CandidateMarket:
    return CandidateMarket(
        run_date=run_date, condition_id=cid, slug=f"slug-{cid}", question=f"Q {cid}",
        outcomes_json='["Yes","No"]', end_date_iso="2027-12-31", end_date_raw="",
        remaining_days=remain, prob_yes=1 - prob, prob_no=prob, minority_prob=prob,
        volume_24h=vol, volume_metric="volume24hr", rank_no=rank,
        horizon_preset="balanced", threshold_lo=0.02, horizon_pass=passed,
        pass_strict=passed, pass_balanced=passed, pass_loose=passed,
        screen_passed=passed, screen_reason="pass", collected_ts=0.0,
        rewards_daily_rate=reward_rate, rewards_asset=reward_asset,
    )


def _meta(run_date: str, count: int, passed: int) -> ScreenRunMeta:
    return ScreenRunMeta(
        run_date=run_date, started_at=f"{run_date} 08:00:00",
        finished_at=f"{run_date} 08:00:05", elapsed_ms=5000, pages_fetched=1,
        candidate_count=count, horizon_preset="balanced", pass_strict_count=0,
        pass_balanced_count=passed, pass_loose_count=0, passed_count=passed,
        status="done", error="",
    )


class _Cfg:
    timezone_offset_hours = 8
    retention_days = 30
    query_defaults = types.SimpleNamespace(
        window_days=10, min_hit_days=10, min_volume_24h=50000.0, max_rank=200,
        min_remaining_days=31, prob_max=0.15, prob_side="minority",
        require_consecutive=False,
    )


@pytest.fixture
async def db(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    await storage.init()
    yield storage
    await storage.close()


@pytest.fixture
async def store(db):
    return MarketScreenStore(db, config=_Cfg())


async def test_replace_day_idempotent_overwrite(db):
    d1 = "2026-11-10"
    await db.replace_screen_day(d1, [_row("0xa", d1, 1, 60000, 0.01, 400)], _meta(d1, 1, 1))
    # 重跑同日：候选被替换而非累加
    await db.replace_screen_day(d1, [_row("0xb", d1, 2, 70000, 0.02, 400)], _meta(d1, 1, 1))
    rows = await db.list_screen_daily(d1)
    assert len(rows) == 1
    assert rows[0]["condition_id"] == "0xb"


async def test_list_daily_filters(db):
    d1 = "2026-11-10"
    rows = [
        _row("0xa", d1, 1, 60000, 0.01, 400, passed=1),
        _row("0xc", d1, 2, 40000, 0.05, 400, passed=0),
    ]
    await db.replace_screen_day(d1, rows, _meta(d1, 2, 1))

    passed = await db.list_screen_daily(d1, passed_only=True)
    assert len(passed) == 1 and passed[0]["condition_id"] == "0xa"

    by_balanced = await db.list_screen_daily(d1, preset="balanced")
    assert len(by_balanced) == 1
    # preset 白名单外名称静默忽略，不拼列名
    unknown = await db.list_screen_daily(d1, preset="nope")
    assert len(unknown) == 2


async def test_query_screen_rows_whitelist(db):
    d1 = "2026-11-10"
    await db.replace_screen_day(d1, [_row("0xa", d1, 1, 60000, 0.01, 400)], _meta(d1, 1, 1))
    rows = await db.query_screen_rows("v_screen_latest_done", {})
    assert len(rows) == 1
    with pytest.raises(ValueError):
        await db.query_screen_rows("sqlite_master", {})


async def test_persistent_hit_and_streak(db):
    days = ["2026-11-09", "2026-11-10", "2026-11-11"]
    for d in days:
        await db.replace_screen_day(
            d,
            [
                _row("0xa", d, 1, 60000, 0.012, 400),
                _row("0xb", d, 2, 55000, 0.03, 400),  # 高概率被 prob_max 滤掉
            ],
            _meta(d, 2, 1),
        )
    # 仅给 0xa 第三天补一行：0xa 三日连续，0xb 只在窗口内两日
    await db.replace_screen_day(
        "2026-11-11",
        [_row("0xa", "2026-11-11", 1, 60000, 0.012, 400,
              reward_rate=150.0, reward_asset="USDC.e")],
        _meta("2026-11-11", 1, 1),
    )

    hit = await db.query_persistent_rows(
        start_date="2026-11-09", end_date="2026-11-11", max_rank=200,
        min_hit_days=3, min_volume_24h=50000.0, min_remaining_days=31,
        prob_max=0.15, prob_side="minority",
    )
    assert [r["condition_id"] for r in hit] == ["0xa"]
    assert hit[0]["hit_days"] == 3
    assert hit[0]["latest_rewards_daily_rate"] == pytest.approx(150.0)
    assert hit[0]["latest_rewards_asset"] == "USDC.e"

    streak = await db.query_persistent_rows(
        start_date="2026-11-09", end_date="2026-11-11", max_rank=200,
        min_hit_days=3, min_volume_24h=50000.0, min_remaining_days=31,
        prob_max=0.15, prob_side="minority", require_consecutive=True,
    )
    assert streak[0]["max_streak"] == 3
    assert streak[0]["latest_rewards_daily_rate"] == pytest.approx(150.0)
    assert streak[0]["latest_rewards_asset"] == "USDC.e"


async def test_persistent_prob_side_yes(db):
    d1 = "2026-11-10"
    # minority=0.49（Yes=0.51 是大侧），按 yes 侧概率 0.51 会被 0.15 滤掉；
    # minority 侧 0.49 也 >0.15，两条都应被滤除。
    await db.replace_screen_day(
        d1, [_row("0xa", d1, 1, 60000, 0.49, 400)], _meta(d1, 1, 0),
    )
    yes_rows = await db.query_persistent_rows(
        start_date=d1, end_date=d1, max_rank=200, min_hit_days=1,
        min_volume_24h=1.0, min_remaining_days=1, prob_max=0.15, prob_side="yes",
    )
    assert yes_rows == []


async def test_prune_keeps_cutoff_day(db):
    old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    recent = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()
    await db.replace_screen_day(old, [_row("0xold", old, 1, 60000, 0.01, 400)], _meta(old, 1, 1))
    await db.replace_screen_day(recent, [_row("0xnew", recent, 1, 60000, 0.01, 400)], _meta(recent, 1, 1))
    await db.prune_screen(30)
    remaining = await db.query_screen_rows("market_screen_daily", {})
    assert len(remaining) == 1
    assert remaining[0]["condition_id"] == "0xnew"


async def test_empty_candidate_day_transaction(db):
    d1 = "2026-11-10"
    await db.replace_screen_day(d1, [], _meta(d1, 0, 0))
    rows = await db.list_screen_daily(d1)
    assert rows == []
    run = await db.query_screen_rows("market_screen_run", {})
    assert len(run) == 1 and run[0]["candidate_count"] == 0 and run[0]["status"] == "done"


async def test_store_defaults_fallback_and_warnings(store, db):
    d1 = "2026-11-10"
    await db.replace_screen_day(
        d1, [_row("0xa", d1, 1, 60000, 0.01, 400)], _meta(d1, 1, 1),
    )
    # 全空入参 → 回落默认；窗口 10 天、命中 10 天，单日数据不命中，无异常
    result = await store.query_persistent_candidates(end_date=d1)
    assert result.rows == []
    assert result.warnings == []

    # 非法 prob_max / prob_side / 越界 hit_days → 收敛并归集 warning
    bad = await store.query_persistent_candidates(
        end_date=d1, prob_max=2.0, prob_side="big", min_hit_days=99,
    )
    text = " ".join(bad.warnings)
    assert "prob_max" in text
    assert "prob_side" in text


def test_store_date_range_offset(store):
    start, end = store._date_range(10, "2026-11-11")
    assert end == "2026-11-11"
    assert start == "2026-11-02"


async def test_view_rebuilt_on_drift(tmp_path):
    path = str(tmp_path / "drift.db")
    first = Storage(path)
    await first.init()
    # 人为篡改视图定义，模拟旧库漂移
    await first._execute("DROP VIEW IF EXISTS v_screen_latest_done")
    await first._execute("CREATE VIEW v_screen_latest_done AS SELECT 1 AS x")
    await first.close()

    second = Storage(path)
    await second.init()  # _ensure_view 应检测漂移并重建
    rows = await second.query_screen_rows("v_screen_latest_done", {})
    # 重建后视图来自 market_screen_daily（空表），列结构为明细列而非 x
    assert rows == []
    await second.close()


async def test_store_persistent_query_maps_dto(store, db):
    for day in ("2026-11-09", "2026-11-10"):
        await db.replace_screen_day(
            day,
            [_row("0xa", day, 1, 60000.0, 0.012, 400)],
            _meta(day, 1, 1),
        )
    result = await store.query_persistent_candidates(
        end_date="2026-11-10",
        window_days=2,
        min_hit_days=2,
        min_volume_24h=50000.0,
        min_remaining_days=31,
        prob_max=0.15,
    )
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.condition_id == "0xa"
    assert row.hit_days == 2
    assert row.slug == "slug-0xa"
    assert row.minority_prob == pytest.approx(0.012)
    assert row.max_streak is None  # 非严格连续路径不填充
    assert result.warnings == []


# ── 奖励字段落库回读与读侧筛选 ──────────────────────────────────────────────


async def test_screen_daily_roundtrip_reward(db):
    d1 = "2026-11-10"
    await db.replace_screen_day(
        d1,
        [_row("0xa", d1, 1, 60000, 0.01, 400, reward_rate=120.0, reward_asset="USDC.e")],
        _meta(d1, 1, 1),
    )
    rows = await db.list_screen_daily(d1)
    assert rows[0]["rewards_daily_rate"] == pytest.approx(120.0)
    assert rows[0]["rewards_asset"] == "USDC.e"


async def test_persistent_reward_filter_and_dto(store, db):
    d1 = "2026-11-10"
    await db.replace_screen_day(
        d1,
        [
            _row("0xa", d1, 1, 60000, 0.01, 400, reward_rate=120.0, reward_asset="USDC.e"),
            _row("0xb", d1, 2, 55000, 0.01, 400, reward_rate=10.0, reward_asset="pUSD"),
            _row("0xc", d1, 3, 55000, 0.01, 400),  # 历史 NULL
        ],
        _meta(d1, 3, 1),
    )
    result = await store.query_persistent_candidates(
        end_date=d1, window_days=1, min_hit_days=1, min_volume_24h=1.0,
        min_remaining_days=1, prob_max=0.15, min_reward_daily_rate=30.0,
    )
    assert [r.condition_id for r in result.rows] == ["0xa"]
    row = result.rows[0]
    assert row.latest_rewards_daily_rate == pytest.approx(120.0)
    assert row.latest_rewards_asset == "USDC.e"


async def test_reward_tier_maps_floor(store, db):
    d1 = "2026-11-10"
    await db.replace_screen_day(
        d1,
        [
            _row("0xa", d1, 1, 60000, 0.01, 400, reward_rate=350.0, reward_asset="USDC.e"),
            _row("0xb", d1, 2, 55000, 0.01, 400, reward_rate=120.0, reward_asset="pUSD"),
        ],
        _meta(d1, 2, 1),
    )
    high = await store.query_persistent_candidates(
        end_date=d1, window_days=1, min_hit_days=1, min_volume_24h=1.0,
        min_remaining_days=1, prob_max=0.15, reward_tier="high",
    )
    assert [r.condition_id for r in high.rows] == ["0xa"]


async def test_sort_mode_reward_prefers_rate(store, db):
    d1 = "2026-11-10"
    await db.replace_screen_day(
        d1,
        [
            _row("0xa", d1, 1, 60000, 0.01, 200, reward_rate=120.0),
            _row("0xb", d1, 2, 55000, 0.01, 400, reward_rate=300.0),
        ],
        _meta(d1, 2, 1),
    )
    result = await store.query_persistent_candidates(
        end_date=d1, window_days=1, min_hit_days=1, min_volume_24h=1.0,
        min_remaining_days=1, prob_max=0.15, sort_mode="reward",
    )
    # 奖励 300 在前（0xb），即使其剩余天数更短。
    assert [r.condition_id for r in result.rows] == ["0xb", "0xa"]


async def test_invalid_sort_and_tier_warn_fallback(store, db):
    d1 = "2026-11-10"
    await db.replace_screen_day(
        d1, [_row("0xa", d1, 1, 60000, 0.01, 400, reward_rate=120.0)],
        _meta(d1, 1, 1),
    )
    result = await store.query_persistent_candidates(
        end_date=d1, window_days=1, min_hit_days=1, min_volume_24h=1.0,
        min_remaining_days=1, prob_max=0.15,
        sort_mode="weird", reward_tier="gold",
    )
    text = " ".join(result.warnings)
    assert "sort_mode" in text and "reward_tier" in text
    assert len(result.rows) == 1


async def test_init_reward_columns_idempotent(tmp_path):
    path = str(tmp_path / "migration.db")
    first = Storage(path)
    await first.init()
    await first.close()
    second = Storage(path)
    await second.init()  # 重复 ALTER 不报错
    await second.close()
