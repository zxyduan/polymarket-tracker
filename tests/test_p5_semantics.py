"""P5: T16/T18/T19 验收测试。"""

import pytest

from framework.models.message import RenderedMessage
from framework.delivery.router import ChannelRouter, DeliveryResult, ChannelFailure


class _Prov:
    def targets(self):
        from framework.delivery.targets import DeliveryTarget
        return [DeliveryTarget(channel="fail", address="1")]


class _FailChan:
    name = "fail"
    def describe(self): return "fail-chan"
    async def deliver(self, m, ts): raise RuntimeError("boom")


# T18: F2 send_failed 语义 → channel_failures 非空
@pytest.mark.asyncio
async def test_t18_send_failed():
    r = ChannelRouter([_FailChan()], _Prov())
    res = await r.dispatch("f2", RenderedMessage(body="x"))
    assert res.configured_channels == 1
    assert len(res.channel_failures) == 1


# T19: F3 channel 落痕 → reference 记录
@pytest.mark.asyncio
async def test_t19_channel_trace():
    r = ChannelRouter([_FailChan()], _Prov())
    res = await r.dispatch("f3", RenderedMessage(body="x"))
    assert res.channel_failures[0].reference == "fail-chan"
