"""交易告警批量摘要（digest）测试。

覆盖终稿提示词第八节验收点：窗口攒批、份额/USDC 阈值提前触发（或关系）、
连续大单连续发、重置、global/wallet 分组、渲染合计、定时器不重置、
停机/移除清理、配置校验，以及 pipeline 层的小额单过滤与生命周期旁路。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.core.digest import DigestConfig, DigestManager
from src.core.models import StandardEvent
from src.core.pipeline import PipelineRunner
from src.storage import Storage
from tests.fixtures import samples as s

UTC = timezone.utc


def _trade(
    tx: str,
    wallet: str,
    side: str,
    price: str,
    size: str,
    notional: str,
    *,
    title: str = "市场A",
    slug: str = "market-a",
    cid: str = "0xaaaa",
    log_index: int = 0,
    when: datetime | None = None,
    outcome: str = "YES",
) -> StandardEvent:
    return StandardEvent(
        dedupe_key=f"tx:{tx}:{log_index}",
        source="rtds",
        wallet=wallet,
        event_type="TRADE",
        tx_hash=tx,
        log_index=log_index,
        event_time=when or datetime(2026, 10, 22, 6, 0, 0, tzinfo=UTC),
        side=side,
        outcome=outcome,
        price=Decimal(price),
        size=Decimal(size),
        notional=Decimal(notional),
        title=title,
        slug=slug,
        condition_id=cid,
    )


def _make_manager(
    queue: asyncio.Queue,
    *,
    enabled: bool = True,
    scope: str = "global",
    window: float = 0.3,
    shares: str = "0",
    usdc: str = "0",
) -> DigestManager:
    cfg = DigestConfig(
        enabled=enabled,
        scope=scope,
        window_seconds=window,
        flush_shares=Decimal(shares),
        flush_usdc=Decimal(usdc),
    )
    return DigestManager(
        cfg,
        queue,
        tz_offset_hours=8,
        polygonscan_base="https://polygonscan.com",
    )


# ── 窗口与阈值触发 ────────────────────────────────────────────────────────


async def test_window_flush_basic() -> None:
    """未到阈值：窗口到点恰好一条汇总，之后不发空摘要。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=0.3)
    await mgr.add(s.W1, "交易员A", [_trade("0x1", s.W1, "BUY", "0.6", "100", "60")])
    assert q.empty()
    msg = await asyncio.wait_for(q.get(), timeout=1.5)
    assert "成交汇总" in msg.html
    assert "1 个地址 · 1 笔" in msg.html
    assert "100 份" in msg.html and "≈$60" in msg.html
    # 再等一段时间，空窗不会再发
    await asyncio.sleep(0.5)
    assert q.empty()
    assert mgr.pending_keys() == []


async def test_no_event_no_message() -> None:
    """空桶永远不产生消息。"""
    q: asyncio.Queue = asyncio.Queue()
    _make_manager(q, window=0.1)
    await asyncio.sleep(0.4)
    assert q.empty()


async def test_flush_usdc_threshold_immediate() -> None:
    """累计 USDC 达到阈值立即发送（不用等窗口）。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=60, usdc="1000")
    await mgr.add(s.W1, "A", [_trade("0x1", s.W1, "BUY", "0.6", "1000", "600")])
    assert q.empty()
    await mgr.add(s.W2, "B", [_trade("0x2", s.W2, "BUY", "0.5", "1000", "500")])
    msg = await asyncio.wait_for(q.get(), timeout=0.5)
    assert "大单触发" in msg.html
    assert "2 个地址 · 2 笔" in msg.html
    assert "≈$1,100" in msg.html
    assert mgr.pending_keys() == []


async def test_flush_shares_threshold_immediate() -> None:
    """累计份额达到阈值立即发送。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=60, shares="2000")
    await mgr.add(s.W1, "A", [_trade("0x1", s.W1, "BUY", "0.6", "1500", "1")])
    assert q.empty()
    await mgr.add(s.W1, "A", [_trade("0x2", s.W1, "BUY", "0.6", "600", "1")])
    msg = await asyncio.wait_for(q.get(), timeout=0.5)
    assert "大单触发" in msg.html
    assert "2,100 份" in msg.html


