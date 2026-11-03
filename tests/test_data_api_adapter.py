"""DataApiAdapter 测试（T04）。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.storage import Storage
from framework.models.ports import CursorStore
from framework.sources import data_api_adapter as ada
from framework.store.state_store import StateStore


class FakeWallets:
    def active_addresses(self):
        return ["0xabc"]


def ev(ts: str):
    from src.core.models import StandardEvent
    return StandardEvent(
        dedupe_key="k", source="data_api", wallet="0xabc",
        event_type="buy", event_time=datetime.fromisoformat(ts),
    )


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    db = Storage(str(tmp_path / "test.db"))
    await db.init()
    return db


@pytest.fixture
async def states(storage) -> StateStore:
    return StateStore(storage)


def test_state_store_is_cursorstore(states):
    assert isinstance(states, CursorStore)


class _FakeFetch:
    async def __call__(self, addr, cursor):
        return ([1, 2], None)


async def test_reconcile_dedup_and_cursor_write(states, monkeypatch):
    a = ada.DataApiAdapter("https://api.example", states, FakeWallets())
    # 第一轮：两条事件，更新 last_time 为后者
    monkeypatch.setattr(ada, "parse_data_trades", lambda items: [ev("2026-11-01T00:00:00+00:00"), ev("2026-11-02T00:00:00+00:00")])
    monkeypatch.setattr(a, "fetch_trades", _FakeFetch())

    got = await a.reconcile_wallet("0xabc")
    assert [e.event_time.isoformat() for e in got] == [
        "2026-11-01T00:00:00+00:00", "2026-11-02T00:00:00+00:00"]
    assert await states.get("data_api", "0xabc:last_time") == "2026-11-02T00:00:00+00:00"

    # 第二轮：旧事件被去重，只出更新的一条
    monkeypatch.setattr(ada, "parse_data_trades", lambda items: [ev("2026-11-02T00:00:00+00:00"), ev("2026-11-03T00:00:00+00:00")])
    got2 = await a.reconcile_wallet("0xabc")
    assert [e.event_time.isoformat() for e in got2] == ["2026-11-03T00:00:00+00:00"]
    assert await states.get("data_api", "0xabc:last_time") == "2026-11-03T00:00:00+00:00"
