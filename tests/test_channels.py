"""framework/delivery 通道与路由单测（对应 T17/T18/T19）。"""

from __future__ import annotations

import asyncio

import unittest.mock as mock

import httpx
import pytest

from framework.delivery.router import ChannelRouter, ChannelFailure, DeliveryResult
from framework.delivery.targets import telegram_chat, ntfy_topic
from framework.delivery.telegram_channel import TelegramChannel
from framework.delivery.ntfy_channel import NtfyChannel
from framework.models.message import RenderedMessage


async def fake_post(self, url, content, headers):
    if "fail" in url:
        raise httpx.ConnectError("gone")
    class R:
        def raise_for_status(self): pass
    return R()


class FakeStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def record_notification(self, chat_id, text, keys, status):
        self.calls.append((chat_id, text, keys, status))


class FakeApi:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()

    async def send_message(self, chat_id, text, reply_markup):
        if chat_id in self.fail:
            raise RuntimeError("boom")


class FakeAuth:
    def __init__(self, chats: list[str]) -> None:
        self._chats = chats

    def chats(self):
        return self._chats


def test_telegram_describe_and_target_outcomes():
    st = FakeStorage()
    ch = TelegramChannel(FakeApi(fail={"2"}), st, FakeAuth(["1", "2"]))
    assert ch.describe() == "telegram@2 chats"

    msg = RenderedMessage(body="hi", event_keys=("trader",))
    res = asyncio.run(ch.deliver(msg, [telegram_chat("1"), telegram_chat("2")]))

    assert [o.status for o in res.outcomes] == ["success", "failed"]
    assert res.outcomes[1].error_kind == "target"          # T19：target 级失败
    assert [c[0] for c in st.calls] == ["1", "2"]
    assert [c[3] for c in st.calls] == ["success", "failed"]  # telegram 双落痕


def test_ntfy_success_only_records_then_router_channel_failure():
    st = FakeStorage()
    ch = NtfyChannel("https://ntfy.sh", storage=st)
    assert ch.describe() == "ntfy@https://ntfy.sh"

    msg = RenderedMessage(body="n", body_format="text", event_keys=("daily_report",))
    with mock.patch.object(httpx.AsyncClient, "post", fake_post):
        with pytest.raises(httpx.ConnectError):
            asyncio.run(ch.deliver(msg, [ntfy_topic("ok"), ntfy_topic("fail")]))
        assert st.calls == [("https://ntfy.sh/ok", "n", "daily_report", "success")]  # ntfy 只落成功，失败上抛


class Prov:
    def __init__(self, targets): self._t = targets
    def targets(self): return self._t


def test_router_catches_channel_exception_with_reference():
    st = FakeStorage()
    bad = NtfyChannel("https://ntfy.sh", storage=st)

    async def boom(self, url, content, headers):
        raise httpx.ConnectError("down")
    with mock.patch.object(httpx.AsyncClient, "post", boom):
        router = ChannelRouter([bad], Prov([ntfy_topic("x")]))
        res = asyncio.run(router.dispatch("daily_report", RenderedMessage(body="x")))

    assert res.configured_channels == 1                  # T18：与 targets 是否成功无关
    assert len(res.channel_failures) == 1
    f = res.channel_failures[0]
    assert isinstance(f, ChannelFailure)
    assert f.channel == "ntfy"
    assert f.reference == "ntfy@https://ntfy.sh"         # T19：reference = describe()
