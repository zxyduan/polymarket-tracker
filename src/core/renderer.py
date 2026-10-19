"""消息渲染：标准事件 → Telegram HTML 消息。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta, timezone
from decimal import Decimal

from src.core.models import StandardEvent


@dataclass
class AlertMessage:
    """一条待推送消息。"""

    title: str
    html: str
    event_keys: list[str] = field(default_factory=list)
    buttons: list[list[dict[str, str]]] = field(default_factory=list)


def html_escape(text: str) -> str:
    """HTML 转义。"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def fmt_price(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def fmt_number(value: Decimal | None) -> str:
    if value is None:
        return "-"
    if value == value.to_integral():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def fmt_time(event: StandardEvent, offset_hours: int) -> str:
    if event.event_time is None:
        return "-"
    local = event.event_time.astimezone(timezone(timedelta(hours=offset_hours)))
    return local.strftime("%Y-%m-%d %H:%M")


def norm_outcome(value: str | None) -> str:
    if value is None:
        return ""
    upper = value.strip().upper()
    mapping = {"YES": "YES", "NO": "NO", "Y": "YES", "N": "NO", "是": "YES", "否": "NO"}
    return mapping.get(upper, upper)


def side_cn(side: str | None) -> str:
    return {"BUY": "买入", "SELL": "卖出"}.get((side or "").upper(), side or "")


def _event_headline(event: StandardEvent) -> str:
    return f"{side_cn(event.side)} {norm_outcome(event.outcome)}".strip()


def _trade_line(event: StandardEvent) -> str:
    headline = _event_headline(event)
    parts = [f"<b>{html_escape(headline)}</b>"]
    detail = f"价格 {fmt_price(event.price)} · 数量 {fmt_number(event.size)}"
    if event.notional is not None:
        detail += f" · ≈${fmt_number(event.notional)}"
    parts.append(detail)
    return " ".join(parts)


def render_alert(
    events: list[StandardEvent],
    *,
    label: str,
    tz_offset_hours: int,
    polygonscan_base: str,
) -> AlertMessage:
    """将一组（同 tx / 同聚合键）事件渲染为一条消息（不含市场标题）。"""
    first = events[0]
    is_trade = all(e.event_type == "TRADE" for e in events)
    label_text = html_escape(label or first.wallet[:6] + "…")

    if is_trade:
        if len(events) == 1:
            event = events[0]
            title = f"{'🔵' if event.side == 'BUY' else '🔴'} {label_text} · {_event_headline(event)}"
            lines = [
                f"价格 {fmt_price(event.price)} · 数量 {fmt_number(event.size)}"
                + (f" · 金额 ≈${fmt_number(event.notional)}" if event.notional is not None else ""),
                f"时间 {fmt_time(event, tz_offset_hours)}",
            ]
        else:
            title = f"🔔 {label_text} · 一笔结算含 {len(events)} 笔成交"
            lines = [f"{i}) {_trade_line(e)}" for i, e in enumerate(events, 1)]
            lines.append(f"时间 {fmt_time(first, tz_offset_hours)}")
    else:
        emoji = {"REDEEM": "🟢", "SPLIT": "🟡", "MERGE": "🟡", "CONVERT": "🔄"}.get(
            first.event_type, "ℹ️"
        )
        title = f"{emoji} {label_text} · {first.event_type}"
        lines = []
        for event in events:
            text = ""
            if event.notional is not None:
                text = f"金额 {fmt_number(event.notional)}"
            lines.append(f"• {text or '-'}")
        lines.append(f"时间 {fmt_time(first, tz_offset_hours)}")

    buttons: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    if first.tx_hash:
        row.append(
            {
                "text": "Polygonscan",
                "url": f"{polygonscan_base.rstrip('/')}/tx/{first.tx_hash}",
            }
        )
    if row:
        buttons.append(row)

    return AlertMessage(
        title=title,
        html=title + "\n" + "\n".join(lines),
        event_keys=[e.dedupe_key for e in events],
        buttons=buttons,
    )
