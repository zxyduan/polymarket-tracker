"""每日成交份额汇总（daily_report）测试（瘦身版）。

保留：Storage 聚合、渲染纯函数、调度状态机（daily_report_loop）、
配置校验、工厂装配、指令与回归。
已迁移：trigger 集成测试 → tests/test_daily_usecase.py；
      分段纯函数测试 → tests/test_paginator.py。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from framework.runtime.loops import daily_report_loop
from framework.runtime import loops as runtime_loops
from features.daily_report.renderer import DailyReportRenderer
from src.settings import DailyReportConfig, MarketEntry, Settings, resolve_daily_config
from src.snapshots.commands import (
    DailyCommandHandler,
    SnapshotCommandHandler,
    daily_result_reply,
)
from src.storage import Storage

R0 = 1_730_000_400  # 固定桶右端点（偶数小时对齐，仅测试用）


# ────────────────────────────── fixtures / helpers ──────────────────────────


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    db = Storage(str(tmp_path / "daily_test.db"))
    await db.init()
    return db


def _mk_settings(raw: dict[str, Any]) -> Settings:
    return Settings(
        bot_token="",
        allowed_chat_ids=[],
        log_level="INFO",
        config_path=Path("config/config.yaml"),
        raw=raw,
    )


def make_cfg(**over: Any) -> DailyReportConfig:
    base: dict[str, Any] = dict(
        enabled=True,
        time_hour=7,
        time_minute=0,
        timezone_offset_hours=8,
        windows_days=[1, 3, 7],
        bucket_seconds=7200,
        top_n=0,
        page_size=1000,
        segment_delay_seconds=0.0,
        dedup=True,
        errors=[],
        warnings=[],
    )
    base.update(over)
    return DailyReportConfig(**base)


async def _snap(
    storage: Storage, cid: str, bucket_ts: int, shares: float, slug: str = "mk", slug_id: str = "MK"
) -> None:
    """写入一个 2h 桶快照（shares 为份额；-1 表示未知）。"""
    await storage.upsert_snapshot(
        condition_id=cid,
        slug=slug,
        slug_id=slug_id,
        bucket_ts=bucket_ts,
        price_open=0.5,
        price_close=0.5,
        price_high=0.6,
        price_low=0.4,
        volume_delta=0.0,
        volume_shares=shares,
        trend="flat",
        trend_pct=0.0,
        trend_streak=0,
        trend_direction="flat",
        drawdown_from_high=0.0,
        price_percentile=0.5,
        bid_depth_top5=0.0,
        ask_depth_top5=0.0,
        bid_wall_count=0,
        bid_depth_change=0.0,
        vol_short=0.0,
        vol_long=0.0,
        vol_regime="normal",
    )


class FakeCollector:
    def __init__(self, resolve_map: dict[str, str] | None = None, backfill_fail: set[str] = frozenset()):
        self.resolve_map = resolve_map or {}
        self.backfill_fail = set(backfill_fail)
        self.backfilled: list[str] = []

    async def resolve_market(self, slug: str):
        cid = self.resolve_map.get(slug)
        return (cid, slug) if cid else None

    async def backfill_prev_bucket(self, cid: str):
        self.backfilled.append(cid)
        if cid in self.backfill_fail:
            raise RuntimeError("boom")


# ──────────────────────────── 6.1 volume_by_window ───────────────────────────


async def test_volume_by_window_sum_and_exclusions(storage: Storage):
    # 市场 A：1d 内 12 桶各 100；3d 覆盖这些 + 更早 24 桶各 200；未知桶(-1)不计和；0 计和
    a = "0xAAA"
    for i in range(12):
        await _snap(storage, a, R0 - (i + 1) * 7200, 100.0)
    # 右开区间边界：bucket_ts == R0 的行不纳入
    await _snap(storage, a, R0, 9999.0)
    # 未知桶（-1）：命中行数计入 buckets，但不贡献 shares
    await _snap(storage, a, R0 - 13 * 7200, -1.0)
    # 真零成交 0.0：正常参与
    await _snap(storage, a, R0 - 14 * 7200, 0.0)
    b = "0xBBB"
    await _snap(storage, b, R0 - 7200, 50.0)  # 仅 1d 有行

    one = await storage.volume_by_window(R0 - 86400, R0)
    rows_a = {r["condition_id"]: r for r in one}
    assert rows_a["0xaaa"]["shares"] == 12 * 100.0
    assert rows_a["0xaaa"]["buckets"] >= 12
    assert "0xbbb" in rows_a

    three = await storage.volume_by_window(R0 - 3 * 86400, R0)
    a3 = next(r for r in three if r["condition_id"] == "0xaaa")
    assert a3["shares"] == 1200.0
    assert a3["buckets"] == 14


async def test_volume_by_window_right_open(storage: Storage):
    await _snap(storage, "0xc", R0, 1.0)
    await _snap(storage, "0xc", R0 - 1, 2.0)
    rows = await storage.volume_by_window(R0 - 7200, R0)
    assert next(r for r in rows if r["condition_id"] == "0xc")["shares"] == 2.0


async def test_volume_by_window_absent_market(storage: Storage):
    await _snap(storage, "0xpresent", R0 - 7200, 10.0)
    rows = await storage.volume_by_window(R0 - 86400, R0)
    assert all(r["condition_id"] != "0xabsent" for r in rows)


# ────────────────────────────── 6.3 渲染纯函数 ────────────────────────────────


def _row(cid, slug_id, d1, d3, d7):
    return {
        "condition_id": cid,
        "slug_id": slug_id,
        "slug": slug_id,
        "shares_1d": d1,
        "shares_3d": d3,
        "shares_7d": d7,
    }


def test_renderer_sort_tie_and_none():
    r = DailyReportRenderer()
    rows = [
        _row("0xb", "B", 100, 200, 300),
        _row("0xa", "A", 100, 200, 300),
        _row("0xc", "C", 500, 600, 700),
    ]
    msg = r.render(rows, windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=0)
    assert msg is not None
    assert "1️⃣ C" in msg.body
    assert r.render([], windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=0) is None


def test_renderer_text_redlines_and_thousands():
    r = DailyReportRenderer()
    rows = [_row("0xa", "a<b&c", 128540, 356208, 812940)]
    msg = r.render(rows, windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=0)
    assert msg is not None
    assert "截至今晨06:00" in msg.body
    assert "128,540" in msg.body and "356,208" in msg.body and "812,940" in msg.body
    assert "$" not in msg.body and "USDC" not in msg.body and "USD" not in msg.body
    assert "&lt;" in msg.body


def test_renderer_truncate():
    r = DailyReportRenderer()
    rows = [_row(f"0x{i}", f"M{i}", i * 10, i * 20, i * 30) for i in range(1, 8)]
    msg = r.render(rows, windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=5)
    assert msg is not None
    assert "其余 2 个市场未列出" in msg.body
    # top_n=0 不截断
    msg0 = r.render(rows, windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=0)
    assert msg0 is not None
    assert "其余" not in msg0.body


# ───────────────────────────── 6.6 调度状态机 ────────────────────────────────────


class _DT(datetime):
    fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.fixed if cls.fixed is not None else datetime.now(tz)


async def _drain_loop(storage, cfg, results, monkeypatch, cap=6):
    """驱动 daily_report_loop 若干轮，返回 trigger 调用次数。"""
    stop = asyncio.Event()
    calls: list[str] = []
    iters = 0

    async def fake_wait(awaitable, timeout=None):
        nonlocal iters
        iters += 1
        if iters >= cap:
            stop.set()
        try:
            awaitable.close()
        except Exception:  # noqa: BLE001
            pass
        return None

    real_wait = asyncio.wait_for
    monkeypatch.setattr(asyncio, "wait_for", fake_wait)
    monkeypatch.setattr(runtime_loops, "datetime", _DT)

    async def trigger():
        r = results[min(len(calls), len(results) - 1)]
        calls.append(r)
        return r

    await daily_report_loop(storage, cfg, trigger, stop)
    monkeypatch.setattr(asyncio, "wait_for", real_wait)
    return calls


async def test_loop_terminal_writes_state(storage: Storage, monkeypatch):
    _DT.fixed = datetime(2026, 10, 31, 7, 0, tzinfo=timezone(timedelta(hours=8)))
    try:
        calls = await _drain_loop(storage, make_cfg(), ["sent"], monkeypatch)
    finally:
        _DT.fixed = None
    assert len(calls) == 1
    date_key = "2026-10-31"
    assert await storage.get_state(f"daily_report:{date_key}") == "sent"


async def test_loop_send_failed_no_state_retry(storage: Storage, monkeypatch):
    _DT.fixed = datetime(2026, 10, 31, 7, 0, tzinfo=timezone(timedelta(hours=8)))
    try:
        calls = await _drain_loop(storage, make_cfg(), ["send_failed"], monkeypatch, cap=5)
    finally:
        _DT.fixed = None
    assert len(calls) >= 2
    assert await storage.get_state("daily_report:2026-10-31") is None


async def test_loop_already_done_skip(storage: Storage, monkeypatch):
    await storage.set_state("daily_report:2026-10-31", "sent")
    _DT.fixed = datetime(2026, 10, 31, 7, 0, tzinfo=timezone(timedelta(hours=8)))
    try:
        calls = await _drain_loop(storage, make_cfg(), ["sent"], monkeypatch)
    finally:
        _DT.fixed = None
    assert calls == []


async def test_loop_0701_no_fire(storage: Storage, monkeypatch):
    _DT.fixed = datetime(2026, 10, 31, 7, 1, tzinfo=timezone(timedelta(hours=8)))
    try:
        calls = await _drain_loop(storage, make_cfg(), ["sent"], monkeypatch)
    finally:
        _DT.fixed = None
    assert calls == []


# ───────────────────────────── 6.8 配置校验 ───────────────────────────────────


def test_config_fatal_errors_disable_only_daily():
    s = _mk_settings({"daily_report": {"time": "24:00", "page_size": 0}})
    cfg = resolve_daily_config(s)
    assert cfg.enabled is False
    assert cfg.errors
    assert s.snapshot == {}


def test_config_windows_not_ascending():
    s = _mk_settings({"daily_report": {"windows_days": [7, 1, 3]}})
    cfg = resolve_daily_config(s)
    assert cfg.enabled is False


def test_config_windows_not_divisible():
    s = _mk_settings({"daily_report": {"windows_days": [1, 3, 7], "bucket_seconds": 5000}})
    cfg = resolve_daily_config(s)
    assert cfg.enabled is False


def test_config_bucket_seconds_non_numeric_does_not_crash():
    s = _mk_settings({"daily_report": {"bucket_seconds": "abc"}})
    cfg = resolve_daily_config(s)
    assert cfg.enabled is False
    assert any("bucket_seconds" in e for e in cfg.errors)
    assert cfg.bucket_seconds == 7200


def test_config_bucket_seconds_zero_or_negative_does_not_crash():
    for bad in (0, -7200):
        s = _mk_settings({"daily_report": {"bucket_seconds": bad}})
        cfg = resolve_daily_config(s)
        assert cfg.enabled is False, bad
        assert any("bucket_seconds" in e for e in cfg.errors)
        assert cfg.bucket_seconds == 7200


def test_config_retention_days_non_numeric_tolerant():
    s = _mk_settings({"snapshot": {"retention_days": "abc"}, "daily_report": {}})
    cfg = resolve_daily_config(s)
    assert cfg.enabled is True


def test_config_retention_warning_not_fatal():
    s = _mk_settings({"snapshot": {"retention_days": 3}, "daily_report": {"windows_days": [1, 3, 7]}})
    cfg = resolve_daily_config(s)
    assert cfg.enabled is True
    assert any("保留期" in w for w in cfg.warnings)


def test_config_display_tz_inherit():
    s = _mk_settings({"display": {"timezone_offset_hours": 5}, "daily_report": {}})
    cfg = resolve_daily_config(s)
    assert cfg.timezone_offset_hours == 5


def test_config_defaults():
    cfg = resolve_daily_config(_mk_settings({}))
    assert cfg.enabled and cfg.time_hour == 7 and cfg.top_n == 0 and cfg.page_size == 20


# ───────────────────────────── 6.9 工厂装配 ────────────────────────────────────


def _ntfy_settings(platforms, ntfy_raw):
    return _mk_settings({"notify": {"platforms": platforms}, "ntfy": ntfy_raw})


def test_factory_daily_uses_daily_topic():
    from src.notify.factory import build_notifiers

    s = _ntfy_settings(["ntfy"], {"topic": "t", "snapshot_topic": "snap", "daily_topic": "polymarket-daily"})
    ns = build_notifiers(s, None, None, None, kind="daily")
    assert len(ns) == 1
    assert "polymarket-daily" in ns[0].describe()


def test_factory_daily_no_fallback():
    from src.notify.factory import build_notifiers

    s = _ntfy_settings(["ntfy"], {"topic": "t", "snapshot_topic": "snap"})
    ns = build_notifiers(s, None, None, None, kind="daily")
    assert ns == []


def test_factory_snapshot_fallback_intact():
    from src.notify.factory import build_notifiers

    s = _ntfy_settings(["ntfy"], {"topic": "t"})
    ns = build_notifiers(s, None, None, None, kind="snapshot")
    assert len(ns) == 1 and "/t" in ns[0].describe()


def test_factory_telegram_api_none_skips():
    from src.notify.factory import build_notifiers

    s = _mk_settings({"notify": {"platforms": ["telegram"]}})
    ns = build_notifiers(s, None, None, None, kind="daily")
    assert ns == []


# ───────────────────────────── 6.10 回归与指令 ────────────────────────────────


def test_fmt_shares_not_duplicated():
    src = Path("features/daily_report/renderer.py").read_text(encoding="utf-8")
    assert src.count("def _fmt_shares(") == 1


async def test_snap_handler_without_daily_returns_hint():
    handler = SnapshotCommandHandler(
        markets=[], collector=FakeCollector(), storage=None,
        trigger_snapshot=lambda: asyncio.sleep(0), health={},
    )
    out = await handler("/daily now", 1)
    assert out == "日报功能未接入"


async def test_snap_handler_daily_routes():
    async def trig():
        return "sent"
    health: dict[str, Any] = {}
    handler = SnapshotCommandHandler(
        markets=[], collector=FakeCollector(), storage=None,
        trigger_snapshot=lambda: asyncio.sleep(0), health={},
        trigger_daily=trig, daily_health=health,
    )
    out = await handler("/daily now", 1)
    assert out == "✅ 日报已推送" and health["daily_result"] == "sent"
    assert (await handler("/daily later", 1)) == "用法：/daily now"


async def test_daily_command_handler_routes():
    async def trig():
        return "sent_dup"
    h = DailyCommandHandler(trigger_daily=trig, daily_health={})
    assert "/help" in (await h("/help", 1)).lower() or "帮助" in (await h("/start", 1))
    assert await h("/daily now", 1) == "ℹ️ 正文与上一条完全一致，已抑制"
    assert await h("/daily xxx", 1) == "用法：/daily now"
    assert await h("/snap now", 1) == "未识别指令，发送 /help 查看可用指令"


def test_daily_result_reply_five_codes():
    assert daily_result_reply("sent").startswith("✅")
    assert daily_result_reply("degraded").startswith("⚠️")
    assert daily_result_reply("selfcheck_failed").startswith("❌")
    assert daily_result_reply("sent_dup").startswith("ℹ️")
    assert daily_result_reply("send_failed").startswith("❌")
    assert daily_result_reply("weird") == "日报结果：weird"
