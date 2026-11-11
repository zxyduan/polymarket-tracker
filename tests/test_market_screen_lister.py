"""GammaMarketLister 单测（设计 §10.2）。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from framework.sources.market_lister import GammaMarketLister, ListerFetchError


class FakeResp:
    def __init__(self, status: int, data: object) -> None:
        self.status_code = status
        self._data = data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=self)

    def json(self) -> object:
        return self._data


class FakeClient:
    def __init__(self, responses: list[FakeResp], repeat_last_on_oob: bool = False) -> None:
        self._responses = responses
        self._repeat = repeat_last_on_oob
        self.calls: list[dict] = []

    async def get(self, url: str, params: dict | None = None) -> FakeResp:
        self.calls.append(params or {})
        idx = len(self.calls) - 1
        if idx >= len(self._responses):
            return self._responses[-1] if self._repeat else FakeResp(200, [])
        return self._responses[idx]


def _market(cid: str) -> dict:
    return {"conditionId": cid, "volume24hr": "60000"}


def _page(n: int, start: int) -> list[dict]:
    return [_market(f"0x{start + i}") for i in range(n)]


async def test_paginates_and_stops_on_short_page():
    fake = FakeClient([FakeResp(200, _page(100, 0)), FakeResp(200, _page(3, 100))])
    lister = GammaMarketLister(client=fake, max_retries=1, page_interval_seconds=0)
    items = await lister.list_active_markets(page_limit=100, max_pages=4)
    assert len(items) == 103
    assert len(fake.calls) == 2


async def test_list_and_data_wrapper_compat():
    fake = FakeClient([
        FakeResp(200, _page(2, 0)),
        FakeResp(200, {"data": _page(2, 2)}),
    ])
    lister = GammaMarketLister(client=fake, max_retries=1, page_interval_seconds=0)
    items = await lister.list_active_markets(page_limit=2, max_pages=4)
    assert [m["conditionId"] for m in items] == ["0x0", "0x1", "0x2", "0x3"]


async def test_retries_after_429_then_success():
    fake = FakeClient([FakeResp(429, []), FakeResp(200, _page(2, 0))])
    lister = GammaMarketLister(client=fake, max_retries=2, page_interval_seconds=0)
    items = await lister.list_active_markets(page_limit=100, max_pages=4)
    assert len(items) == 2
    assert len(fake.calls) == 2  # 一次 429 重试后成功


async def test_4xx_not_retried_rejected():
    fake = FakeClient([FakeResp(400, [])], repeat_last_on_oob=True)
    lister = GammaMarketLister(client=fake, max_retries=2, page_interval_seconds=0)
    with pytest.raises(ListerFetchError):
        await lister.list_active_markets(page_limit=100, max_pages=4)
    assert len(fake.calls) == 2  # 重试耗尽后抛出


async def test_page_interval_sleeps_between_pages(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    fake = FakeClient([FakeResp(200, _page(100, 0)), FakeResp(200, _page(100, 100))])
    lister = GammaMarketLister(client=fake, max_retries=1, page_interval_seconds=0.2)
    await lister.list_active_markets(page_limit=100, max_pages=4)
    assert sleeps == [0.2, 0.2]
