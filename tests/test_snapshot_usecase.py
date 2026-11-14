"""SnapshotPushUsecase 测试（T05/S-01/S-02）。"""

from __future__ import annotations

import asyncio
import time

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
    async def latest_complete_buckets(self, now_bucket, condition_ids=None): return self._rows


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
    status = asyncio.run(uc.run())
    assert status == "sent"
    assert uc._collector.backfilled == ["cid-a", "cid-b"]
    assert uc._hash.recorded == ["body-A"]
    assert uc._health["dedup_skipped"] == 0


def test_dedup_hit_skips():
    h = FakeHash(hits={"body-A"})
    uc = _build(hash=h)
    status = asyncio.run(uc.run())
    assert status == "dedup_skip"
    assert h.recorded == []
    assert uc._health["dedup_skipped"] == 1


def test_empty_rows_early_return():
    uc = _build(query=FakeQuery([]))
    status = asyncio.run(uc.run())
    assert status == "empty"
    assert uc._hash.recorded == []
    assert uc._health["empty_skipped"] == 1


def test_render_none_is_observable():
    uc = _build(renderer=FakeRenderer(None))
    status = asyncio.run(uc.run())
    assert status == "render_none"
    assert uc._health["render_none"] == 1
    assert uc._hash.recorded == []


class NoneResolveCollector(FakeCollector):
    async def resolve_market(self, slug):
        return None


def test_all_resolve_failed_returns_empty():
    uc = _build(collector=NoneResolveCollector())
    status = asyncio.run(uc.run())
    assert status == "empty"
    assert uc._health["empty_skipped"] == 1
    assert uc._collector.backfilled == []
    assert uc._hash.recorded == []


class DynamicQuery(FakeQuery):
    def __init__(self, rows):
        super().__init__(rows)
        self.seen_now = None

    async def latest_complete_buckets(self, now_bucket, condition_ids=None):
        self.seen_now = now_bucket
        self.seen_condition_ids = condition_ids
        return self._rows


def test_now_bucket_zero_uses_current_clock():
    current_bucket = (int(time.time()) // 7200) * 7200
    query = DynamicQuery([{"bucket_ts": current_bucket - 7200}])
    uc = _build(
        query=query,
        config=SnapshotPushConfig(dedup=False, now_bucket=0, bucket_seconds=7200),
    )
    status = asyncio.run(uc.run())
    assert status == "sent"
    assert query.seen_now is not None
    assert query.seen_now % 7200 == 0
    assert uc._health["last_push_bucket"] == query.seen_now
