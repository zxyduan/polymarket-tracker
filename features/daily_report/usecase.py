"""F3 日报用例（§7）。

步骤：cid 解析 → backfill → windows_sum 聚合 → 单调自检 →
渲染 → paginator 分段 → body_hash 去重 → router 分发。
行为与 trigger_daily_report 对齐；具体渲染仍复用现有 renderer。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, Mapping, MutableMapping, Protocol

from framework.delivery.router import ChannelRouter
from framework.models.message import RenderedMessage
from framework.models.ports import SnapshotQuery
from framework.render.paginator import MessagePaginator
from framework.scheduler.guard import BodyHashGuard
from features.daily_report.ports import DailyRenderer

logger = logging.getLogger(__name__)


class Catalog(Protocol):
    def markets(self) -> list[Any]: ...


@dataclass(frozen=True)
class DailyReportConfig:
    windows_days: list[int] = field(default_factory=lambda: [7, 30, 90])
    timezone_offset_hours: int = 8
    top_n: int = 50
    page_size: int = 30
    dedup: bool = True
    now_bucket: int = 0


class DailyReportUsecase:
    def __init__(
        self, *,
        catalog: Catalog,
        query: SnapshotQuery,
        renderer: DailyRenderer,
        router: ChannelRouter,
        paginator: MessagePaginator,
        body_hash: BodyHashGuard,
        config: DailyReportConfig,
        health: MutableMapping[str, Any],
    ) -> None:
        self._catalog = catalog
        self._query = query
        self._renderer = renderer
        self._router = router
        self._paginator = paginator
        self._hash = body_hash
        self._cfg = config
        self._health = health

    async def run(self) -> str:
        R = self._cfg.now_bucket
        windows = self._cfg.windows_days

        # 1) windows_sum 聚合
        agg: dict[str, dict[int, float]] = {}
        failed = 0
        for d in windows:
            try:
                rows = await self._query.windows_sum(
                    start_ts=R - d * 86400, end_ts=R
                )
            except Exception as exc:
                failed += 1
                logger.error("日报窗口聚合失败 d=%s: %s", d, exc)
                continue
            for row in rows:
                agg.setdefault(row.condition_id.lower(), {})[d] = row.shares or 0.0

        # 2) 合并行
        out: list[dict] = []
        for m in self._catalog.markets():
            cid = (getattr(m, "condition_id", "") or "").lower()
            if not cid or cid not in agg:
                continue
            row = {"condition_id": cid}
            for d in windows:
                row[f"shares_{d}d"] = max(0.0, agg[cid].get(d, 0.0))
            out.append(row)

        # 3) 单调自检（仅全部窗口成功时）
        if failed == 0:
            for row in out:
                vals = [row[f"shares_{d}d"] for d in windows]
                if any(a > b for a, b in pairwise(vals)):
                    self._health["daily_result"] = "selfcheck_failed"
                    return "selfcheck_failed"

        # 4) 渲染
        msg = self._renderer.render(
            out, windows_days=windows, now_bucket=R,
            tz_offset_hours=self._cfg.timezone_offset_hours, top_n=self._cfg.top_n,
        )
        if msg is None:
            self._health["daily_result"] = "empty"
            return "sent_dup" if failed == 0 else "degraded"

        # 5) 分段
        pages = self._paginator.paginate(msg)

        # 6) dedup（按首段指纹）
        if self._cfg.dedup:
            if await self._hash.matches("daily", f"push:{R}", pages[0].body):
                self._health["daily_result"] = "sent_dup"
                return "sent_dup"

        # 7) 分发
        failures = 0
        configured = 0
        for p in pages:
            res = await self._router.dispatch("daily_report", p)
            failures += len(res.channel_failures)
            configured = res.configured_channels
        send_failed = failures > 0 or configured == 0
        result = "send_failed" if send_failed else ("degraded" if failed else "sent")
        self._health["daily_result"] = result

        if self._cfg.dedup and not send_failed:
            await self._hash.record("daily", f"push:{R}", pages[0].body)
        return result
