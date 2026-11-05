"""Data API 兜底客户端参数测试。"""

from __future__ import annotations

from src.sources.data_api_client import DataApiClient


def test_trade_params_include_maker_rows() -> None:
    client = DataApiClient("https://data-api.polymarket.com", page_limit=50)

    params = client._trade_params("0xabc", None)

    assert params["user"] == "0xabc"
    assert params["limit"] == 50
    # 默认 taker_only=true 会漏掉 maker 侧成交；监控需要两侧都进入兜底对账。
    assert params["taker_only"] == "false"


def test_trade_params_preserve_cursor() -> None:
    client = DataApiClient("https://data-api.polymarket.com")

    params = client._trade_params("0xabc", "cursor-abc")

    assert params["cursor"] == "cursor-abc"
    assert params["taker_only"] == "false"
