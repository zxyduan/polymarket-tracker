"""DailyReportUsecase 测试。"""

from __future__ import annotations

import asyncio

from framework.delivery.router import ChannelRouter, DeliveryResult
from framework.models.message import RenderedMessage
from framework.render.paginator import MessagePaginator
from features.daily_report.usecase import DailyReportConfig, DailyReportUsecase


class _Prov:
    def targets(self): return []


class _M:
    def __init__(self, cid): self.condition_id = cid


class Catalog:
    def markets(self): return [_M("CID-A"), _M("CID-B")]


class _WSum:
    def __init__(self, cid, shares): self.condition_id = cid; self.shares = shares


class Query:
    async def windows_sum(self, *, start_ts, end_ts, cid=None):
        return [_WSum("cid-a", 1.0), _WSum("cid-b", 2.0)]
    async def latest_complete_buckets(self, now): return []


class Renderer:
    def render(self, rows, **kw):
        return RenderedMessage(body="\n".join(r["condition_id"] for r in rows))


class Hash:
    def __init__(self): self.recorded = []
    async def matches(self, ns, k, b): return False
    async def record(self, ns, k, b): self.recorded.append(b)


class Good:
    name = "telegram"
    def describe(self): return "t"
    async def deliver(self, m, ts): return DeliveryResult(outcomes=())


def _run():
    uc = DailyReportUsecase(
        catalog=Catalog(), query=Query(), renderer=Renderer(),
        router=ChannelRouter([Good()], _Prov()), paginator=MessagePaginator(),
        body_hash=Hash(), config=DailyReportConfig(now_bucket=100), health={},
    )
    return asyncio.run(uc.run()), uc


def test_daily_happy():
    result, uc = _run()
    assert result == "sent"
    assert uc._health["daily_result"] == "sent"
    assert uc._hash.recorded
