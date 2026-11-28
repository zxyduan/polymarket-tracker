"""price_watch 渲染器测试：方向文案、口径标注、代币化风险提示。"""

from __future__ import annotations

from datetime import datetime, timezone

from features.price_watch.renderer import PriceAlertRenderer
from framework.models.message import RenderedMessage
from framework.models.price_watch import PriceAlertEvent


def _event(
    event_code: str = "break_above",
    category: str = "crypto",
    price_kind: str = "crypto",
    liquidity: float | None = None,
    source: str = "binance",
) -> PriceAlertEvent:
    return PriceAlertEvent(
        symbol="BTCUSDT",
        label="比特币",
        category=category,  # type: ignore[arg-type]
        event_code=event_code,  # type: ignore[arg-type]
        price=130.0,
        threshold=120.0,
        price_kind=price_kind,  # type: ignore[arg-type]
        source=source,
        event_time=datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc),
        liquidity_usd=liquidity,
    )


def test_break_above_renders_title_and_body() -> None:
    msg = PriceAlertRenderer().render(_event("break_above"))
    assert isinstance(msg, RenderedMessage)
    assert "上破" in msg.title
    assert "120" in msg.body and "130" in msg.body
    assert msg.event_keys == ("break_above:BTCUSDT",)
    assert msg.body_format == "html"


def test_break_below_renders_down_direction() -> None:
    msg = PriceAlertRenderer().render(_event("break_below"))
    assert msg is not None
    assert "下破" in msg.title
    assert msg.event_keys == ("break_below:BTCUSDT",)


def test_tokenized_stock_carries_non_positive_stock_notice() -> None:
    ev = _event(
        category="tokenized_stock", price_kind="dex", liquidity=1234567.0,
    )
    msg = PriceAlertRenderer().render(ev)
    assert msg is not None
    assert "非正股" in msg.body
    assert "DEX" in msg.body
    assert "1,234,567.00" in msg.body


def test_plain_stock_has_no_tokenized_notice() -> None:
    ev = _event(category="stock", price_kind="stock", source="stooq")
    msg = PriceAlertRenderer().render(ev)
    assert msg is not None
    assert "非正股" not in msg.body
