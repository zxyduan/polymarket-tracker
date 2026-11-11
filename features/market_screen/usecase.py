"""MarketScreen 采集轮用例（设计 v3.5 §7，dev §4.7）。

编排：lister 拉原始条目 → parsing 解析 → 量阈值过滤 → 成交量排序 TopN →
三档布尔判定 → 组装候选行与运行元信息 → 落当日分区。

返回 `done`（有候选落库）/ `empty`（本轮零候选，仍落 done 运行行）/
`failed`（采集或落库异常，落 failed 运行行）。
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from framework.models.market_screen import CandidateMarket, ScreenRunMeta
from framework.parsing.market_screen_parsing import (
    PRESETS,
    evaluate_presets,
    parse_market,
    resolve_anchors,
)

logger = logging.getLogger(__name__)


class MarketScreenUsecase:
    """每日市场筛选采集轮用例；端口通过构造注入。"""

    def __init__(
        self,
        lister: Any,
        store: Any,
        *,
        config: Any,
    ) -> None:
        self._lister = lister
        self._store = store
        self._cfg = config

    async def run(self) -> str:
        cfg = self._cfg
        tz = timezone(timedelta(hours=int(cfg.timezone_offset_hours)))
        now = datetime.now(tz)
        run_date = now.date().isoformat()
        started_at = now.isoformat(timespec="seconds")
        started_monotonic = time.monotonic()

        try:
            raws = await self._lister.list_active_markets(
                page_limit=int(cfg.page_limit), max_pages=int(cfg.max_pages)
            )
            rows, counts, passed_count, pages_fetched = self._build_rows(
                raws, now=now, tz_offset=int(cfg.timezone_offset_hours),
                run_date=run_date,
            )
            finished_at = datetime.now(tz).isoformat(timespec="seconds")
            elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
            meta = ScreenRunMeta(
                run_date=run_date,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_ms=elapsed_ms,
                pages_fetched=pages_fetched,
                candidate_count=len(rows),
                horizon_preset=cfg.main_preset,
                pass_strict_count=counts["strict"],
                pass_balanced_count=counts["balanced"],
                pass_loose_count=counts["loose"],
                passed_count=passed_count,
                status="done",
                error="",
            )
            await self._store.replace_screen_day(run_date, rows, meta)
            return "empty" if not rows else "done"
        except Exception as exc:  # noqa: BLE001 - 轮次边界，需落 failed 行后重交调度层
            logger.exception("market screen 采集轮失败: %s", exc)
            finished_at = datetime.now(tz).isoformat(timespec="seconds")
            elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
            failed = ScreenRunMeta(
                run_date=run_date,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_ms=elapsed_ms,
                pages_fetched=0,
                candidate_count=0,
                horizon_preset=cfg.main_preset,
                pass_strict_count=0,
                pass_balanced_count=0,
                pass_loose_count=0,
                passed_count=0,
                status="failed",
                error=str(exc)[:500],
            )
            try:
                await self._store.replace_screen_day(run_date, [], failed)
            except Exception:  # noqa: BLE001 - failed 落库失败仅记录，不覆盖原异常
                logger.exception("failed 运行元信息落库失败")
            return "failed"

    def _build_rows(
        self,
        raws: list[dict[str, Any]],
        *,
        now: datetime,
        tz_offset: int,
        run_date: str,
    ) -> tuple[list[CandidateMarket], dict[str, int], int, int]:
        cfg = self._cfg
        anchors_by = {}
        for preset in PRESETS:
            # 自定义锚点为单组列表，整体覆盖主档模板；其余档位用内置模板。
            custom = cfg.custom_anchors if preset == cfg.main_preset else None
            anchors_by[preset] = resolve_anchors(preset, custom)

        parsed: list[CandidateMarket] = []
        for raw in raws:
            row = parse_market(raw, now=now, tz_offset_hours=tz_offset)
            if row is None:
                continue
            if row.volume_24h < float(cfg.min_value):
                continue
            parsed.append(row)

        parsed.sort(key=lambda r: r.volume_24h, reverse=True)
        parsed = parsed[: int(cfg.top_n)]

        rows: list[CandidateMarket] = []
        counts = {"strict": 0, "balanced": 0, "loose": 0}
        passed_count = 0
        rule_on = bool(cfg.horizon_enabled)

        persist_non_passed = bool(getattr(cfg, "persist_non_passed", True))

        for rank, base in enumerate(parsed, start=1):
            ev = None
            if not rule_on:
                # 规则整体关闭：候选过量阈值 + TopN 即通过，三档标志留 NULL。
                row = dataclasses.replace(
                    base,
                    run_date=run_date,
                    rank_no=rank,
                    horizon_preset=cfg.main_preset,
                    threshold_lo=None,
                    horizon_pass=0,
                    pass_strict=None,
                    pass_balanced=None,
                    pass_loose=None,
                    screen_passed=1,
                    screen_reason="volume_topn_only",
                    collected_ts=now.timestamp(),
                )
                passed = 1
            else:
                ev = evaluate_presets(
                    base.remaining_days,
                    base.minority_prob,
                    anchors_by,
                    record_all=bool(cfg.record_all_presets),
                    main_preset=cfg.main_preset,
                    clamp_min=float(cfg.clamp_min),
                    clamp_max=float(cfg.clamp_max),
                )
                passed = ev.horizon_pass
                row = dataclasses.replace(
                    base,
                    run_date=run_date,
                    rank_no=rank,
                    horizon_preset=cfg.main_preset,
                    threshold_lo=ev.threshold_lo,
                    horizon_pass=ev.horizon_pass,
                    pass_strict=ev.pass_strict,
                    pass_balanced=ev.pass_balanced,
                    pass_loose=ev.pass_loose,
                    screen_passed=passed,
                    screen_reason="pass" if passed == 1 else "neutral_band",
                    collected_ts=now.timestamp(),
                )

            # persist_non_passed=false：未过主档候选不落库（与持续性主场景冲突，建议保持 true）。
            if rule_on and not persist_non_passed and passed == 0:
                continue

            rows.append(row)
            passed_count += passed
            if ev is not None:
                counts["strict"] += ev.pass_strict or 0
                counts["balanced"] += ev.pass_balanced or 0
                counts["loose"] += ev.pass_loose or 0

        pages_fetched = min(
            int(cfg.max_pages),
            math.ceil(len(raws) / max(1, int(cfg.page_limit))),
        )
        return rows, counts, passed_count, pages_fetched
