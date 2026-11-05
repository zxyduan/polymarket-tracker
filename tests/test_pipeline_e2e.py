"""PipelineRunner 端到端测试（队列进、消息出、去重/聚合）。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.core.models import StandardEvent
from src.core.parsing import parse_rtds_message
from src.core.pipeline import PipelineRunner
from src.storage import Storage
from tests.fixtures import samples as s


class FakeSettings:
    """最小 settings 替身。"""

    def __init__(self) -> None:
        self._raw: dict[str, Any] = {
            "filters": {
                "event_types": ["TRADE"],
                "min_notional_usd": 0,
                "aggregate_window_seconds": 0.2,
            },
            "display": {"timezone_offset_hours": 8},
            "endpoints": {"polygonscan": "https://polygonscan.com"},
            "market_cache": {"ttl_hours": 24},
        }

    def section(self, name: str) -> dict[str, Any]:
        return dict(self._raw.get(name, {}))

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


@pytest.fixture
async def wired(tmp_path: Path) -> tuple[
    asyncio.Queue[StandardEvent],
    asyncio.Queue[Any],
    PipelineRunner,
]:
    storage = Storage(str(tmp_path / "e2e.db"))
    await storage.init()
    await storage.add_wallet(s.W2, "交易员B")

    in_q: asyncio.Queue[StandardEvent] = asyncio.Queue()
    out_q: asyncio.Queue[Any] = asyncio.Queue()

    class _Gamma:
        async def get_market(self, _storage: Any, _cid: str) -> tuple[None, None]:
            return None, None

    runner = PipelineRunner(in_q, out_q, storage, _Gamma(), FakeSettings())
    return in_q, out_q, runner


async def test_end_to_end_aggregation(
    wired: tuple[
        asyncio.Queue[StandardEvent],
        asyncio.Queue[Any],
        PipelineRunner,
    ]
) -> None:
    in_q, out_q, runner = wired
    task = asyncio.create_task(runner.run())

    events = parse_rtds_message(s.RTDS_ORDERS_MATCHED)
    maker = [e for e in events if e.wallet == s.W2]
    for event in maker:
        await in_q.put(event)

    alert = await asyncio.wait_for(out_q.get(), timeout=2)
    assert "2 笔成交" in alert.html
    assert len(alert.event_keys) == 2

    # 重复投递同一事件不会再产生消息
    await in_q.put(maker[0])
    assert out_q.empty()

    await runner.shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_trade_funnel_counts_missing_below_and_accepted(
    tmp_path: Path,
) -> None:
    storage = Storage(str(tmp_path / "funnel.db"))
    await storage.init()
    await storage.add_wallet(s.W2, "交易员B")

    class StrictSettings(FakeSettings):
        def __init__(self) -> None:
            super().__init__()
            self._raw["filters"]["min_notional_usd"] = 2

    class _Gamma:
        async def get_market(self, _storage: Any, _cid: str) -> tuple[None, None]:
            return None, None

    in_q: asyncio.Queue[StandardEvent] = asyncio.Queue()
    out_q: asyncio.Queue[Any] = asyncio.Queue()
    runner = PipelineRunner(in_q, out_q, storage, _Gamma(), StrictSettings())
    task = asyncio.create_task(runner.run())

    def make_event(key: str, notional: Decimal | None) -> StandardEvent:
        return StandardEvent(
            dedupe_key=key,
            source="test",
            wallet=s.W2,
            event_type="TRADE",
            notional=notional,
            condition_id="0xcid",
            side="BUY",
            outcome="Yes",
        )

    await in_q.put(make_event("none-key", None))
    await in_q.put(make_event("below-key", Decimal("1.50")))
    await in_q.put(make_event("accept-key", Decimal("2.00")))
    await asyncio.sleep(0.4)

    assert runner.funnel["notional_none"] == 1
    assert runner.funnel["notional_below"] == 1
    assert runner.funnel["accepted"] == 1

    await runner.shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
