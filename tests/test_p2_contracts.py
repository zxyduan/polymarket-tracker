"""P2 架构验收补充：paginator / channel deliver 契约。"""

import pytest

from framework.models.message import RenderedMessage
from framework.render.paginator import MessagePaginator, LengthPolicy


def test_p2_paginator_returns_list():
    p = MessagePaginator(LengthPolicy(max_chars=50, max_bytes=50))
    out = p.paginate(RenderedMessage(body="\n".join("l" * 80)))
    assert isinstance(out, list)
    assert all(isinstance(x, RenderedMessage) for x in out)
    assert len(out) >= 2


class _Chan:
    name = "x"
    def describe(self): return "x"
    async def deliver(self, m, ts):
        from framework.delivery.router import DeliveryResult
        return DeliveryResult(outcomes=())


@pytest.mark.asyncio
async def test_p2_channel_deliver_returns_result():
    from framework.delivery.router import ChannelRouter
    class _Prov:
        def targets(self): return []
    r = ChannelRouter([_Chan()], _Prov())
    res = await r.dispatch("f", RenderedMessage(body="b"))
    assert res.configured_channels == 1
