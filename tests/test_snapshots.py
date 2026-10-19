"""快照特征计算 / 渲染 / 存储测试。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.snapshots import collector as col
from src.snapshots.renderer import render_snapshot_summary
from src.storage import Storage


# ── 特征计算 ─────────────────────────────────────────────────────────────


def test_bucketize_prices() -> None:
    base = (int(time.time()) // 7200) * 7200
    points = [
        {"t": base, "p": "0.50"},
        {"t": base + 60, "p": 0.51},
        {"t": base + 7200, "p": 0.52},
    ]
    buckets = col._bucketize_prices(points)
    assert buckets[base] == [0.50, 0.51]
    assert buckets[base + 7200] == [0.52]


def test_calc_volume_delta() -> None:
    assert col._calc_volume_delta(100.0, 80.0) == 20.0
    assert col._calc_volume_delta(80.0, 100.0) == 0.0  # 回退钳制
    assert col._calc_volume_delta(None, 80.0) == 0.0
    assert col._calc_volume_delta(100.0, None) == 0.0


def test_calc_trend() -> None:
    hist = [
        {
            "price_close": 0.50,
            "trend": "up",
            "trend_streak": 2,
            "bucket_ts": 1,
        }
    ]
    # 上涨
    trend, pct, streak = col._calc_trend(0.51, hist)
    assert trend == "up"
    assert streak == 3
    assert pct == pytest.approx(0.02)
    # flat（半个 tick 内）
    trend, pct, streak = col._calc_trend(0.5004, hist)
    assert trend == "flat"
    assert streak == 2
    # 下跌
    trend, _, streak = col._calc_trend(0.49, hist)
    assert trend == "down"
    assert streak == -1


def test_calc_trend_direction() -> None:
    # 数据不足返回空
    assert col._calc_trend_direction([]) == ""
    history = [
        {"trend": "up" if i % 3 != 0 else "down"} for i in range(24)
    ]
    assert col._calc_trend_direction(history) in ("long_up", "sideways")


def test_calc_drawdown_and_percentile() -> None:
    now = int(time.time())
    hist = [
        {
            "bucket_ts": now - 3600,
            "price_high": 0.80,
            "price_low": 0.40,
            "price_close": 0.60,
            "trend": "flat",
            "trend_streak": 0,
        },
        {
            "bucket_ts": now - 8 * 86400,  # 8 天前：超过回撤窗口（7 天）
            "price_high": 0.90,
            "price_low": 0.30,
            "price_close": 0.50,
            "trend": "flat",
            "trend_streak": 0,
        },
    ]
    # 回撤：当前价 0.65 vs 近 7 天最高 0.80
    dd = col._calc_drawdown(0.65, hist)
    assert dd == pytest.approx((0.65 - 0.80) / 0.80)
    # 分位：0.65 在 30 天区间 [0.30, 0.90]（8 天前仍在窗口内）
    pct = col._calc_percentile(0.65, hist)
    assert pct == pytest.approx((0.65 - 0.30) / (0.90 - 0.30))


def test_calc_volatility() -> None:
    # 6 桶以内数据不足
    hist = [{"price_close": 0.5} for _ in range(3)]
    assert col._calc_volatility(hist) == (0.0, 0.0, "")
    # 平稳 → normal
    hist = [{"price_close": 0.5 + (i % 2) * 0.001} for i in range(30)]
    vol_s, vol_l, regime = col._calc_volatility(hist)
    assert regime == "normal"
    # 近期剧烈 → high
    hist2 = [{"price_close": 0.5} for _ in range(24)] + [
        {"price_close": 0.5 + (i % 2) * 0.3} for i in range(6)
    ]
    hist2 = hist2[::-1]  # 新的在前
    _, _, regime2 = col._calc_volatility(hist2)
    assert regime2 == "high"


def test_parse_book_depth() -> None:
    data = {
        "bids": [
            {"price": "0.50", "size": "100"},
            {"price": "0.49", "size": "50"},
            {"price": "0.48", "size": "30"},
            {"price": "0.47", "size": "20"},
            {"price": "0.46", "size": "10"},
            {"price": "0.45", "size": "5"},  # 第 6 档，忽略
        ],
        "asks": [{"price": "0.51", "size": "200"}],
    }
    book = col._parse_book_depth(data)
    # 前 5 档名义金额：0.50*100 + 0.49*50 + 0.48*30 + 0.47*20 + 0.46*10
    assert book.bid_top5 == pytest.approx(50 + 24.5 + 14.4 + 9.4 + 4.6)
    assert book.ask_top5 == pytest.approx(0.51 * 200)
    # 墙：名义 ≥100 USDC → 只有 0.50*100=50 不够，0.49*50=24.5 不够 → 0
    assert book.bid_wall_count == 0


def test_json_list() -> None:
    assert col._json_list('["a","b"]') == ["a", "b"]
    assert col._json_list(["a"]) == ["a"]
    assert col._json_list(None) == []
    assert col._json_list("bad") == []


# ── 渲染 ────────────────────────────────────────────────────────────────


def _row(
    slug_id: str,
    slug: str,
    bucket_ts: int,
    volume_delta: float = 100.0,
    trend: str = "up",
    trend_pct: float = 0.021,
    vol_regime: str = "high",
) -> dict:
    return {
        "slug_id": slug_id,
        "slug": slug,
        "bucket_ts": bucket_ts,
        "volume_delta": volume_delta,
        "trend": trend,
        "trend_pct": trend_pct,
        "vol_regime": vol_regime,
        "price_close": 0.63,
    }


def test_render_summary() -> None:
    bucket = (int(time.time()) // 7200) * 7200 - 7200  # 上一完整桶
    rows = [
        _row("kkkkkkk", "clarity-act-signed-into-law-in-2026", bucket, 1240, "up", 0.021, "high"),
        _row("abcdefg", "who-wins-2026", bucket, 312, "flat", 0.0, "low"),
    ]
    alert = render_snapshot_summary(rows, tz_offset_hours=8)
    assert alert is not None
    body = alert.html
    # slug_id 展示，不出现完整 slug
    assert "kkkkkkk" in body
    assert "clarity-act-signed-into-law-in-2026" not in body
    assert "abcdefg" in body
    # 两指标
    assert "量 1,240" in body
    assert "量 312" in body
    assert "波动 高 📈+2.1%" in body
    assert "波动 低 ➡️" in body
    assert "市场快照" in body


def test_render_summary_empty() -> None:
    assert render_snapshot_summary([], tz_offset_hours=8) is None


def test_render_fallback_slug() -> None:
    bucket = (int(time.time()) // 7200) * 7200 - 7200
    rows = [_row("", "clarity-act-signed-into-law-in-2026", bucket)]
    alert = render_snapshot_summary(rows, tz_offset_hours=8)
    assert alert is not None
    assert "clarity-act-signed-into-law-in-2026" in alert.html


# ── 存储 ────────────────────────────────────────────────────────────────


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    db = Storage(str(tmp_path / "test.db"))
    await db.init()
    return db


async def _sample_snapshot(**overrides: object) -> dict:
    base: dict = {
        "condition_id": "0xcid",
        "slug": "slug-a",
        "slug_id": "aaaa",
        "bucket_ts": (int(time.time()) // 7200) * 7200,
        "price_open": 0.5,
        "price_close": 0.51,
        "price_high": 0.52,
        "price_low": 0.49,
        "volume_delta": 100.0,
        "trend": "up",
        "trend_pct": 0.02,
        "trend_streak": 1,
        "trend_direction": "long_up",
        "drawdown_from_high": -0.02,
        "price_percentile": 0.6,
        "bid_depth_top5": 1000.0,
        "ask_depth_top5": 900.0,
        "bid_wall_count": 2,
        "bid_depth_change": 0.1,
        "vol_short": 0.01,
        "vol_long": 0.008,
        "vol_regime": "normal",
        "our_fv": 0.0,
        "market_fv": 0.505,
    }
    base.update(overrides)
    return base


async def test_snapshot_upsert_idempotent(storage: Storage) -> None:
    snap = await _sample_snapshot()
    await storage.upsert_snapshot(**snap)
    await storage.upsert_snapshot(**{**snap, "volume_delta": 200.0})
    last = await storage.last_snapshot("0xcid")
    assert last is not None
    assert last["volume_delta"] == 200.0
    rows = await storage.recent_snapshots("0xcid", buckets=10)
    assert len(rows) == 1  # 同桶重跑不重复


async def test_snapshot_baseline(storage: Storage) -> None:
    assert await storage.get_snap_baseline("0xcid") is None
    await storage.set_snap_baseline("0xcid", 1000.0)
    assert await storage.get_snap_baseline("0xcid") == 1000.0
    await storage.set_snap_baseline("0xcid", 1200.0)
    assert await storage.get_snap_baseline("0xcid") == 1200.0


async def test_latest_complete_buckets(storage: Storage) -> None:
    now_bucket = (int(time.time()) // 7200) * 7200
    # 两个市场：一个完整桶 + 一个当前未闭合桶
    await storage.upsert_snapshot(
        **await _sample_snapshot(condition_id="0xa", slug_id="aaaa", bucket_ts=now_bucket - 7200)
    )
    await storage.upsert_snapshot(
        **await _sample_snapshot(condition_id="0xa", slug_id="aaaa", bucket_ts=now_bucket)
    )
    await storage.upsert_snapshot(
        **await _sample_snapshot(condition_id="0xb", slug_id="bbbb", bucket_ts=now_bucket - 7200)
    )
    rows = await storage.latest_complete_buckets(now_bucket)
    # 只返回每个市场最近一个已闭合桶（排除当前桶）
    by_cid = {r["condition_id"]: r["bucket_ts"] for r in rows}
    assert by_cid == {"0xa": now_bucket - 7200, "0xb": now_bucket - 7200}


async def test_prune_snapshots(storage: Storage) -> None:
    old_ts = int(time.time()) - 100 * 86400
    await storage.upsert_snapshot(
        **await _sample_snapshot(condition_id="0xold", bucket_ts=old_ts)
    )
    await storage.upsert_snapshot(
        **await _sample_snapshot(condition_id="0xnew", bucket_ts=(int(time.time()) // 7200) * 7200)
    )
    await storage.prune_snapshots(retention_days=90)
    assert await storage.last_snapshot("0xold") is None
    assert await storage.last_snapshot("0xnew") is not None