async def test_threshold_or_relationship() -> None:
    """份额未到、USDC 先到，也应立即触发（或关系）。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=60, shares="2000", usdc="1000")
    await mgr.add(s.W1, "A", [_trade("0x1", s.W1, "BUY", "0.6", "1500", "950")])
    assert q.empty()
    await mgr.add(s.W1, "A", [_trade("0x2", s.W1, "BUY", "0.6", "100", "60")])
    msg = await asyncio.wait_for(q.get(), timeout=0.5)
    assert "大单触发" in msg.html
    assert "1,600 份" in msg.html  # 份额仅 1600，未到 2000
    assert "≈$1,010" in msg.html


async def test_single_big_order_immediate() -> None:
    """单笔大单立即发。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=60, usdc="1000")
    start = time.monotonic()
    await mgr.add(s.W1, "A", [_trade("0x9", s.W1, "BUY", "0.8", "2500", "2000")])
    msg = await asyncio.wait_for(q.get(), timeout=0.5)
    assert time.monotonic() - start < 0.5
    assert "大单触发" in msg.html
    assert "≈$2,000" in msg.html


async def test_four_consecutive_big_orders() -> None:
    """连续 4 笔超大单 → 4 条独立摘要，不合并、不延迟。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=60, usdc="1000")
    for i in range(4):
        tx = f"0xbig{i}"
        await mgr.add(
            s.W1,
            "A",
            [_trade(tx, s.W1, "BUY", "0.8", "2500", "2000", log_index=i)],
        )
    await asyncio.sleep(0)
    msgs = []
    for _ in range(4):
        msgs.append(await asyncio.wait_for(q.get(), timeout=0.5))
    assert q.empty()
    assert mgr.pending_keys() == []
    for i, msg in enumerate(msgs):
        assert "大单触发" in msg.html
        assert "1 个地址 · 1 笔" in msg.html
        # tx 体现在 Polygonscan 按钮 URL（正文不含 tx_hash）
        flat = [b for row in msg.buttons for b in row]
        assert any(b["url"].endswith(f"/tx/0xbig{i}") for b in flat)


async def test_reset_after_flush() -> None:
    """阈值 flush 后清零；下一条消息开新窗，重新计时。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=0.4, usdc="1000")
    await mgr.add(s.W1, "A", [_trade("0x1", s.W1, "BUY", "0.8", "2500", "2000")])
    first = await asyncio.wait_for(q.get(), timeout=0.5)
    assert "≈$2,000" in first.html
    # 新窗口的小单不立即发
    await mgr.add(s.W1, "A", [_trade("0x2", s.W1, "BUY", "0.5", "100", "50")])
    assert q.empty()
    second = await asyncio.wait_for(q.get(), timeout=1.5)
    assert "成交汇总" in second.html
    assert "≈$50" in second.html
    assert "2,000" not in second.html


async def test_timer_not_reset_by_later_events() -> None:
    """后续消息只累计、不重置计时：首条后约 0.4s 即到点。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=0.4)
    start = time.monotonic()
    await mgr.add(s.W1, "A", [_trade("0x1", s.W1, "BUY", "0.5", "100", "50")])
    await asyncio.sleep(0.25)
    await mgr.add(s.W1, "A", [_trade("0x2", s.W1, "BUY", "0.5", "100", "50")])
    await asyncio.wait_for(q.get(), timeout=1.5)
    elapsed = time.monotonic() - start
    assert 0.35 < elapsed < 0.6  # 若被重置会在 0.65s 后才发


async def test_shutdown_flush() -> None:
    """停机时未到期桶尽量补发。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=60)
    await mgr.add(s.W1, "A", [_trade("0x1", s.W1, "BUY", "0.5", "100", "50")])
    assert q.empty()
    await mgr.flush_all()
    msg = await asyncio.wait_for(q.get(), timeout=0.5)
    assert "成交汇总" in msg.html
    assert mgr.pending_keys() == []


# ── 分组与渲染 ────────────────────────────────────────────────────────────


async def test_global_scope_groups_addresses() -> None:
    """global：多地址合成一条，按地址分组，合计精确（Decimal）；不显示市场标题。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=0.3)
    await mgr.add(
        s.W1,
        "交易员A",
        [
            _trade("0x1", s.W1, "BUY", "0.5", "333.33", "333.33",
                   title="市场A", slug="mkt-a", cid="0xa1", log_index=1),
            _trade("0x2", s.W1, "SELL", "0.25", "200", "50",
                   title="市场B", slug="mkt-b", cid="0xb1", outcome="NO",
                   log_index=1),
        ],
    )
    await mgr.add(
        s.W2,
        "交易员B",
        [_trade("0x3", s.W2, "BUY", "0.5", "100", "50",
                title="市场A", slug="mkt-a", cid="0xa1", log_index=1)],
    )
    msg = await asyncio.wait_for(q.get(), timeout=1.5)
    assert "2 个地址 · 3 笔" in msg.html
    assert "交易员A" in msg.html and "交易员B" in msg.html
    # 不显示市场标题，也不出现 polymarket 市场链接
    assert "市场A" not in msg.html and "市场B" not in msg.html
    assert "polymarket.com/event" not in msg.html
    # 333.33 + 50 + 50 = 433.33；333.33 + 200 + 100 = 633.33 份
    assert "633.33 份" in msg.html
    assert "≈$433.33" in msg.html
    assert "买 2 笔 / 卖 1 笔" in msg.html


async def test_wallet_scope_independent_buckets() -> None:
    """wallet 模式：两个地址各一个桶、各一条消息，互不串数据。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=0.3, scope="wallet")
    await mgr.add(s.W1, "交易员A", [_trade("0x1", s.W1, "BUY", "0.5", "100", "50")])
    await mgr.add(s.W2, "交易员B", [_trade("0x2", s.W2, "BUY", "0.5", "200", "100")])
    msgs = [
        await asyncio.wait_for(q.get(), timeout=1.5),
        await asyncio.wait_for(q.get(), timeout=1.5),
    ]
    texts = [m.html for m in msgs]
    assert any("交易员A" in t and "交易员B" not in t for t in texts)
    assert any("交易员B" in t and "交易员A" not in t for t in texts)
    assert all("成交汇总" in t for t in texts)


