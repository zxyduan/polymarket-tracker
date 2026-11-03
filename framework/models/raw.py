"""RawRecord 与 RecordKind（设计 v3.5 §9.1）。

适配器产出的最原始记录；kind 供 L2 路由分发，禁止跨层透传 dict。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class RecordKind(str, Enum):
    EVENT = "event"
    SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class RawRecord:
    source: str
    kind: RecordKind
    received_at: datetime
    payload: Mapping[str, Any]
