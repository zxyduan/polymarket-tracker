"""快照行/聚合 DTO（设计 v3.5 §3.2）。

边界只传 DTO，禁止 dict[str, Any] 穿过 L1/L2/L3。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WindowSum:
    condition_id: str
    slug: str
    slug_id: str
    shares: float          # SUM(volume_shares >= 0)，-1/缺失贡献 0
    buckets: int           # 命中行数，仅诊断