async def test_digest_buttons() -> None:
    """按钮区：只保留 Polygonscan（≤3 个 tx），不放 polymarket 市场链接。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=0.3)
    await mgr.add(
        s.W1,
        "A",
        [
            _trade("0x1", s.W1, "BUY", "0.5", "100", "50",
                   slug="mkt-a", cid="0xa1"),
            _trade("0x2", s.W1, "BUY", "0.5", "100", "50",
                   slug="mkt-b", cid="0xb1"),
        ],
    )
    msg = await asyncio.wait_for(q.get(), timeout=1.5)
    flat = [btn for row in msg.buttons for btn in row]
    tx_urls = [b["url"] for b in flat if "polygonscan" in b["url"]]
    assert tx_urls == [
        "https://polygonscan.com/tx/0x1",
        "https://polygonscan.com/tx/0x2",
    ]
    # 不允许出现 polymarket 市场链接
    assert not any("polymarket.com/event" in b["url"] for b in flat)
    assert "polymarket.com" not in msg.html


async def test_digest_no_market_title() -> None:
    """摘要正文不出现市场标题（event.title / condition_id 均不显示）。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=0.3)
    await mgr.add(
        s.W1,
        "A",
        [
            _trade("0x1", s.W1, "BUY", "0.5", "100", "50",
                   title="Will X happen in 2026?", slug="mkt-a", cid="0xa1"),
            _trade("0x2", s.W1, "BUY", "0.5", "100", "50",
                   title=None, slug=None, cid="0xdeadbeef1234567890"),
        ],
    )
    msg = await asyncio.wait_for(q.get(), timeout=1.5)
    assert "Will X happen" not in msg.html
    assert "0xdeadbe" not in msg.html


async def test_discard_wallet() -> None:
    """wallet 模式移除地址：非空桶补发；global 模式 no-op。"""
    q: asyncio.Queue = asyncio.Queue()
    mgr = _make_manager(q, window=60, scope="wallet")
    await mgr.add(s.W2, "B", [_trade("0x1", s.W2, "BUY", "0.5", "100", "50")])
    await mgr.discard_wallet(s.W2)
    await asyncio.wait_for(q.get(), timeout=0.5)
    assert mgr.pending_keys() == []

    # global 模式 discard 不影响全局桶
    mgr2 = _make_manager(q, window=60, scope="global")
    await mgr2.add(s.W1, "A", [_trade("0x2", s.W1, "BUY", "0.5", "100", "50")])
    await mgr2.discard_wallet(s.W1)
    assert mgr2.pending_keys() == ["__all__"]
    await mgr2.flush_all()


# ── 配置解析与校验 ────────────────────────────────────────────────────────


def test_config_defaults_when_section_missing() -> None:
    cfg = DigestConfig.from_section({})
    assert cfg.enabled is False
    assert cfg.scope == "global"
    assert cfg.window_seconds == 60
    assert cfg.flush_shares == Decimal("0")
    assert cfg.flush_usdc == Decimal("0")


def test_config_invalid_window() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        DigestConfig.from_section({"enabled": True, "window_seconds": 4})


def test_config_negative_threshold() -> None:
    with pytest.raises(ValueError, match="flush_"):
        DigestConfig.from_section({"enabled": True, "flush_usdc": -1})


def test_config_invalid_scope() -> None:
    with pytest.raises(ValueError, match="scope"):
        DigestConfig.from_section({"enabled": True, "scope": "per-tx"})


def test_config_non_numeric() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        DigestConfig.from_section({"enabled": True, "window_seconds": "abc"})


# ── pipeline 接线 ─────────────────────────────────────────────────────────


