"""DailyReportUsecase 测试。

覆盖旧 trigger_daily_report 集成测试迁移过来的关键语义：
happy path / selfcheck_failed / sent_dup / send_failed / degraded。
"""

from __future__ import annotations

import asyncio

import pytest

from framework.delivery.router import ChannelRouter, DeliveryResult
from framework.models.message import RenderedMessage
from framework.render.paginator import MessagePaginator
from features.daily_report.usecase import DailyReportConfig, DailyReportUsecase


class _Prov:
    def targets(self): return []


class _M:
    def __init__(self, cid): self.condition_id = cid


class Catalog:
    def __init__(self, cids=("cid-a", "cid-b")):
        self._cids = cids
    def markets(self): return [_M(c) for c in self._cids]


class _WSum:
    def __init__(self, cid, shares): self.condition_id = cid; self.shares = shares


class Query:
    """可配置的假 windows_sum：按窗口天数返回不同 shares。

    windows_shares: dict[天数d -> shares值或Exception]；None 表示该窗口无行。
    """
    def __init__(self, windows_shares=None):
        self._ws = windows_shares or {1: 1.0, 3: 2.0, 7: 3.0}
    async def windows_sum(self, *, start_ts, end_ts, cid=None):
        span_days = (end_ts - start_ts) // 86400
        val = self._ws.get(span_days, 1.0)
        if isinstance(val, Exception):
            raise val
        if val is None:
            return []
        return [_WSum("cid-a", float(val)), _WSum("cid-b", float(val) * 2)]
    async def latest_complete_buckets(self, now, condition_ids=None): return []


class Renderer:
    def __init__(self, body=None): self._body = body
    def render(self, rows, **kw):
        if self._body is None:
            return RenderedMessage(body="\n".join(r["condition_id"] for r in rows))
        return RenderedMessage(body=self._body) if self._body else None


class Hash:
    def __init__(self, already_hit=False):
        self.recorded = []
        self._hit = already_hit
    async def matches(self, ns, k, b): return self._hit
    async def record(self, ns, k, b): self.recorded.append(b)


class Good:
    name = "telegram"
    def describe(self): return "t"
    async def deliver(self, m, ts): return DeliveryResult(outcomes=())


def _run(*, query=None, renderer=None, body_hash=None, channels=None,
         config=None, catalog=None):
    uc = DailyReportUsecase(
        catalog=catalog or Catalog(),
        query=query or Query(),
        renderer=renderer or Renderer(),
        router=ChannelRouter(channels if channels is not None else [Good()], _Prov()),
        paginator=MessagePaginator(),
        body_hash=body_hash or Hash(),
        config=config or DailyReportConfig(now_bucket=100, windows_days=[1, 3, 7]),
        health={},
    )
    return asyncio.run(uc.run()), uc


# ── happy path ──

def test_daily_happy():
    result, uc = _run()
    assert result == "sent"
    assert uc._health["daily_result"] == "sent"
    assert uc._hash.recorded


# ── 单调自检：逆序拒发 ──

def test_daily_selfcheck_reject():
    """7d < 3d < 1d 人为逆序 → selfcheck_failed，零发送。"""
    q = Query(windows_shares={1: 999.0, 3: 500.0, 7: 400.0})
    h = Hash()
    result, uc = _run(query=q, body_hash=h)
    assert result == "selfcheck_failed"
    assert uc._health["daily_result"] == "selfcheck_failed"
    assert not h.recorded  # 拒发不落指纹


def test_daily_selfcheck_second_pair_reject():
    """3d > 7d 第二对逆序同样拒发。"""
    q = Query(windows_shares={1: 100.0, 3: 100.0, 7: 0.0})
    result, _ = _run(query=q)
    assert result == "selfcheck_failed"


# ── 去重 ──

def test_daily_sent_dup():
    """Hash.matches 命中 → sent_dup，不发送不 record。"""
    h = Hash(already_hit=True)
    result, uc = _run(body_hash=h)
    assert result == "sent_dup"
    assert uc._health["daily_result"] == "sent_dup"
    assert not h.recorded


# ── 成败判定 ──

def test_daily_send_failed_no_channels():
    """configured_channels=0 → send_failed，不落指纹。"""
    h = Hash()
    result, uc = _run(channels=[], body_hash=h)
    assert result == "send_failed"
    assert not h.recorded


# ── 降级：部分窗口失败 ──

def test_daily_degraded_partial_window():
    """7d 窗口异常 → 跳过自检，降级发送 degraded。"""
    q = Query(windows_shares={1: 100.0, 3: 200.0, 7: RuntimeError("db lock")})
    result, uc = _run(query=q)
    assert result == "degraded"
    assert uc._health["daily_result"] == "degraded"


def test_daily_missing_markets_render_note():
    from features.daily_report.renderer import DailyReportRenderer

    renderer = DailyReportRenderer()
    msg = renderer.render(
        [{
            "condition_id": "cid-a",
            "slug_id": "a",
            "shares_1d": 1.0,
            "shares_3d": 2.0,
            "shares_7d": 3.0,
        }],
        windows_days=[1, 3, 7],
        now_bucket=100,
        tz_offset_hours=8,
        top_n=0,
        missing_markets=["b", "c"],
    )
    assert msg is not None
    assert "另有 2 个市场数据采集中：b、c" in msg.body

    collecting = renderer.render(
        [],
        windows_days=[1, 3, 7],
        now_bucket=100,
        tz_offset_hours=8,
        top_n=0,
        missing_markets=["b"],
    )
    assert collecting is not None
    assert "正在采集" in collecting.body

    empty = renderer.render(
        [],
        windows_days=[1, 3, 7],
        now_bucket=100,
        tz_offset_hours=8,
        top_n=0,
        missing_markets=[],
    )
    assert empty is None
