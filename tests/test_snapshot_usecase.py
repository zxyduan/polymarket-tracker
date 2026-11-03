"""SnapshotPushUsecase 测试（T05/S-01/S-02）。"""

from __future__ import annotations

import asyncio

from framework.delivery.router import ChannelRouter, DeliveryResult
from framework.models.message import RenderedMessage
from features.market_snapshot.usecase import SnapshotPushConfig, SnapshotPushUsecase


class _Prov:
    def targets(self): return []


class FakeCatalog:
    def slugs(self): return ["a", "b"]


class FakeCollector:
    def __init__(self): self.backfilled = []
    async def resolve_market(self, slug): return (f"cid-{slug}", slug)
    async def backfill_prev_bucket(self, cid): self.backfilled.append(cid)
    async def fetch_live_prices(self, rows): return {"cid-a": 1.0}


class FakeQuery:
    def __init__(self, rows): self._rows = rows
    async def latest_complete_buckets(self, now_bucket): return self._rows


class FakeRenderer:
    def __init__(self, msg): self._msg = msg
    def render(self, rows, *, live_prices, now_bucket): return self._msg


class FakeHash:
    def __init__(self, hits=()): self.hits = set(hits); self.recorded = []
    async def matches(self, ns, key, body): return body in self.hits
    async def record(self, ns, key, body): self.recorded.append(body)


class GoodChannel:
    name = "telegram"
    def describe(self): return "telegram@1"
    async def deliver(self, msg, targets): return DeliveryResult(outcomes=())


def _build(**kw):
    return SnapshotPushUsecase(
        catalog=FakeCatalog(),
        collector=kw.get("collector", FakeCollector()),
        query=kw.get("query", FakeQuery([{"x": 1}])),
        renderer=kw.get("renderer", FakeRenderer(RenderedMessage(body="body-A"))),
        router=ChannelRouter([GoodChannel()], _Prov()),
        body_hash=kw.get("hash", FakeHash()),
        config=kw.get("config", SnapshotPushConfig(dedup=True, now_bucket=100)),
        health=kw.get("health", {}),
    )


def test_happy_path_records_hash():
    uc = _build()
    asyncio.run(uc.run())
    assert uc._collector.backfilled == ["cid-a", "cid-b"]
    assert uc._hash.recorded == ["body-A"]


def test_dedup_hit_skips():
    h = FakeHash(hits={"body-A"})
    uc = _build(hash=h)
    asyncio.run(uc.run())
    assert h.recorded == []


def test_empty_rows_early_return():
    uc = _build(query=FakeQuery([]))
    asyncio.run(uc.run())
    assert uc._hash.recorded == []
