"""price_watch 告警消息渲染。

输出 L2 `RenderedMessage`；本层不 import `src/`，HTML 转义 / 数值 / 时间
格式化在模块内自带（对齐 watchlist_alert / market_snapshot renderer 先例）。
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from framework.models.message import RenderedMessage
from framework.models.price_watch import PriceAlertEvent

logger = logging.getLogger(__name__)

# 事件码 → (图标, 中文方向)
_DIR_CN: dict[str, tuple[str, str]] = {
    "break_above": ("🔺", "上破"),
    "break_below": ("🔻", "下破"),
}
_KIND_CN: dict[str, str] = {
    "crypto": "Binance 现货",
    "stock": "Stooq 收盘价",
    "dex": "DEX 二级池价",
}


def _escape(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _price(value: float) -> str:
    return f"{value:g}"


def _fmt_time(event_time: datetime | None, offset_hours: int) -> str:
    if event_time is None:
        return "-"
    local = event_time.astimezone(timezone(timedelta(hours=offset_hours)))
    return local.strftime("%Y-%m-%d %H:%M")


class PriceAlertRenderer:
    """价位突破告警事件 → L2 消息。"""

    def __init__(self, *, tz_offset_hours: int = 8) -> None:
        self._tz = tz_offset_hours

    def render(self, event: PriceAlertEvent) -> RenderedMessage | None:
        try:
            return self._render(event)
        except Exception:
            logger.exception("price_watch 渲染失败 %s", event.symbol)
            return None

    def _render(self, event: PriceAlertEvent) -> RenderedMessage:
        icon, dir_cn = _DIR_CN.get(
            event.event_code, ("ℹ️", event.event_code)
        )
        subject = event.label or event.symbol
        title = f"{icon} {dir_cn} · {subject}"

        lines = [
            f"标的：{_escape(subject)}（{_escape(event.symbol)}）",
            f"方向：{dir_cn} 阈值 {_price(event.threshold)}",
            f"现价：{_price(event.price)} USD",
            f"口径：{_escape(_KIND_CN.get(event.price_kind, event.price_kind))} · {_escape(event.source)}",
        ]
        if event.category == "tokenized_stock":
            lines.append(
                "⚠️ 代币化标的，价格为 DEX 二级池成交价，非正股，"
                "含折价 / 溢价与薄池噪声"
            )
        if event.liquidity_usd is not None:
            lines.append(f"池流动性：≈${event.liquidity_usd:,.2f}")
        lines.append(f"时间：{_fmt_time(event.event_time, self._tz)}")

        body = _escape(title) + "\n" + "\n".join(lines)
        return RenderedMessage(
            body=body,
            body_format="html",
            title=title,
            event_keys=(f"{event.event_code}:{event.symbol}",),
        )
