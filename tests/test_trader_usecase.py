"""TraderAlertUsecase 测试。"""

import asyncio

from framework.delivery.router import ChannelRouter, DeliveryResult
from framework.models.message import RenderedMessage
from features.trader.usecase import TraderAlertUsecase


class _Prov:
    def targets(self): return []


class R:
    def render(self, e): return RenderedMessage(body="alert")


class Good:
    name = "telegram"
    def describe(self): return "t"
    async def deliver(self, m, ts): return DeliveryResult(outcomes=())


def test_handle_routes():
    uc = TraderAlertUsecase(
        renderer=R(), router=ChannelRouter([Good()], _Prov()),
        guard=None, config=None, health={},
    )
    asyncio.run(uc.handle({"x": 1}))
    assert uc._health["trader_failures"] == 0
