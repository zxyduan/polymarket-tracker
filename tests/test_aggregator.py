"""聚合器测试。"""

from __future__ import annotations

import asyncio

from src.core.parsing import parse_rtds_message
from src.core.pipeline import Aggregator
from tests.fixtures import samples as s


async def test_aggregates_same_tx() -> None:
    flushed: list[list] = []

    async def on_flush(events: list) -> None:
        flushed.append(events)

    agg = Aggregator(0.1, on_flush)
    events = parse_rtds_message(s.RTDS_ORDERS_MATCHED)
    maker = [e for e in events if e.wallet == s.W2]
    for event in maker:
        await agg.add(event)

    await asyncio.sleep(0.3)
    assert len(flushed) == 1
    assert len(flushed[0]) == 2
    # logIndex 升序
    assert flushed[0][0].log_index == 2
    assert flushed[0][1].log_index == 3


async def test_distinct_tx_separate_groups() -> None:
    flushed: list[list] = []

    async def on_flush(events: list) -> None:
        flushed.append(events)

    agg = Aggregator(0.1, on_flush)
    e1 = parse_rtds_message(s.RTDS_TRADE)[0]
    await agg.add(e1)
    e2 = parse_rtds_message(
        {**s.RTDS_TRADE, "txHash": "0xffff", "logIndex": 1}
    )[0]
    await agg.add(e2)

    await asyncio.sleep(0.3)
    assert len(flushed) == 2
