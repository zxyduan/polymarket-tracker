"""标准事件模型。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

# 成交事件类型常量
TRADE = "TRADE"
SPLIT = "SPLIT"
MERGE = "MERGE"
REDEEM = "REDEEM"
CONVERT = "CONVERT"
REWARD = "REWARD"
DEPOSIT = "DEPOSIT"


@dataclass
class StandardEvent:
    """跨数据源归一后的标准事件。"""

    dedupe_key: str
    source: str
    wallet: str
    event_type: str
    tx_hash: str | None = None
    log_index: int | None = None
    event_time: datetime | None = None
    side: str | None = None
    outcome: str | None = None
    price: Decimal | None = None
    size: Decimal | None = None
    notional: Decimal | None = None
    title: str | None = None
    slug: str | None = None
    condition_id: str | None = None
    external_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> str:
        """序列化为可入库 JSON。"""
        return json.dumps(
            {
                "dedupe_key": self.dedupe_key,
                "source": self.source,
                "wallet": self.wallet,
                "event_type": self.event_type,
                "tx_hash": self.tx_hash,
                "log_index": self.log_index,
                "event_time": self.event_time.isoformat() if self.event_time else None,
                "side": self.side,
                "outcome": self.outcome,
                "price": str(self.price) if self.price is not None else None,
                "size": str(self.size) if self.size is not None else None,
                "notional": str(self.notional) if self.notional is not None else None,
                "title": self.title,
                "slug": self.slug,
                "condition_id": self.condition_id,
                "external_id": self.external_id,
                "raw": self.raw,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_payload(cls, payload: str) -> "StandardEvent":
        """从入库 JSON 反序列化。"""
        data = json.loads(payload)
        return cls(
            dedupe_key=data["dedupe_key"],
            source=data["source"],
            wallet=data["wallet"],
            event_type=data["event_type"],
            tx_hash=data.get("tx_hash"),
            log_index=data.get("log_index"),
            event_time=datetime.fromisoformat(data["event_time"])
            if data.get("event_time")
            else None,
            side=data.get("side"),
            outcome=data.get("outcome"),
            price=Decimal(data["price"]) if data.get("price") is not None else None,
            size=Decimal(data["size"]) if data.get("size") is not None else None,
            notional=Decimal(data["notional"])
            if data.get("notional") is not None
            else None,
            title=data.get("title"),
            slug=data.get("slug"),
            condition_id=data.get("condition_id"),
            external_id=data.get("external_id"),
            raw=data.get("raw", {}),
        )

    def asdict(self) -> dict[str, Any]:
        return asdict(self)
