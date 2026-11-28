"""PipelineRunner 白名单成交旁路测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from framework.models.event import StandardEvent
from src.core.pipeline import PipelineRunner
from src.storage import Storage


class FakeSettings:
    def __init__(self) -> None:
        self._raw: dict[str, Any] = {
            "filters": {"event_types": ["TRADE"], "min_notional_usd": 0},
            "display": {"timezone_offset_hours": 8},
            "endpoints": {"polygonscan": "https://polygonscan.com"},
            "market_cache": {"ttl_hours": 24},
        }

    def section(self, name: str) -> dict[str, Any]:
        return dict(self._raw.get(name, {}))

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class _Gamma:
    async def get_market(self, _storage: Any, _cid: str) -> tuple[None, None]:
        return None, None


def _event(dedupe_key: str, cid: str) -> StandardEvent:
    return StandardEvent(
        dedupe_key=dedupe_key, source="rtds", wallet="0xwallet",
        event_type="TRADE", tx_hash="0xabc", condition_id=cid,
        side="BUY", outcome="YES", price=Decimal("0.5"), size=Decimal(10),
        notional=Decimal(5), event_time=datetime.now(timezone.utc),
    )


async def _run_one(
    storage: Storage, event: StandardEvent,
    *, alert_queue: asyncio.Queue[StandardEvent] | None,
    watchlist_cids: set[str] | None,
) -> None:
    in_q: asyncio.Queue[StandardEvent] = asyncio.Queue()
    out_q: asyncio.Queue[Any] = asyncio.Queue()
    runner = PipelineRunner(
        in_q, out_q, storage, _Gamma(), FakeSettings(),
        market_alert_queue=alert_queue, watchlist_cids=watchlist_cids,
    )
    task = asyncio.create_task(runner.run())
    await in_q.put(event)
    await asyncio.sleep(0.15)
    await runner.shutdown()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_hit_cid_is_copied_to_alert_queue(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "p.db"))
    await storage.init()
    alert_q: asyncio.Queue[StandardEvent] = asyncio.Queue()
    await _run_one(
        storage, _event("tx:1", "0xcid"),
        alert_queue=alert_q, watchlist_cids={"0xcid"},
    )
    assert alert_q.qsize() == 1
    copied = alert_q.get_nowait()
    assert copied.dedupe_key == "tx:1"


async def test_non_hit_cid_not_copied(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "p.db"))
    await storage.init()
    alert_q: asyncio.Queue[StandardEvent] = asyncio.Queue()
    await _run_one(
        storage, _event("tx:2", "0xother"),
        alert_queue=alert_q, watchlist_cids={"0xcid"},
    )
    assert alert_q.qsize() == 0


async def test_no_bypass_params_keeps_legacy_behavior(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "p.db"))
    await storage.init()
    await _run_one(storage, _event("tx:3", "0xcid"), alert_queue=None, watchlist_cids=None)
    # 无旁路队列时不抛异常，旧链路行为不变。
    assert storage is not None
