"""price_watch 采集源解析与失败隔离测试（mock httpx，不依赖外网）。"""

from __future__ import annotations

from typing import Any

import pytest

from framework.sources.price_feed import PriceFeedClient


class FakeResponse:
    def __init__(
        self,
        *,
        json_data: Any = None,
        text: str = "",
        status: int = 200,
        json_raises: bool = False,
    ) -> None:
        self.status_code = status
        self._json = json_data
        self.text = text
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("non-json")
        return self._json


class FakeAsyncClient:
    """按 URL 关键词返回预设响应；可注入异常模拟网络故障。"""

    def __init__(self, routes: dict[str, FakeResponse], fail: Exception | None = None) -> None:
        self.routes = routes
        self.fail = fail
        self.closed = False

    async def get(self, url: str, params: dict | None = None) -> FakeResponse:
        if self.fail is not None:
            raise self.fail
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return FakeResponse(status=404)

    async def aclose(self) -> None:
        self.closed = True


def _make_client(
    routes: dict[str, FakeResponse] | None = None,
    *,
    fail: Exception | None = None,
) -> tuple[PriceFeedClient, FakeAsyncClient]:
    fake = FakeAsyncClient(routes or {}, fail=fail)
    client = PriceFeedClient(
        max_retries=1,
        retry_backoff_seconds=0.0,
        request_interval_seconds=0.0,
        client=fake,  # type: ignore[arg-type]
    )
    return client, fake


# ── Binance ──────────────────────────────────────────────────────────────


async def test_binance_parses_last_price() -> None:
    routes = {
        "ticker/24hr": FakeResponse(json_data={"lastPrice": "120000.5"}),
    }
    client, _ = _make_client(routes)
    quote = await client.get_quote("BTCUSDT", "crypto")
    assert quote is not None
    assert quote.price == 120000.5
    assert quote.category == "crypto"
    assert quote.price_kind == "crypto"
    assert quote.source == "binance"


@pytest.mark.parametrize("bad", ["abc", "-5", "0", "", None, float("nan")])
async def test_binance_invalid_price_returns_none(bad: Any) -> None:
    routes = {"ticker/24hr": FakeResponse(json_data={"lastPrice": bad})}
    client, _ = _make_client(routes)
    assert await client.get_quote("BTCUSDT", "crypto") is None


async def test_binance_non_json_returns_none() -> None:
    routes = {"ticker/24hr": FakeResponse(json_raises=True)}
    client, _ = _make_client(routes)
    assert await client.get_quote("BTCUSDT", "crypto") is None


# ── Stooq ────────────────────────────────────────────────────────────────


async def test_stooq_parses_close() -> None:
    csv_text = (
        "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
        "nvda.us,2026-11-24,16:00,218,221,217,220.5,12345\n"
    )
    routes = {"q/l/": FakeResponse(text=csv_text)}
    client, _ = _make_client(routes)
    quote = await client.get_quote("NVDA", "stock")
    assert quote is not None
    assert quote.price == 220.5
    assert quote.category == "stock"
    assert quote.source == "stooq"


async def test_stooq_dirty_row_returns_none() -> None:
    routes = {"q/l/": FakeResponse(text="Symbol,Date,Time,Open,High,Low,Close,Volume\n")}
    client, _ = _make_client(routes)
    assert await client.get_quote("NVDA", "stock") is None


async def test_stooq_missing_close_column_returns_none() -> None:
    routes = {"q/l/": FakeResponse(text="A,B,C\n1,2,3\n")}
    client, _ = _make_client(routes)
    assert await client.get_quote("NVDA", "stock") is None


# ── DEX 代币化股票 ───────────────────────────────────────────────────────


async def test_dex_parses_pool_price_and_liquidity() -> None:
    payload = {
        "data": [
            {
                "attributes": {
                    "price_in_usd": "150.2",
                    "reserve_in_usd": "50000",
                    "quote_token": "WETH",
                }
            }
        ]
    }
    routes = {"pools": FakeResponse(json_data=payload)}
    client, _ = _make_client(routes)
    quote = await client.get_quote(
        "NVDAc", "tokenized_stock", contract="0xb20000000000000000000078ee7ce2fe4908108c"
    )
    assert quote is not None
    assert quote.price == 150.2
    assert quote.liquidity_usd == 50000
    assert quote.price_kind == "dex"
    assert quote.source == "geckoterminal"


async def test_dex_no_pool_returns_none() -> None:
    routes = {"pools": FakeResponse(json_data={"data": []})}
    client, _ = _make_client(routes)
    assert await client.get_quote("NVDAc", "tokenized_stock", contract="0xabc") is None


@pytest.mark.parametrize("bad", ["abc", "-1", "0", float("inf")])
async def test_dex_invalid_price_returns_none(bad: Any) -> None:
    payload = {"data": [{"attributes": {"price_in_usd": bad}}]}
    routes = {"pools": FakeResponse(json_data=payload)}
    client, _ = _make_client(routes)
    assert await client.get_quote("NVDAc", "tokenized_stock", contract="0xabc") is None


# ── 失败隔离 ────────────────────────────────────────────────────────────


async def test_network_failure_isolated_to_none() -> None:
    client, _ = _make_client(fail=RuntimeError("connection reset"))
    assert await client.get_quote("BTCUSDT", "crypto") is None


async def test_http_4xx_returns_none() -> None:
    routes = {"ticker/24hr": FakeResponse(status=404)}
    client, _ = _make_client(routes)
    assert await client.get_quote("BTCUSDT", "crypto") is None


async def test_http_5xx_exhausted_returns_none() -> None:
    routes = {"ticker/24hr": FakeResponse(status=503)}
    client, _ = _make_client(routes)
    assert await client.get_quote("BTCUSDT", "crypto") is None


async def test_unknown_category_returns_none() -> None:
    client, _ = _make_client({})
    assert await client.get_quote("FOO", "forex") is None


async def test_injected_client_not_closed_by_feed() -> None:
    client, fake = _make_client({})
    await client.aclose()
    assert fake.closed is False  # 注入客户端由调用方负责
