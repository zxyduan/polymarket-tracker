"""bot 指令渲染：市场名单 + 成交量状态（/list、/vol）。

快照总览和日报渲染已迁到 features/，本文件只保留 bot 命令用的两个函数。
"""

from __future__ import annotations

import time
from typing import Any

from src.core.renderer import html_escape
from src.snapshots.collector import BUCKET_SIZE_SEC


def _fmt_shares(value: float | None) -> str:
    if value is None or (isinstance(value, (int, float)) and value < 0):
        return "-"
    return f"{value:,.0f}"


def render_market_list(markets: list[Any]) -> str:
    """市场名单（/list 指令）。"""
    if not markets:
        return "监控市场为空：编辑 config/markets.toml 后重启"
    lines = [f"<b>快照市场（{len(markets)}）</b>"]
    for i, m in enumerate(markets, 1):
        slug_id = html_escape(m.slug_id or m.slug)
        lines.append(f"{i}. {slug_id} · <code>{html_escape(m.slug)}</code>")
    return "\n".join(lines)


async def render_tracker_status(storage: Any, collector: Any) -> str:
    """成交量实时状态（/vol）。"""
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
        cur_sh: float | None = None
        if cid:
            try:
                cum = await collector._fetch_cumulative_volume(cid)
                base_cur = await storage.get_snap_baseline(cid, now_bucket)
                if cum is not None and base_cur is not None:
                    cur_sh = max(0.0, cum - base_cur)
            except Exception:  # noqa: BLE001
                cur_sh = None
        prev_sh = r.get("volume_shares")
        cur_s = f"{cur_sh:,.0f}" if cur_sh is not None and cur_sh > 0 else "0"
        prev_s = _fmt_shares(prev_sh)
        lines.append(f"{slug_id}｜{cur_s}｜{prev_s}")
    return "\n".join(lines)
