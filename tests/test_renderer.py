"""渲染器测试。"""

from __future__ import annotations

from decimal import Decimal

from src.core.parsing import parse_rtds_message, parse_data_activity
from src.core.renderer import render_alert
from tests.fixtures import samples as s

POLYSCAN = "https://polygonscan.com"


def test_render_single_trade() -> None:
    events = parse_rtds_message(s.RTDS_TRADE)
    alert = render_alert(
        events, label="交易员A", tz_offset_hours=8, polygonscan_base=POLYSCAN
    )
    assert "交易员A" in alert.html
    assert "买入" in alert.html
    assert "0.632" in alert.html
    assert "1,500" in alert.html
    assert len(alert.event_keys) == 1
    # 市场标题已去掉
    assert "Will X happen" not in alert.html
    # 只剩 Polygonscan 一个按钮
    assert len(alert.buttons[0]) == 1
    assert alert.buttons[0][0]["text"] == "Polygonscan"
    assert alert.buttons[0][0]["url"].endswith("/tx/0xabc123")


def test_render_aggregated() -> None:
    events = parse_rtds_message(s.RTDS_ORDERS_MATCHED)
    maker = [e for e in events if e.wallet == s.W2]
    alert = render_alert(
        maker, label="交易员B", tz_offset_hours=8, polygonscan_base=POLYSCAN
    )
    assert "2 笔成交" in alert.html
    assert "1)" in alert.html and "2)" in alert.html
    # 市场标题已去掉
    assert "Q1?" not in alert.html
    assert "Q2?" not in alert.html
    # 只保留 Polygonscan 按钮
    assert len(alert.buttons[0]) == 1
    assert alert.buttons[0][0]["text"] == "Polygonscan"


def test_render_redeem() -> None:
    events = parse_data_activity(s.DATA_ACTIVITY_REDEEM)
    alert = render_alert(
        events, label="交易员D", tz_offset_hours=8, polygonscan_base=POLYSCAN
    )
    assert "REDEEM" in alert.html
    assert "1,000" in alert.html
    # 市场标题已去掉
    assert "Q3?" not in alert.html


def test_html_escape_label() -> None:
    events = parse_rtds_message(s.RTDS_TRADE)
    alert = render_alert(
        events,
        label="<script>alert(1)</script> & x",
        tz_offset_hours=8,
        polygonscan_base=POLYSCAN,
    )
    assert "<script>" not in alert.html
    assert "&lt;script&gt;" in alert.html
    assert "&amp;" in alert.html


def test_number_formatting() -> None:
    from src.core.renderer import fmt_number, fmt_price

    assert fmt_number(Decimal("1234.0")) == "1,234"
    assert fmt_number(Decimal("1234.56")) == "1,234.56"
    assert fmt_price(Decimal("0.630")) == "0.63"
