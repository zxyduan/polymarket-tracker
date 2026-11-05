"""TraderAlertUsecase 测试。"""

import asyncio

from framework.delivery.router import ChannelRouter, DeliveryResult
from framework.models.message import RenderedMessage
from features.trader.usecase import TraderAlertUsecase, TraderConfig


class _Prov:
    def targets(self): return []


class R:
    def render(self, e): return RenderedMessage(body="alert")


class NoneR:
    def render(self, e): return None


class Hash:
    def __init__(self, hit=False):
        self.hit = hit
        self.recorded = []

    async def matches(self, ns, key, body):
        return self.hit

    async def record(self, ns, key, body):
        self.recorded.append(body)


class Good:
    name = "telegram"
    def describe(self): return "t"
    async def deliver(self, m, ts): return DeliveryResult(outcomes=())


def test_handle_routes():
    uc = TraderAlertUsecase(
        renderer=R(), router=ChannelRouter([Good()], _Prov()),
        guard=None, config=None, health={},
    )
    result = asyncio.run(uc.handle({"x": 1}))
    assert result.channel_failures == ()
    assert uc._health["trader_failures"] == 0
    assert uc._health["dispatched"] == 1


def test_render_none_counted():
    uc = TraderAlertUsecase(
        renderer=NoneR(), router=ChannelRouter([Good()], _Prov()),
        guard=None, config=None, health={},
    )
    asyncio.run(uc.handle({"x": 1}))
    assert uc._health["render_none"] == 1
    assert uc._health["dispatched"] == 0


def test_body_hash_dedup_counted():
    h = Hash(hit=True)
    uc = TraderAlertUsecase(
        renderer=R(), router=ChannelRouter([Good()], _Prov()),
        guard=h, config=TraderConfig(dedup=True), health={},
    )
    asyncio.run(uc.handle({"dedupe_key": "k1"}))
    assert uc._health["dedup_hit"] == 1
    assert uc._health["dispatched"] == 0
    assert h.recorded == []
