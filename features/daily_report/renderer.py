"""F3 日报渲染（§7）。

纯函数：不发送、不感知分段；输入合并后的三窗口份额行，输出 RenderedMessage。
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any

from framework.models.message import RenderedMessage


def _fmt_shares(value: float | None) -> str:
    """份额显示：0 = 真实零成交显示 0；-1/None = 未知显示 -。"""
    if value is None or (isinstance(value, (int, float)) and value < 0):
        return "-"
    return f"{value:,.0f}"


def _daily_index(i: int) -> str:
    """序号：1–9 用键帽 emoji，≥10 回退普通数字。"""
    if 1 <= i <= 9:
        return f"{i}️⃣"
    return f"{i}."


class DailyReportRenderer:
    """日报渲染器：rows → RenderedMessage | None。"""

    def render(self, rows: list[dict[str, Any]], *, windows_days: list[int],
               now_bucket: int, tz_offset_hours: int,
               top_n: int) -> RenderedMessage | None:
        if not rows:
            return None

        primary = f"shares_{windows_days[0]}d"
        ordered = sorted(
            rows,
            key=lambda r: (-float(r.get(primary, 0.0) or 0.0), str(r.get("condition_id", ""))),
        )
        total = len(ordered)

        truncated_note = ""
        if top_n > 0 and total > top_n:
            display = ordered[:top_n]
            truncated_note = f"其余 {total - top_n} 个市场未列出"
        else:
            display = ordered

        local_tz = timezone(timedelta(hours=tz_offset_hours))
        r_local = datetime.fromtimestamp(now_bucket, tz=local_tz)
        title = f"📈 每日成交份额汇总 · {r_local.month}月{r_local.day}日（截至今晨06:00）"

        market_lines: list[str] = []
        for i, row in enumerate(display, 1):
            label = html.escape(str(row.get("slug_id") or row.get("slug") or "?"))
            parts = [
                f"{d}天 {_fmt_shares(float(row.get(f'shares_{d}d', 0.0) or 0.0))}"
                for d in windows_days
            ]
            market_lines.append(f"{_daily_index(i)} {label}\n   " + "｜".join(parts))

        start_bucket = now_bucket - max(windows_days) * 86400
        footer = (
            f"共 {total} 个市场｜区间 "
            f"{datetime.fromtimestamp(start_bucket, tz=local_tz):%m-%d %H:%M} ~ "
            f"{r_local:%m-%d %H:%M}（UTC{tz_offset_hours:+d}）"
        )

        body = title + "\n" + "\n".join(market_lines)
        if footer:
            body += "\n" + footer
        if truncated_note:
            body += "\n" + truncated_note
        return RenderedMessage(body=body, title=title, event_keys=("daily_report",))
