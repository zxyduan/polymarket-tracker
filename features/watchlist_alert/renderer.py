"""白名单市场告警渲染（T4）。

输出 L2 `RenderedMessage`；本层不 import `src/`，HTML 转义 / 数值 / 时间
格式化在模块内自带（对齐 market_snapshot / daily_report renderer 先例）。
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from framework.models.message import RenderedMessage
from framework.models.watchlist_alert import MarketTradeAlert, RewardChangeEvent

# 事件码 → (图标, 文案)
_REWARD_TITLES: dict[str, tuple[str, str]] = {
    "exit": ("🚫", "奖励退出"),
    "floor_break": ("⚠️", "跌破准入线"),
    "decay_pct": ("📉", "奖励衰减"),
    "tier_drop": ("🔻", "档位下滑"),
    "rebound": ("🔁", "奖励恢复"),
}

_TIER_CN: dict[str, str] = {"high": "高", "mid": "中", "low": "低"}
_POLYGONSCAN_BASE = "https://polygonscan.com"


def _escape(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _price(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:.3f}".rstrip("0").rstrip(".")


def _number(value: Decimal | None) -> str:
    if value is None:
        return "-"
    if value == value.to_integral():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _side_cn(side: str | None) -> str:
    return {"BUY": "买入", "SELL": "卖出"}.get((side or "").upper(), side or "")


def _outcome(value: str | None) -> str:
    if not value:
        return ""
    upper = value.strip().upper()
    mapping = {"YES": "YES", "NO": "NO", "Y": "YES", "N": "NO"}
    return mapping.get(upper, upper)


def _fmt_time(event_time: datetime | None, offset_hours: int) -> str:
    if event_time is None:
        return "-"
    local = event_time.astimezone(timezone(timedelta(hours=offset_hours)))
    return local.strftime("%Y-%m-%d %H:%M")


def _tier_cn(tier: str | None) -> str:
    return _TIER_CN.get(tier, "-") if tier else "-"


class RewardChangeRenderer:
    """M1 奖励变化消息渲染。"""

    def render_batch(
        self,
        events: list[RewardChangeEvent],
        *,
        question: str = "",
        slug: str = "",
    ) -> RenderedMessage | None:
        """同市场一轮多事件合并为一条消息。"""
        if not events:
            return None
        first = events[0]
        icon, label = _REWARD_TITLES.get(
            first.event_code, ("ℹ️", first.event_code)
        )
        subject = question or slug or first.cid[:12]
        title = f"{icon} {label} · {subject}"

        lines: list[str] = []
        for ev in events:
            _, ev_label = _REWARD_TITLES.get(ev.event_code, ("ℹ️", ev.event_code))

            def _rate(value: float | None) -> str:
                return "-" if value is None else f"{value:g}"

            line = (
                f"· {ev_label}：速率 {_rate(ev.prev_rate)} → {_rate(ev.cur_rate)}"
                f"（档位 {_tier_cn(ev.prev_tier)} → {_tier_cn(ev.cur_tier)}）"
            )
            if ev.asset:
                line += f" {_escape(ev.asset)}"
            lines.append(line)
        body = _escape(title) + "\n" + "\n".join(_escape(line) for line in lines)

        buttons: tuple[Mapping[str, str], ...] = ()
        if slug:
            buttons = (
                ({"text": "市场页", "url": f"https://polymarket.com/market/{slug}"},),
            )
        return RenderedMessage(
            body=body,
            body_format="html",
            title=title,
            event_keys=tuple(f"{ev.cid}:{ev.event_code}" for ev in events),
            buttons=buttons,
        )

    def render(self, event: RewardChangeEvent) -> RenderedMessage:
        icon, label = _REWARD_TITLES.get(
            event.event_code, ("ℹ️", event.event_code)
        )
        subject = event.question or event.slug or event.cid[:12]
        title = f"{icon} {label} · {subject}"

        def _rate(value: float | None) -> str:
            return "-" if value is None else f"{value:g}"

        lines = [
            f"事件：{label}",
            f"速率：{_rate(event.prev_rate)} → {_rate(event.cur_rate)}"
            + (f" {_escape(event.asset)}" if event.asset else ""),
            f"档位：{_tier_cn(event.prev_tier)} → {_tier_cn(event.cur_tier)}",
        ]
        if event.prev_date or event.cur_date:
            lines.append(
                f"奖励期：{event.prev_date or '-'} → {event.cur_date or '-'}"
            )
        body = _escape(title) + "\n" + "\n".join(_escape(line) for line in lines)

        buttons: tuple[Mapping[str, str], ...] = ()
        if event.slug:
            buttons = (
                ({"text": "市场页", "url": f"https://polymarket.com/market/{event.slug}"},),
            )
        return RenderedMessage(
            body=body,
            body_format="html",
            title=title,
            event_keys=(f"{event.cid}:{event.event_code}",),
            buttons=buttons,
        )


class TradeAlertRenderer:
    """M2 同市场合并摘要渲染。"""

    def __init__(
        self,
        *,
        tz_offset_hours: int = 8,
        polygonscan_base: str = _POLYGONSCAN_BASE,
    ) -> None:
        self._tz = tz_offset_hours
        self._polygon = polygonscan_base.rstrip("/")

    def render(
        self, alerts: list[MarketTradeAlert], *, reason: str = "window"
    ) -> RenderedMessage | None:
        if not alerts:
            return None
        try:
            return self._render(alerts, reason=reason)
        except Exception:  # noqa: BLE001 - 渲染异常隔离，返回 None 由用例记日志
            return None

    def _render(
        self, alerts: list[MarketTradeAlert], *, reason: str
    ) -> RenderedMessage:
        events = [a.event for a in alerts]
        first = events[0]
        title_base = alerts[0].title or first.condition_id or "市场成交"
        buy = sum(1 for e in events if (e.side or "").upper() == "BUY")
        sell = len(events) - buy
        outcome = _outcome(first.outcome)

        if len(events) == 1:
            side_icon = "🔵" if (first.side or "").upper() == "BUY" else "🔴"
            title = f"{side_icon} {_side_cn(first.side)} {outcome} · {title_base}".strip()
        else:
            title = f"🔔 {title_base} · {len(events)} 笔成交（买 {buy} / 卖 {sell}）"

        lines: list[str] = []
        total_notional = Decimal(0)
        has_notional = False
        for i, alert in enumerate(alerts, 1):
            e = alert.event
            head = f"{_side_cn(e.side)} {_outcome(e.outcome)}".strip()
            detail = f"价格 {_price(e.price)} · 数量 {_number(e.size)}"
            if e.notional is not None:
                detail += f" · ≈${_number(e.notional)}"
                total_notional += e.notional
                has_notional = True
            lines.append(f"{i}) <b>{_escape(head)}</b> {detail}")
        lines.append(
            f"合计：{len(events)} 笔 · 买 {buy} / 卖 {sell}"
            + (f" · ≈${_number(total_notional)}" if has_notional else "")
        )
        lines.append(f"时间 {_fmt_time(first.event_time, self._tz)}")
        body = _escape(title) + "\n" + "\n".join(lines)

        # 按钮：市场页一行；Polygonscan 交易哈希最多 3 个，另起一行。
        rows: list[list[dict[str, str]]] = []
        slug = alerts[0].slug or first.slug
        if slug:
            rows.append(
                [{"text": "市场页", "url": f"https://polymarket.com/market/{slug}"}]
            )
        tx_hashes = [e.tx_hash for e in events if e.tx_hash][:3]
        if tx_hashes:
            rows.append(
                [
                    {"text": f"区块 {idx + 1}", "url": f"{self._polygon}/tx/{tx}"}
                    for idx, tx in enumerate(tx_hashes)
                ]
            )
        buttons: tuple[Mapping[str, Any], ...] = tuple(tuple(row) for row in rows)

        return RenderedMessage(
            body=body,
            body_format="html",
            title=title,
            event_keys=tuple(e.dedupe_key for e in events),
            buttons=buttons,
        )
