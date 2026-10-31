"""每日成交份额汇总（daily_report）测试。

覆盖施工文档 V2 §6 测试清单：存储聚合、合并、渲染纯函数、分段、单调自检、
调度状态机、成败判定（R4/B3 专项）、配置校验、工厂装配、指令与回归。
风格对齐基线 tests/：asyncio_mode=auto、tmp_path Storage fixture、monkeypatch 假通道。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import src.main as m
from src.main import (
    DailyReportConfig,
    _paginate_daily,
    daily_report_loop,
    trigger_daily_report,
)
from src.settings import DailyReportConfig as Cfg, MarketEntry, Settings, resolve_daily_config
from src.snapshots.commands import (
    DailyCommandHandler,
    SnapshotCommandHandler,
    daily_result_reply,
)
from src.snapshots.renderer import render_daily_volume_report
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


class TGSwallow:
    """模拟 TelegramNotifier：逐 chat 吞异常、内部落 success 痕、不抛出。"""

    def __init__(self, storage: Storage, chats: int = 1, fail_chats: int = 0):
        self.storage = storage
        self.chats = chats
        self.fail_chats = fail_chats
        self.sent: list[str] = []

    async def send(self, text: str, *, buttons=None, event_keys=None):
        keys = ",".join(event_keys or [])
        self.sent.append(text)
        ok = self.chats - self.fail_chats
        for i in range(ok):
            await self.storage.record_notification(f"c{i}", text, keys, "success")
        for i in range(self.fail_chats):
            await self.storage.record_notification(
                f"bad{i}", text, keys, "failed"
            )

    def describe(self) -> str:
        return f"telegram@{self.chats} chats"


class NtfyThrow:
    """模拟 NtfyNotifier 失败：抛异常且不落痕（触发层 except 补记 failed）。"""

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str, *, buttons=None, event_keys=None):
        self.sent.append(text)
        raise RuntimeError("ntfy 503")

    def describe(self) -> str:
        return "ntfy@https://ntfy.sh/daily-topic"


class NtfyOk:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.sent: list[str] = []

    async def send(self, text: str, *, buttons=None, event_keys=None):
        keys = ",".join(event_keys or [])
        self.sent.append(text)
        await self.storage.record_notification("ntfy", text, keys, "success")

    def describe(self) -> str:
        return "ntfy@https://ntfy.sh/daily-topic"


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
    assert rows_a["0xaaa"]["shares"] == 12 * 100.0  # R0 行与 -1 行都被排除出 1d（在界外/不计和）
    assert rows_a["0xaaa"]["buckets"] >= 12
    assert "0xbbb" in rows_a

    three = await storage.volume_by_window(R0 - 3 * 86400, R0)
    a3 = next(r for r in three if r["condition_id"] == "0xaaa")
    # 1d 的 12 桶 + R0-13..14 两桶(-1 计 0、0 计和) = 1200 + 0 + 0 = 1200
    assert a3["shares"] == 1200.0
    # -1 与 0 都计入 buckets：12 + 2 = 14
    assert a3["buckets"] == 14


async def test_volume_by_window_right_open(storage: Storage):
    await _snap(storage, "0xc", R0, 1.0)      # 恰好右端点，不纳入
    await _snap(storage, "0xc", R0 - 1, 2.0)  # 纳入
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
    rows = [
        _row("0xb", "B", 100, 200, 300),
        _row("0xa", "A", 100, 200, 300),  # 同 shares_1d，condition_id 升序应排前
        _row("0xc", "C", 500, 600, 700),
    ]
    rep = render_daily_volume_report(
        rows, windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=0
    )
    assert rep is not None
    assert rep.market_lines[0].startswith("1️⃣ C")  # 500 最大
    assert rep.total == 3
    # renderer 无段号概念（S9）
    assert not hasattr(rep, "segment") and not hasattr(rep, "page")
    assert render_daily_volume_report(
        [], windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=0
    ) is None


def test_renderer_text_redlines_and_thousands():
    rows = [_row("0xa", "a<b&c", 128540, 356208, 812940)]
    rep = render_daily_volume_report(
        rows, windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=0
    )
    assert "截至今晨06:00" in rep.title
    assert "07:00" not in rep.title.split("截至今晨06:00")[1] if "截至今晨06:00" in rep.title else True
    body = "\n".join(rep.market_lines)
    assert "128,540" in body and "356,208" in body and "812,940" in body
    # 文案红线：不得出现金额符号
    assert "$" not in body and "USDC" not in body and "USD" not in body
    # 市场名 HTML 转义
    assert "&lt;" in rep.market_lines[0]


def test_renderer_truncate():
    rows = [_row(f"0x{i}", f"M{i}", i * 10, i * 20, i * 30) for i in range(1, 8)]
    rep = render_daily_volume_report(
        rows, windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=5
    )
    assert len(rep.market_lines) == 5
    assert "其余 2 个市场未列出" == rep.truncated_note
    assert rep.total == 7
    rep0 = render_daily_volume_report(
        rows, windows_days=[1, 3, 7], now_bucket=R0, tz_offset_hours=8, top_n=0
    )
    assert rep0.truncated_note == ""


# ────────────────────────────── 6.4 分段 ──────────────────────────────────────


def test_paginate_25_by_20():
    title = "T"
    lines = [f"L{i}" for i in range(25)]
    segs = _paginate_daily(title, lines, "FOOTER", "", page_size=20)
    assert len(segs) == 2
    assert segs[0].startswith(title) and "（续" not in segs[0]
    assert "（续 2/2）" in segs[1]
    assert "FOOTER" in segs[1] and "FOOTER" not in segs[0]


def test_paginate_topn_and_page_no_conflict():
    lines = [f"L{i}" for i in range(25)]
    segs = _paginate_daily("T", lines, "F", "其余 20 个市场未列出", page_size=10)
    # 25 行按 10 一段 → 3 段；截断提示只在末段
    assert len(segs) == 3
    assert segs[-1].endswith("其余 20 个市场未列出")


def test_paginate_oversize_resplit():
    title = "T" * 2000
    lines = [f"市场{i}｜1天 1｜3天 2｜7天 3" * 300 for i in range(6)]
    segs = _paginate_daily(title, lines, "", "", page_size=100)
    assert len(segs) > 1  # 单段必超 4096 → 二分兜底


def test_paginate_empty_lines_single_segment():
    segs = _paginate_daily("T", [], "F", "", page_size=20)
    assert len(segs) == 1 and "F" in segs[0]


# ─────────────────────── 6.2/6.5/6.7 trigger 集成 ────────────────────────────


def _markets():
    return [
        MarketEntry(slug_id="A", slug="aaa", condition_id="0xAAA"),
        MarketEntry(slug_id="B", slug="bbb", condition_id=""),  # 需 resolve
    ]


async def _seed(storage: Storage):
    # A: 1d=100 (12桶), B(resolve→0xBBB): 1d=50
    await _snap(storage, "0xaaa", R0 - 7200, 100.0)
    await _snap(storage, "0xbbb", R0 - 7200, 50.0)


async def test_trigger_sent_and_dedup(storage: Storage, monkeypatch):
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)
    await _seed(storage)
    notifier = TGSwallow(storage)
    collector = FakeCollector(resolve_map={"bbb": "0xBBB"})
    health: dict[str, Any] = {}
    cfg = make_cfg(dedup=True)

    res = await trigger_daily_report(
        storage=storage, collector=collector, markets=_markets(),
        cfg=cfg, notifiers=[notifier], health=health,
    )
    assert res == "sent"
    assert health["daily_result"] == "sent"
    assert notifier.sent and "截至今晨06:00" in notifier.sent[0]

    # 第二次同数据 → sent_dup
    res2 = await trigger_daily_report(
        storage=storage, collector=collector, markets=_markets(),
        cfg=cfg, notifiers=[notifier], health=health,
    )
    assert res2 == "sent_dup"


async def test_trigger_selfcheck_reject(storage: Storage, monkeypatch):
    """注入人为逆序聚合结果 → 拒发（真实滚动窗口天然单调，只能伪造触发防御分支）。"""
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)

    async def fake_vbw(start: int, end: int):
        span = end - start
        val = {86400: 999.0, 259200: 500.0, 604800: 400.0}[span]
        return [{"condition_id": "0xaaa", "slug": "aaa", "slug_id": "A",
                 "shares": val, "buckets": 12}]

    monkeypatch.setattr(storage, "volume_by_window", fake_vbw)
    notifier = TGSwallow(storage)
    res = await trigger_daily_report(
        storage=storage, collector=FakeCollector(), markets=_markets()[:1],
        cfg=make_cfg(), notifiers=[notifier], health={},
    )
    assert res == "selfcheck_failed"
    assert notifier.sent == []  # 拒发，零发送


async def test_trigger_selfcheck_reject_second_pair(storage: Storage, monkeypatch):
    """第二对相邻档 3d > 7d 同样必须拒发（覆盖除 1d>3d 之外的逆序方向）。"""
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)

    async def fake_vbw(start: int, end: int):
        span = end - start
        val = {86400: 100.0, 259200: 100.0, 604800: 0.0}[span]
        return [{"condition_id": "0xaaa", "slug": "aaa", "slug_id": "A",
                 "shares": val, "buckets": 12}]

    monkeypatch.setattr(storage, "volume_by_window", fake_vbw)
    notifier = TGSwallow(storage)
    res = await trigger_daily_report(
        storage=storage, collector=FakeCollector(), markets=_markets()[:1],
        cfg=make_cfg(), notifiers=[notifier], health={},
    )
    assert res == "selfcheck_failed"
    assert notifier.sent == []


async def test_selfcheck_skipped_when_window_failed(storage: Storage, monkeypatch):
    """部分窗口聚合失败时跳过自检（缺失档按 0 计会形成假逆序，属已知限制非数据异常）。"""
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)
    real_vbw = storage.volume_by_window

    async def fake_vbw(start: int, end: int):
        if end - start == 604800:  # 7d 失败 → 该档按 0，会"大于"关系逆序
            raise RuntimeError("db lock")
        return await real_vbw(start, end)

    await _snap(storage, "0xaaa", R0 - 7200, 500.0)
    monkeypatch.setattr(storage, "volume_by_window", fake_vbw)
    notifier = TGSwallow(storage)
    res = await trigger_daily_report(
        storage=storage, collector=FakeCollector(), markets=_markets()[:1],
        cfg=make_cfg(), notifiers=[notifier], health={},
    )
    assert res == "degraded"  # 未被自检误拒，走降级发送


async def test_trigger_merge_zero_init_and_backfill_order(storage: Storage, monkeypatch):
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)
    # A 仅 1d 窗口有行（3d/7d 也含该桶故非零）；C 完全无行 → 三档 0 起步
    await _snap(storage, "0xaaa", R0 - 7200, 10.0, slug="aaa", slug_id="A")
    collector = FakeCollector(resolve_map={"bbb": "0xBBB", "ccc": "0xCCC"})
    notifier = TGSwallow(storage)
    markets = _markets() + [MarketEntry(slug_id="C", slug="ccc", condition_id="")]
    res = await trigger_daily_report(
        storage=storage, collector=collector, markets=markets,
        cfg=make_cfg(), notifiers=[notifier], health={},
    )
    assert res == "sent"
    # resolve 出的 cid 统一小写后补算
    assert set(collector.backfilled) == {"0xaaa", "0xbbb", "0xccc"}
    body = notifier.sent[0]
    assert "A" in body and "C" in body
    assert "1天 0｜3天 0｜7天 0" in body  # C 无行 → 三档 0 起步（决策⑦）


# ───────────────── 6.7 成败判定专项（R4 + B3 + B4，禁止回归） ──────────────────


async def test_r4_telegram_swallow_success_is_sent(storage: Storage, monkeypatch):
    """TelegramNotifier 吞异常但落 success 痕 → 判定 sent（不靠 try/except）。"""
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)
    await _seed(storage)
    tg = TGSwallow(storage, chats=2, fail_chats=1)  # 部分 chat 失败仍算成功
    res = await trigger_daily_report(
        storage=storage, collector=FakeCollector(resolve_map={"bbb": "0xBBB"}),
        markets=_markets(), cfg=make_cfg(), notifiers=[tg], health={},
    )
    assert res == "sent"


async def test_send_failed_when_all_channels_fail(storage: Storage, monkeypatch):
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)
    await _seed(storage)
    tg_allfail = TGSwallow(storage, chats=1, fail_chats=1)  # 全落 failed
    res = await trigger_daily_report(
        storage=storage, collector=FakeCollector(resolve_map={"bbb": "0xBBB"}),
        markets=_markets(), cfg=make_cfg(), notifiers=[tg_allfail], health={},
    )
    assert res == "send_failed"
    # send_failed 不写去重指纹（下轮可重试）
    assert await storage.get_state("daily_report:body_hash") is None


async def test_count_notifications_since_ownership(storage: Storage):
    """B3：其他任务（event_keys 不含 daily_report）的 success 痕不影响归属统计。"""
    w0 = await storage.latest_notification_id()
    await storage.record_notification("1", "digest", "digest_alert", "success")
    await storage.record_notification("2", "snap", "snap_key", "success")
    await storage.record_notification("3", "daily", "daily_report", "success")
    await storage.record_notification("4", "daily", "daily_report", "failed")
    counts = await storage.count_notifications_since(w0, "daily_report")
    assert counts == {"success": 1, "failed": 1}


async def test_b4_ntfy_throw_continues_other_segments(storage: Storage, monkeypatch):
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)
    # 造 3 段（page_size=1，delay=0）
    for i in range(3):
        await _snap(storage, f"0x{i}", R0 - 7200, 10.0 + i)
    markets = [MarketEntry(slug_id=f"M{i}", slug=f"s{i}", condition_id=f"0x{i}") for i in range(3)]
    thrower = NtfyThrow()
    ok = NtfyOk(storage)
    cfg = make_cfg(page_size=1)  # 每市场一段 → 3 段
    res = await trigger_daily_report(
        storage=storage, collector=FakeCollector(), markets=markets,
        cfg=cfg, notifiers=[thrower, ok], health={},
    )
    # thrower 每段抛异常被 except 捕获补记 failed，ok 每段落 success → sent
    assert res == "sent"
    assert len(thrower.sent) == 3  # 首段抛错未中断剩余段
    assert len(ok.sent) == 3


async def test_trigger_degraded_partial_window(storage: Storage, monkeypatch):
    """单窗口聚合异常 → 降级正文仍发送，结果码 degraded。"""
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)
    await _seed(storage)
    real_vbw = storage.volume_by_window

    async def flaky(start, end):
        if end - start == 604800:  # 7d 窗口
            raise RuntimeError("db lock")
        return await real_vbw(start, end)

    monkeypatch.setattr(storage, "volume_by_window", flaky)
    tg = TGSwallow(storage)
    res = await trigger_daily_report(
        storage=storage, collector=FakeCollector(resolve_map={"bbb": "0xBBB"}),
        markets=_markets(), cfg=make_cfg(), notifiers=[tg], health={},
    )
    assert res == "degraded"


async def test_trigger_no_channel_send_failed(storage: Storage, monkeypatch):
    monkeypatch.setattr(m, "_current_bucket_start", lambda: R0)
    await _seed(storage)
    res = await trigger_daily_report(
        storage=storage, collector=FakeCollector(resolve_map={"bbb": "0xBBB"}),
        markets=_markets(), cfg=make_cfg(), notifiers=[], health={},
    )
    assert res == "send_failed"


# ─────────────────────────── 6.6 调度状态机 ───────────────────────────────────


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
    monkeypatch.setattr(m, "datetime", _DT)

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
    assert len(calls) == 1  # 终值写 state 后不再触发
    date_key = "2026-10-31"
    assert await storage.get_state(f"daily_report:{date_key}") == "sent"


async def test_loop_send_failed_no_state_retry(storage: Storage, monkeypatch):
    _DT.fixed = datetime(2026, 10, 31, 7, 0, tzinfo=timezone(timedelta(hours=8)))
    try:
        calls = await _drain_loop(storage, make_cfg(), ["send_failed"], monkeypatch, cap=5)
    finally:
        _DT.fixed = None
    assert len(calls) >= 2  # send_failed 同分钟重试
    assert await storage.get_state("daily_report:2026-10-31") is None


async def test_loop_already_done_skip(storage: Storage, monkeypatch):
    await storage.set_state("daily_report:2026-10-31", "sent")
    _DT.fixed = datetime(2026, 10, 31, 7, 0, tzinfo=timezone(timedelta(hours=8)))
    try:
        calls = await _drain_loop(storage, make_cfg(), ["sent"], monkeypatch)
    finally:
        _DT.fixed = None
    assert calls == []  # 当日已完成 → 不触发


async def test_loop_0701_no_fire(storage: Storage, monkeypatch):
    _DT.fixed = datetime(2026, 10, 31, 7, 1, tzinfo=timezone(timedelta(hours=8)))
    try:
        calls = await _drain_loop(storage, make_cfg(), ["sent"], monkeypatch)
    finally:
        _DT.fixed = None
    assert calls == []  # 错过时刻不补发


# ───────────────────────────── 6.8 配置校验 ───────────────────────────────────


def test_config_fatal_errors_disable_only_daily():
    s = _mk_settings({"daily_report": {"time": "24:00", "page_size": 0}})
    cfg = resolve_daily_config(s)
    assert cfg.enabled is False
    assert cfg.errors  # 含 time 与 page_size 两条
    # 致命错误只禁用日报；快照节配置仍由 Settings 原样持有，不受日报解析影响
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
    """非数字 bucket_seconds 必须进 errors、禁用日报，不向启动流程抛 ValueError。"""
    s = _mk_settings({"daily_report": {"bucket_seconds": "abc"}})
    cfg = resolve_daily_config(s)
    assert cfg.enabled is False
    assert any("bucket_seconds" in e for e in cfg.errors)
    assert cfg.bucket_seconds == 7200  # 兜底默认值，避免后续取模再抛


def test_config_bucket_seconds_zero_or_negative_does_not_crash():
    """0 / 负数 bucket_seconds 必须进 errors，不触发 ZeroDivisionError。"""
    for bad in (0, -7200):
        s = _mk_settings({"daily_report": {"bucket_seconds": bad}})
        cfg = resolve_daily_config(s)
        assert cfg.enabled is False, bad
        assert any("bucket_seconds" in e for e in cfg.errors)
        assert cfg.bucket_seconds == 7200


def test_config_retention_days_non_numeric_tolerant():
    """快照节保留期非数字时容错回退 90，不抛异常、不阻断日报启用。"""
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


# ───────────────────────────── 6.9 工厂装配 ───────────────────────────────────


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
    assert ns == []  # 无 daily_topic 不回退


def test_factory_snapshot_fallback_intact():
    from src.notify.factory import build_notifiers

    s = _ntfy_settings(["ntfy"], {"topic": "t"})  # 无 snapshot_topic → 回退 topic（回归红线）
    ns = build_notifiers(s, None, None, None, kind="snapshot")
    assert len(ns) == 1 and "/t" in ns[0].describe()


def test_factory_telegram_api_none_skips():
    from src.notify.factory import build_notifiers

    s = _mk_settings({"notify": {"platforms": ["telegram"]}})
    ns = build_notifiers(s, None, None, None, kind="daily")  # telegram_api None → 跳过
    assert ns == []


# ───────────────────────────── 6.10 回归与指令 ────────────────────────────────


def test_fmt_shares_not_duplicated():
    src = Path("src/snapshots/renderer.py").read_text(encoding="utf-8")
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
