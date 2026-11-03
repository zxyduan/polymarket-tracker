"""LegacyCollectorAdapter 桥接测试。"""

import asyncio

from framework.adapters.legacy_collector import LegacyCollectorAdapter


class FakeCollector:
    async def resolve_market(self, slug): return ("cid-x", slug)
    async def backfill_prev_bucket(self, cid): self.cid = cid
    async def fetch_live_prices(self, rows): return {"a": 1.0}


def test_adapter_delegates():
    c = FakeCollector()
    a = LegacyCollectorAdapter(c)
    assert asyncio.run(a.resolve_market("s")) == ("cid-x", "s")
    asyncio.run(a.backfill_prev_bucket("x"))
    assert c.cid == "x"
    assert asyncio.run(a.fetch_live_prices([])) == {"a": 1.0}
