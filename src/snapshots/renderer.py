"""快照总览消息渲染（精简：仅最新成交量 + 波动档）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.renderer import AlertMessage, html_escape

REGIME_CN = {"low": "低", "normal": "正常", "high": "高"}
TREND_ICON = {"up": "📈", "down": "📉", "flat": "➡️"}


def _fmt_volume(value: float | None) -> str:
    if value is None or value <= 0:
        return "-"
    return f"{value:,.0f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:+.1f}%"


def _market_label(row: dict[str, Any]) -> str:
    return html_escape(str(row.get("slug_id") or row.get("slug") or "?"))


def render_snapshot_summary(
    rows: list[dict[str, Any]],
    *,
    tz_offset_hours: int = 8,
    bucket_seconds: int = 7200,
) -> AlertMessage | None:
    """将最近完整桶渲染为一条总览；无数据返回 None。

    rows 来自 latest_complete_buckets()：每个市场一条（bucket_ts 升序）。
    若多个市场的桶时段不一致，取最早桶作为时段标题。
    """
    if not rows:
        return None

    # 桶时段（北京时间）；所有行 bucket_ts 相同则取其一，否则用最早桶
    bucket_ts = min(int(r["bucket_ts"]) for r in rows)
    local_tz = timezone(timedelta(hours=tz_offset_hours))
    start_dt = datetime.fromtimestamp(bucket_ts, tz=local_tz)
    end_dt = datetime.fromtimestamp(bucket_ts + bucket_seconds, tz=local_tz)
    window = f"{start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"

    lines: list[str] = [f"📊 市场快照 · {window}（北京时间）"]
    for i, row in enumerate(rows, 1):
        trend = str(row.get("trend") or "")
        icon = TREND_ICON.get(trend, "➡️")
        trend_pct = _fmt_pct(row.get("trend_pct"))
        regime = REGIME_CN.get(str(row.get("vol_regime") or ""), "-")
        volume = _fmt_volume(row.get("volume_delta"))
        line = f"{i}️⃣ {_market_label(row)}：量 {volume} ｜ 波动 {regime} {icon}{trend_pct}".rstrip()
        lines.append(line)

    body = "\n".join(lines)
    return AlertMessage(title=body, html=body, event_keys=[])


def render_market_list(markets: list[Any]) -> str:
    """市场名单（/list 指令）。"""
    if not markets:
        return "监控市场为空：编辑 config/markets.toml 后重启"
    lines = [f"<b>快照市场（{len(markets)}）</b>"]
    for i, m in enumerate(markets, 1):
        slug_id = html_escape(m.slug_id or m.slug)
        lines.append(f"{i}. {slug_id} · <code>{html_escape(m.slug)}</code>")
    return "\n".join(lines)
