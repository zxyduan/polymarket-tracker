"""解析层测试。"""

from __future__ import annotations

from decimal import Decimal

from src.core.models import REDEEM, TRADE
from src.core.parsing import (
    parse_data_activity,
    parse_data_trade,
    parse_rtds_message,
)
from tests.fixtures import samples as s


def test_rtds_single_trade() -> None:
    events = parse_rtds_message(s.RTDS_TRADE)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == TRADE
    assert event.wallet == s.W1
    assert event.side == "BUY"
    assert event.outcome == "Yes"
    assert event.price == Decimal("0.632")
    assert event.size == Decimal("1500")
    assert event.notional == Decimal("0.632") * Decimal("1500")
    assert event.tx_hash == "0xabc123"
    assert event.log_index == 7
    assert event.dedupe_key == "0xabc123:7"


def test_rtds_orders_matched_extracts_all_fills() -> None:
    events = parse_rtds_message(s.RTDS_ORDERS_MATCHED)
    wallets = {e.wallet for e in events}
    assert wallets == {s.W2, s.W3}
    maker_fills = [e for e in events if e.wallet == s.W2]
    assert len(maker_fills) == 2
    taker_fills = [e for e in events if e.wallet == s.W3]
    assert len(taker_fills) == 1
    # 同一 tx、不同 logIndex，键唯一
    keys = {e.dedupe_key for e in events}
    assert len(keys) == 3


def test_data_trade_item() -> None:
    event = parse_data_trade(s.DATA_TRADES_ITEM)
    assert event is not None
    assert event.source == "data_api"
    assert event.wallet == s.W5
    assert event.notional == Decimal("0.7") * Decimal("200")


def test_data_activity_trade() -> None:
    events = parse_data_activity(s.DATA_ACTIVITY_TRADE)
    assert len(events) == 1
    event = events[0]
    assert event.wallet == s.W4
    assert event.outcome == "No"
    assert event.tx_hash == "0x999"  # 外层字段已透传


def test_data_activity_redeem() -> None:
    events = parse_data_activity(s.DATA_ACTIVITY_REDEEM)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == REDEEM
    assert event.notional == Decimal("1000")


def test_data_activity_list() -> None:
    events = parse_data_activity(
        [s.DATA_ACTIVITY_TRADE, s.DATA_ACTIVITY_REDEEM]
    )
    assert len(events) == 2
