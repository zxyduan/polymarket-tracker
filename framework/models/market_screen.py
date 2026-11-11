"""Market Screen 边界 DTO（设计 v3.5 §6.3）。

跨层只传 frozen dataclass，不把松散 dict[str, Any] 从用例穿到存储层；
本模块仅允许 Python 标准库，不 import 业务包。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CandidateMarket:
    """采集候选行（usecase → 存储层，对应 market_screen_daily 一行）。"""

    run_date: str
    condition_id: str
    slug: str
    question: str
    outcomes_json: str
    end_date_iso: str | None
    end_date_raw: str
    remaining_days: int | None
    prob_yes: float | None
    prob_no: float | None
    minority_prob: float | None
    volume_24h: float
    volume_metric: str
    rank_no: int
    horizon_preset: str
    threshold_lo: float | None
    horizon_pass: int
    pass_strict: int | None
    pass_balanced: int | None
    pass_loose: int | None
    screen_passed: int
    screen_reason: str
    collected_ts: float


@dataclass(frozen=True)
class ScreenRunMeta:
    """运行元信息（对应 market_screen_run 一行）。"""

    run_date: str
    started_at: str
    finished_at: str | None
    elapsed_ms: int
    pages_fetched: int
    candidate_count: int
    horizon_preset: str
    pass_strict_count: int
    pass_balanced_count: int
    pass_loose_count: int
    passed_count: int
    status: str
    error: str


@dataclass(frozen=True)
class PersistentCandidateRow:
    """持续性查询结果行（对齐视图 C / 模板⑦-b 输出）。

    `max_streak` 仅模板⑧严格连续路径填充，其余路径为 None；⑧ 路径未返回
    的聚合列留空（0 或 None），不臆造均值/最佳名次。
    """

    condition_id: str
    slug: str
    question: str
    end_date_iso: str | None
    remaining_days: int | None
    prob_yes: float | None
    prob_no: float | None
    minority_prob: float | None
    hit_days: int
    row_count: int
    min_volume_24h: float
    avg_volume_24h: float
    max_volume_24h: float
    best_rank: int
    worst_rank: int
    latest_volume_24h: float
    latest_rank_no: int
    first_run_date: str
    last_run_date: str
    pass_strict: int | None
    pass_balanced: int | None
    pass_loose: int | None
    screen_passed: int
    max_streak: int | None = None


@dataclass(frozen=True)
class ScreenQueryResult:
    """只读二次筛选结果：结果行 + 收敛/截断/枚举回退等 warning 归集。"""

    rows: list[PersistentCandidateRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
