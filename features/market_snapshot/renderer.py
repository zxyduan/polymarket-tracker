"""F2 快照总览渲染（§5.2）。

live_prices 注入、shares_display 归一、HTML 正文渲染。
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from framework.models.message import RenderedMessage

REGIME_CN = {"low": "低", "normal": "正常", "high": "高"}
TREND_ICON = {"up": "📈", "down": "📉", "flat": "➡️"}


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:+.1f}%"


def _fmt_price(value: float | None) -> str:
    if value is None or value <= 0:
        return "-"
    return f"{value:.4f}"


def _fmt_shares(value: float | None) -> str:
    if value is None or (isinstance(value, (int, float)) and value < 0):
        return "-"
    return f"{value:,.0f}"


def _bucket_amplitude(row: dict[str, Any]) -> float | None:
    try:
        high = float(row.get("price_high") or 0)
        low = float(row.get("price_low") or 0)
        open_ = float(row.get("price_open") or 0)
    except (TypeError, ValueError):
        return None
    if open_ <= 0 or high < low:
        return None
    return (high - low) / open_ * 100


class SnapshotSummaryRenderer:
    """快照总览渲染器。"""

    def __init__(self, *, tz_offset_hours: int = 8,
                 bucket_seconds: int = 7200) -> None:
        self._tz = tz_offset_hours
        self._bs = bucket_seconds

    def render(self, rows: list[Any], *, live_prices: Mapping[str, float],
               now_bucket: int | None = None) -> RenderedMessage | None:
        for r in rows:
            cid = str(r.get("condition_id") or "")
            if cid in live_prices:
                r["current_price"] = live_prices[cid]
            if "volume_shares_display" not in r:
                sh = r.get("volume_shares")
                r["volume_shares_display"] = float(sh) if sh is not None else -1.0

        if not rows:
            return None

        bucket_ts = min(int(r["bucket_ts"]) for r in rows)
        local_tz = timezone(timedelta(hours=self._tz))
        start_dt = datetime.fromtimestamp(bucket_ts, tz=local_tz)
        end_dt = datetime.fromtimestamp(bucket_ts + self._bs, tz=local_tz)
        window = f"{start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"

        lines: list[str] = [f"📊 市场快照 · {window}（时间）"]

        lag = (now_bucket - bucket_ts) // self._bs
        if lag >= 2:
            stale_hours = (lag - 1) * self._bs // 3600
            lines.append(
                f"⚠️ 数据延迟约 {stale_hours} 小时（最新闭合桶 {window}，采集异常，请查日志）"
            )

        for i, row in enumerate(rows, 1):
            trend = str(row.get("trend") or "")
            icon = TREND_ICON.get(trend, "➡️")
            trend_pct = _fmt_pct(row.get("trend_pct"))
            regime = REGIME_CN.get(str(row.get("vol_regime") or ""), "-")
            amp = _bucket_amplitude(row)
            amp_s = f"{amp:.1f}%" if amp is not None else "-"
            volume = _fmt_shares(row.get("volume_shares_display"))
            current = row.get("current_price")
            price = _fmt_price(current if current is not None else row.get("price_close"))
            label = html.escape(str(row.get("slug_id") or row.get("slug") or "?"))
            line = (
                f"{i}️⃣ {label}：量 {volume} 份"
                f" ｜ 波动 {regime}·{amp_s} ｜ 现价 {price} {icon}{trend_pct}"
            ).rstrip()
            if int(row["bucket_ts"]) != bucket_ts:
                row_start = datetime.fromtimestamp(
                    int(row["bucket_ts"]), tz=local_tz).strftime("%H:%M")
                row_end = datetime.fromtimestamp(
                    int(row["bucket_ts"]) + self._bs, tz=local_tz).strftime("%H:%M")
                line += f"（{row_start}-{row_end} 桶）"
            lines.append(line)

        body = "\n".join(lines)
        return RenderedMessage(body=body, event_keys=("market_snapshot",))
