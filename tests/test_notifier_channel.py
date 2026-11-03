"""NotifierChannel 适配测试。"""

import asyncio

from framework.adapters.notifier_channel import NotifierChannel
from framework.models.message import RenderedMessage


class Good:
    async def send(self, body): self.body = body
    def describe(self): return "tg"


class Bad:
    async def send(self, body): raise RuntimeError("boom")
    def describe(self): return "ntfy"


def test_good_deliver():
    c = NotifierChannel(Good())
    res = asyncio.run(c.deliver(RenderedMessage(body="x"), []))
    assert not res.channel_failures


def test_bad_captured():
    c = NotifierChannel(Bad())
    res = asyncio.run(c.deliver(RenderedMessage(body="x"), []))
    assert len(res.channel_failures) == 1
