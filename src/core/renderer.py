"""消息渲染：标准事件 → Telegram HTML 消息。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

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


# ── 批量摘要（digest）渲染 ────────────────────────────────────────────────


def _fmt_duration(seconds: float) -> str:
    """秒数 → 中文时长，如 60s → 1分钟、90s → 1分30秒。"""
    total = int(seconds)
    if total < 60:
        return f"{total}秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分钟" if sec == 0 else f"{minutes}分{sec}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"


def _digest_label(wallet: str, label: str) -> str:
    return html_escape(label or ((wallet or "")[:6] + "…"))


def render_digest(
    bucket: Any,
    *,
    scope: str,
    window_seconds: float,
    reason: str,
    elapsed_seconds: float,
    tz_offset_hours: int,
    polygonscan_base: str,
) -> AlertMessage:
    """把一个摘要桶渲染成一条汇总消息。

    global：正文按地址分组；wallet：不按地址分组。
    不显示市场标题（event.title），也不放 polymarket 市场链接；
    仅保留 Polygonscan 链上按钮。
    reason：threshold=阈值提前触发；window=到点触发；shutdown/remove 同到点样式。
    """
    # 收集钱包 → 事件（保留出现顺序）
    wallet_events: dict[str, list[StandardEvent]] = {}
    labels: dict[str, str] = {}
    for wallet, label, events in bucket.groups:
        key = (wallet or "").lower()
        wallet_events.setdefault(key, []).extend(events)
        if label and not labels.get(key):
            labels[key] = label

    def _sort_key(e: StandardEvent) -> tuple[bool, datetime, int]:
        return (
            e.event_time is None,
            e.event_time or datetime.min.replace(tzinfo=timezone.utc),
            e.log_index or 0,
        )

    for evs in wallet_events.values():
        evs.sort(key=_sort_key)

    all_events = [e for evs in wallet_events.values() for e in evs]
    n_addr = len(wallet_events)
    n_events = len(all_events)

    duration = _fmt_duration(window_seconds)
    elapsed = int(round(elapsed_seconds))
    if scope == "global":
        if reason == "threshold":
            title = (
                f"⚡ 大单触发 · 窗口已用 {elapsed}s"
                f"（{n_addr} 个地址 · {n_events} 笔）"
            )
        else:
            title = (
                f"🧾 成交汇总 · {duration}"
                f"（{n_addr} 个地址 · {n_events} 笔）"
            )
    else:
        only_key = next(iter(wallet_events))
        label_text = _digest_label(only_key, labels.get(only_key, ""))
        if reason == "threshold":
            title = (
                f"⚡ {label_text} · 大单触发"
                f"（窗口 {elapsed}s，{n_events} 笔）"
            )
        else:
            title = f"🧾 {label_text} · {duration}成交汇总（{n_events} 笔）"

    lines: list[str] = []
    for key in sorted(wallet_events):
        evs = wallet_events[key]
        if scope == "global":
            lines.append(f"👤 <b>{_digest_label(key, labels.get(key, ''))}</b>")
            prefix = "  · "
        else:
            prefix = "· "
        for event in evs:
            lines.append(prefix + _trade_line(event))

    total_prefix = "全量合计" if scope == "global" else "合计"
    lines.append(
        f"{total_prefix}："
        + (f"{n_addr} 个地址 · " if scope == "global" else "")
        + f"{n_events} 笔 · {fmt_number(bucket.total_shares)} 份 · "
        f"≈${fmt_number(bucket.total_usdc)} · "
        f"买 {bucket.buy_count} 笔 / 卖 {bucket.sell_count} 笔"
    )

    if bucket.started_at is not None:
        local_tz = timezone(timedelta(hours=tz_offset_hours))
        start = bucket.started_at.astimezone(local_tz)
        span = elapsed_seconds if reason == "threshold" else window_seconds
        end = start + timedelta(seconds=span)
        lines.append(
            f"窗口 {start.strftime('%H:%M:%S')}–{end.strftime('%H:%M:%S')}"
            "（北京时间）"
        )

    # 按钮：仅 Polygonscan（最多 3 个不同 tx）；不放 polymarket 市场链接
    tx_hashes: list[str] = []
    for event in all_events:
        if event.tx_hash and event.tx_hash not in tx_hashes:
            tx_hashes.append(event.tx_hash)
    buttons: list[list[dict[str, str]]] = []
    tx_row = [
        {
            "text": "Polygonscan" if i == 0 else f"Polygonscan {i + 1}",
            "url": f"{polygonscan_base.rstrip('/')}/tx/{tx_hash}",
        }
        for i, tx_hash in enumerate(tx_hashes[:3])
    ]
    if tx_row:
        buttons.append(tx_row)

    return AlertMessage(
        title=title,
        html=title + "\n" + "\n".join(lines),
        event_keys=[e.dedupe_key for e in all_events],
        buttons=buttons,
    )
