"""MarketScreenStore 门面（设计 v3.5 §6.4、§13.7，dev §4.5）。

包装 Storage：默认值回落、UTC+8 日期换算、参数收敛与 warning 归集、
原始行 dict → PersistentCandidateRow DTO 映射。`src.storage` 仅
TYPE_CHECKING 引入；门面不拼 SQL 文本、不暴露存储层 `query_persistent_rows`
细节给用例边界。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from framework.models.market_screen import (
    CandidateMarket,
    PersistentCandidateRow,
    ScreenQueryResult,
    ScreenRunMeta,
)

if TYPE_CHECKING:
    from src.storage import Storage


class MarketScreenStore:
    """usecase / 只读查询共用的存储门面。"""

    def __init__(self, storage: "Storage", *, config: Any) -> None:
        self._storage = storage
        self._cfg = config

    # ── 写入（透传存储层，单事务替换当日分区）──

    async def replace_screen_day(
        self,
        run_date: str,
        rows: list[CandidateMarket],
        meta: ScreenRunMeta,
    ) -> None:
        await self._storage.replace_screen_day(run_date, rows, meta)

    # ── 只读持续性查询 ──────────────────────────────────────────────────────

    def _default(self, key: str, fallback: Any) -> Any:
        defaults = getattr(self._cfg, "query_defaults", None)
        if defaults is None:
            return fallback
        if isinstance(defaults, dict):
            value = defaults.get(key)
        else:
            value = getattr(defaults, key, None)
        return fallback if value is None else value

    def _date_range(self, window_days: int, end_date: str | None) -> tuple[str, str]:
        tz = timezone(timedelta(hours=int(self._cfg.timezone_offset_hours)))
        if end_date:
            end = date.fromisoformat(end_date)
        else:
            end = datetime.now(tz).date()
        start = end - timedelta(days=max(1, window_days) - 1)
        return start.isoformat(), end.isoformat()

    async def query_persistent_candidates(
        self,
        *,
        window_days: int | None = None,
        min_hit_days: int | None = None,
        min_volume_24h: float | None = None,
        max_rank: int | None = None,
        min_remaining_days: int | None = None,
        prob_max: float | None = None,
        prob_side: str | None = None,
        require_consecutive: bool | None = None,
        end_date: str | None = None,
    ) -> ScreenQueryResult:
        warnings: list[str] = []

        # 默认值回落 + 非法收敛
        wd = self._coerce_int(window_days, self._default("window_days", 10), 1, warnings, "window_days")
        hit = self._coerce_int(min_hit_days, self._default("min_hit_days", wd), 1, warnings, "min_hit_days")
        hit = min(hit, wd)  # 命中天数不得超过窗口长度
        if min_hit_days is not None and min_hit_days != hit:
            warnings.append(f"min_hit_days 越界，已收敛为 {hit}")

        vol = self._coerce_float(
            min_volume_24h, self._default("min_volume_24h", 50000.0), 0.0, warnings, "min_volume_24h"
        )
        rank = self._coerce_int(max_rank, self._default("max_rank", 200), 1, warnings, "max_rank")
        remain = self._coerce_int(
            min_remaining_days, self._default("min_remaining_days", 31), 0, warnings, "min_remaining_days"
        )

        pmax = self._coerce_prob(prob_max, self._default("prob_max", 0.15), warnings)
        side_raw = prob_side if prob_side is not None else self._default("prob_side", "minority")
        side = "yes" if side_raw == "yes" else "minority"
        if side_raw not in ("yes", "minority"):
            warnings.append(f"prob_side 非法 {side_raw!r}，回退 minority")

        consecutive = require_consecutive
        if consecutive is None:
            consecutive = bool(self._default("require_consecutive", False))

        retention = int(getattr(self._cfg, "retention_days", 30) or 30)
        if wd > retention:
            warnings.append(
                f"window_days={wd} 超过 retention_days={retention}，早期历史已被清理，持续性可能被低估"
            )

        start_date, end_date_str = self._date_range(wd, end_date)
        rows = await self._storage.query_persistent_rows(
            start_date=start_date,
            end_date=end_date_str,
            max_rank=rank,
            min_hit_days=hit,
            min_volume_24h=vol,
            min_remaining_days=remain,
            prob_max=pmax,
            prob_side=side,
            require_consecutive=consecutive,
        )
        mapped = [self._map_row(row) for row in rows]
        return ScreenQueryResult(rows=mapped, warnings=warnings)

    # ── 参数收敛辅助 ─────────────────────────────────────────────────────────

    @staticmethod
    def _coerce_int(
        raw: Any, fallback: int, minimum: int, warnings: list[str], label: str
    ) -> int:
        if raw is None:
            return fallback
        try:
            value = int(raw)
        except (TypeError, ValueError):
            warnings.append(f"{label} 非法 {raw!r}，回退 {fallback}")
            return fallback
        if value < minimum:
            warnings.append(f"{label}={value} 低于下限 {minimum}，已收敛")
            return minimum
        return value

    @staticmethod
    def _coerce_float(
        raw: Any, fallback: float, minimum: float, warnings: list[str], label: str
    ) -> float:
        if raw is None:
            return fallback
        try:
            value = float(raw)
        except (TypeError, ValueError):
            warnings.append(f"{label} 非法 {raw!r}，回退 {fallback}")
            return fallback
        if value < minimum:
            warnings.append(f"{label}={value} 低于下限 {minimum}，已收敛")
            return minimum
        return value

    @staticmethod
    def _coerce_prob(raw: Any, fallback: float, warnings: list[str]) -> float:
        if raw is None:
            return fallback
        try:
            value = float(raw)
        except (TypeError, ValueError):
            warnings.append(f"prob_max 非法 {raw!r}，回退 {fallback}")
            return fallback
        if not (0.0 < value < 0.5):
            warnings.append(f"prob_max={value} 越界 (0,0.5)，回退 {fallback}")
            return fallback
        return value

    @staticmethod
    def _map_row(row: dict[str, Any]) -> PersistentCandidateRow:
        def _int(key: str) -> int:
            return int(row.get(key) or 0)

        def _float(key: str) -> float:
            return float(row.get(key) or 0.0)

        return PersistentCandidateRow(
            condition_id=str(row["condition_id"]),
            slug=str(row.get("slug") or ""),
            question=str(row.get("question") or ""),
            end_date_iso=row.get("end_date_iso"),
            remaining_days=row.get("remaining_days"),
            prob_yes=row.get("prob_yes"),
            prob_no=row.get("prob_no"),
            minority_prob=row.get("minority_prob"),
            hit_days=_int("hit_days"),
            row_count=_int("row_count"),
            min_volume_24h=_float("min_volume_24h"),
            avg_volume_24h=_float("avg_volume_24h"),
            max_volume_24h=_float("max_volume_24h"),
            best_rank=_int("best_rank"),
            worst_rank=_int("worst_rank"),
            latest_volume_24h=_float("latest_volume_24h"),
            latest_rank_no=_int("latest_rank_no"),
            first_run_date=str(row.get("first_run_date") or ""),
            last_run_date=str(row.get("last_run_date") or ""),
            pass_strict=row.get("pass_strict"),
            pass_balanced=row.get("pass_balanced"),
            pass_loose=row.get("pass_loose"),
            screen_passed=_int("screen_passed"),
            max_streak=row.get("max_streak"),
        )