class _DigestSettings:
    """最小 settings 替身（可配 digest/filters）。"""

    def __init__(
        self,
        *,
        digest: dict[str, Any] | None = None,
        min_notional: float = 0,
        event_types: list[str] | None = None,
    ) -> None:
        self._raw: dict[str, Any] = {
            "filters": {
                "event_types": event_types or ["TRADE"],
                "min_notional_usd": min_notional,
                "aggregate_window_seconds": 0.2,
            },
            "display": {"timezone_offset_hours": 8},
            "endpoints": {"polygonscan": "https://polygonscan.com"},
            "market_cache": {"ttl_hours": 24},
        }
        if digest is not None:
            self._raw["digest"] = digest

    def section(self, name: str) -> dict[str, Any]:
        return dict(self._raw.get(name, {}))

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class _Gamma:
    async def get_market(self, _storage: Any, _cid: str) -> tuple[None, None]:
        return None, None


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    st = Storage(str(tmp_path / "digest.db"))
    await st.init()
    await st.add_wallet(s.W1, "交易员A")
    await st.add_wallet(s.W2, "交易员B")
    return st


async def _run_pipeline(
    storage: Storage,
    settings: _DigestSettings,
) -> tuple[asyncio.Queue, asyncio.Queue, PipelineRunner, asyncio.Task]:
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    runner = PipelineRunner(in_q, out_q, storage, _Gamma(), settings)
    # 测试用短窗口（绕过生产 ≥5s 的校验）
    if settings.section("digest"):
        cfg = DigestConfig(
            enabled=True,
            scope=str(settings.section("digest").get("scope", "global")),
            window_seconds=float(
                settings.section("digest").get("window_seconds", 0.3)
            ),
            flush_shares=Decimal(
                str(settings.section("digest").get("flush_shares", 0))
            ),
            flush_usdc=Decimal(
                str(settings.section("digest").get("flush_usdc", 0))
            ),
        )
        runner.digest = DigestManager(cfg, out_q, tz_offset_hours=8)
    task = asyncio.create_task(runner.run())
    return in_q, out_q, runner, task


async def test_pipeline_digest_small_trade_excluded(storage: Storage) -> None:
    """min_notional 以下的小单不进摘要；大单到点正常汇总。"""
    settings = _DigestSettings(min_notional=100, digest={"enabled": True})
    in_q, out_q, runner, task = await _run_pipeline(storage, settings)
    # $50 小单（低于阈值）
    await in_q.put(_trade("0xsmall", s.W1, "BUY", "0.5", "100", "50"))
    # $600 大单
    await in_q.put(_trade("0xbig", s.W1, "BUY", "0.6", "1000", "600"))
    msg = await asyncio.wait_for(out_q.get(), timeout=2)
    assert "成交汇总" in msg.html
    assert "≈$600" in msg.html
    assert "$50" not in msg.html
    # 两笔都已落库（小单不推送但仍入库）
    row = await storage._fetchone("SELECT COUNT(*) AS c FROM events")
    assert row is not None and row["c"] == 2
    await runner.shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_pipeline_digest_disabled_path(storage: Storage) -> None:
    """digest 不配置：逐笔路径行为不变（render_alert 样式）。"""
    settings = _DigestSettings()
    in_q, out_q, runner, task = await _run_pipeline(storage, settings)
    assert runner.digest.cfg.enabled is False
    await in_q.put(_trade("0x1", s.W1, "BUY", "0.632", "1500", "948"))
    msg = await asyncio.wait_for(out_q.get(), timeout=2)
    assert "交易员A" in msg.html and "买入" in msg.html
    assert "成交汇总" not in msg.html
    await runner.shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_pipeline_lifecycle_bypasses_digest(storage: Storage) -> None:
    """REDEEM 等生命周期事件不进摘要，逐笔即时推送。"""
    settings = _DigestSettings(
        event_types=["TRADE", "REDEEM"], digest={"enabled": True}
    )
    in_q, out_q, runner, task = await _run_pipeline(storage, settings)
    redeem = StandardEvent(
        dedupe_key="tx:0xred:1",
        source="data_api",
        wallet=s.W1,
        event_type="REDEEM",
        tx_hash="0xred",
        log_index=1,
        event_time=datetime(2026, 10, 22, 6, 0, 0, tzinfo=UTC),
        notional=Decimal("1000"),
        title="市场A",
        slug="mkt-a",
        condition_id="0xaaaa",
    )
    await in_q.put(redeem)
    msg = await asyncio.wait_for(out_q.get(), timeout=2)
    assert "REDEEM" in msg.html
    assert "成交汇总" not in msg.html and "大单触发" not in msg.html
    await runner.shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
