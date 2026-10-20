"""快照总览消息渲染（量 + 波动率 + 当前盘面价）。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from src.core.renderer import AlertMessage, html_escape
from src.snapshots.collector import BUCKET_SIZE_SEC

REGIME_CN = {"low": "低", "normal": "正常", "high": "高"}
TREND_ICON = {"up": "📈", "down": "📉", "flat": "➡️"}


def _fmt_volume(value: float | None) -> str:
    """成交量增量显示（USDC）：0 = 真实零成交显示 $0；无数据显示 -。"""
    if value is None:
        return "-"
    return f"${value:,.0f}"


def _fmt_shares(value: float | None) -> str:
    """份额显示：0 = 真实零成交显示 0；-1 = 未知（RTDS 未记账）显示 -。"""
    if value is None or (isinstance(value, (int, float)) and value < 0):
        return "-"
    return f"{value:,.0f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:+.1f}%"


def _fmt_price(value: float | None) -> str:
    """盘面价显示：价格即概率，取 4 位小数；缺失显示 -。"""
    if value is None or value <= 0:
        return "-"
    return f"{value:.4f}"


def _bucket_amplitude(row: dict[str, Any]) -> float | None:
    """桶内价格波动幅度（%）：(high - low) / open × 100。"""
    try:
        high = float(row.get("price_high") or 0)
        low = float(row.get("price_low") or 0)
        open_ = float(row.get("price_open") or 0)
    except (TypeError, ValueError):
        return None
    if open_ <= 0 or high < low:
        return None
    return (high - low) / open_ * 100


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
    row 可带 current_price（推送前实时拉取），缺失时降级用 price_close。
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
        amp = _bucket_amplitude(row)
        amp_s = f"{amp:.1f}%" if amp is not None else "-"
        volume = _fmt_shares(row.get("volume_shares_display"))
        current = row.get("current_price")
        price = _fmt_price(current if current is not None else row.get("price_close"))
        line = (
            f"{i}️⃣ {_market_label(row)}：量 {volume} 份"
            f" ｜ 波动 {regime}·{amp_s} ｜ 现价 {price} {icon}{trend_pct}"
        ).rstrip()
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


def _fmt_shares(value: float | None) -> str:
    """份额显示：0 = 真实零成交显示 0；-1 = 未知（RTDS 未记账）显示 -。"""
    if value is None or (isinstance(value, (int, float)) and value < 0):
        return "-"
    return f"{value:,.0f}"


async def render_tracker_status(storage: Any, collector: Any) -> str:
    """成交量实时状态（/vol，v10 基线差口径，免等 2h 验证链路）。

    Gamma volumeNum 官方口径为份额：
    - 当前桶增量 = 实时累计份额 - 当前桶起点基线份额（秒级验证，不写库）
    - 上一闭合桶 = 库内已补算份额（真实成交份额）
    """
    now_bucket = (int(time.time()) // BUCKET_SIZE_SEC) * BUCKET_SIZE_SEC
    rows = await storage.latest_complete_buckets(now_bucket)
    if not rows:
        return "<b>成交量状态</b>\n暂无快照数据（先跑一轮采集）"
    lines = [
        "<b>成交量状态</b>",
        "格式：市场｜当前桶(份)｜上一闭合桶(份)",
    ]
    for r in rows:
        cid = str(r.get("condition_id") or "")
        slug_id = html_escape(str(r.get("slug_id") or r.get("slug") or "?"))
        # 当前桶：实时累计份额 - 桶起点基线份额（免等 2h 验证链路）
        cur_sh: float | None = None
        if cid:
            try:
                cum = await collector._fetch_cumulative_volume(cid)
                base_cur = await storage.get_snap_baseline(cid, now_bucket)
                if cum is not None and base_cur is not None:
                    cur_sh = max(0.0, cum - base_cur)
            except Exception:  # noqa: BLE001 - 单市场失败降级
                cur_sh = None
        # 上一闭合桶：库内已结算份额
        prev_sh = r.get("volume_shares")
        cur_s = f"{cur_sh:,.0f}" if cur_sh is not None and cur_sh > 0 else "0"
        prev_s = _fmt_shares(prev_sh)
        lines.append(f"{slug_id}｜{cur_s}｜{prev_s}")
    return "\n".join(lines)
