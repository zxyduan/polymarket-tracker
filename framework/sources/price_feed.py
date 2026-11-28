"""price_watch 行情采集客户端。

按 `category` 分发到三类适配器：
- `crypto`：Binance 公开 24hr ticker；
- `stock`：Stooq CSV 报价；
- `tokenized_stock`：DEX 聚合（GeckoTerminal 按合约地址取最活跃池）。

只做协议翻译与脏值防护；单标的失败隔离返回 `None`，不抛异常、不拖垮整轮。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
from datetime import datetime, timezone
from typing import Any

import httpx

from framework.models.price_watch import Quote
from src.utils.retry import backoff_delay

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com"
STOOQ_BASE = "https://stooq.com"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com"


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头为秒数；非法格式返回 None。"""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _parse_positive_float(value: Any) -> float | None:
    """解析有限正数；空值 / 非数值 / NaN / inf / 负值返回 None。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


class PriceFeedClient:
    """聚合行情客户端：按类别分发适配器，持有共享 httpx 客户端。"""

    def __init__(
        self,
        *,
        binance_base: str = BINANCE_BASE,
        stooq_base: str = STOOQ_BASE,
        gecko_base: str = GECKOTERMINAL_BASE,
        timeout_seconds: int = 10,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        request_interval_seconds: float = 0.2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.binance_base = binance_base.rstrip("/")
        self.stooq_base = stooq_base.rstrip("/")
        self.gecko_base = gecko_base.rstrip("/")
        self.max_retries = max(1, max_retries)
        self.retry_backoff = retry_backoff_seconds
        self.request_interval = request_interval_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def get_quote(
        self, symbol: str, category: str, *, contract: str = ""
    ) -> Quote | None:
        """按类别取报价；失败 / 脏值返回 None。"""
        if category == "crypto":
            quote = await self._crypto_quote(symbol)
        elif category == "stock":
            quote = await self._stock_quote(symbol)
        elif category == "tokenized_stock":
            quote = await self._tokenized_quote(symbol, contract)
        else:
            logger.warning("price_watch 未知类别: %r", category)
            quote = None

        if self.request_interval > 0 and quote is not None:
            await asyncio.sleep(self.request_interval)
        return quote

    # ── HTTP 通用 ──────────────────────────────────────────────────────

    async def _get(
        self, url: str, params: dict[str, Any], label: str
    ) -> httpx.Response | None:
        """带 429/5xx 退避重试的 GET；重试耗尽 / 4xx 返回 None。"""
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.get(url, params=params)
            except Exception as exc:  # noqa: BLE001 - 网络/超时隔离本轮该标的
                logger.warning("行情请求失败 %s: %s", label, exc)
                if attempt == self.max_retries:
                    return None
                await asyncio.sleep(backoff_delay(attempt, self.retry_backoff, 30.0))
                continue

            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                delay = (
                    retry_after
                    if retry_after is not None
                    else backoff_delay(attempt, self.retry_backoff, 30.0)
                )
                logger.warning(
                    "行情 429 %s，%.1fs 后重试（%d/%d）",
                    label, delay, attempt, self.max_retries,
                )
                if attempt == self.max_retries:
                    return None
                await asyncio.sleep(min(delay, 30.0))
                continue

            if response.status_code >= 500:
                logger.warning(
                    "行情 5xx HTTP %s %s（%d/%d）",
                    response.status_code, label, attempt, self.max_retries,
                )
                if attempt == self.max_retries:
                    return None
                await asyncio.sleep(backoff_delay(attempt, self.retry_backoff, 30.0))
                continue

            if response.status_code >= 400:
                logger.warning("行情不可用 HTTP %s %s", response.status_code, label)
                return None

            return response
        return None

    # ── 适配器 ────────────────────────────────────────────────────────

    async def _crypto_quote(self, symbol: str) -> Quote | None:
        response = await self._get(
            f"{self.binance_base}/api/v3/ticker/24hr",
            {"symbol": symbol},
            symbol,
        )
        if response is None:
            return None
        try:
            payload = response.json()
        except ValueError:
            logger.warning("Binance 响应非 JSON: %s", symbol)
            return None
        if not isinstance(payload, dict):
            return None
        price = _parse_positive_float(payload.get("lastPrice"))
        if price is None:
            logger.warning("Binance 价格缺失或非法: %s", symbol)
            return None
        return Quote(
            symbol=symbol,
            label=symbol,
            category="crypto",
            price=price,
            price_kind="crypto",
            source="binance",
            fetched_at=datetime.now(timezone.utc),
            raw=payload,
        )

    async def _stock_quote(self, symbol: str) -> Quote | None:
        response = await self._get(
            f"{self.stooq_base}/q/l/",
            {"s": f"{symbol.lower()}.us", "f": "sd2t2ohlcv", "h": "", "e": "csv"},
            symbol,
        )
        if response is None:
            return None
        rows = list(csv.reader(io.StringIO(response.text)))
        rows = [row for row in rows if row and any(cell.strip() for cell in row)]
        if len(rows) < 2:
            logger.warning("Stooq 无有效行情行: %s", symbol)
            return None

        header = [cell.strip().lower() for cell in rows[0]]
        if "close" in header:
            close_idx = header.index("close")
        elif len(header) >= 5:
            close_idx = 4
        else:
            logger.warning("Stooq 缺 Close 列: %s", symbol)
            return None

        last = rows[-1]
        if close_idx >= len(last):
            return None
        price = _parse_positive_float(last[close_idx])
        if price is None:
            logger.warning("Stooq 收盘价缺失或非法: %s", symbol)
            return None
        return Quote(
            symbol=symbol,
            label=symbol,
            category="stock",
            price=price,
            price_kind="stock",
            source="stooq",
            fetched_at=datetime.now(timezone.utc),
            raw={"row": last},
        )

    async def _tokenized_quote(self, symbol: str, contract: str) -> Quote | None:
        address = (contract or symbol).strip()
        if not address:
            return None
        response = await self._get(
            f"{self.gecko_base}/api/v2/networks/base/tokens/{address}/pools",
            {},
            symbol,
        )
        if response is None:
            return None
        try:
            payload = response.json()
        except ValueError:
            logger.warning("DEX 聚合响应非 JSON: %s", symbol)
            return None
        pools = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(pools, list) or not pools:
            logger.warning("DEX 无活跃池: %s", symbol)
            return None

        pool = pools[0]
        attrs = pool.get("attributes", pool) if isinstance(pool, dict) else {}
        if not isinstance(attrs, dict):
            return None
        price = _parse_positive_float(
            attrs.get("price_in_usd") or attrs.get("price_usd")
        )
        if price is None:
            logger.warning("DEX 池价缺失或非法: %s", symbol)
            return None
        liquidity = _parse_positive_float(attrs.get("reserve_in_usd"))
        return Quote(
            symbol=symbol,
            label=symbol,
            category="tokenized_stock",
            price=price,
            price_kind="dex",
            source="geckoterminal",
            fetched_at=datetime.now(timezone.utc),
            liquidity_usd=liquidity,
            raw={"pool": pool, "quote_token": attrs.get("quote_token", "")},
        )

    async def aclose(self) -> None:
        """关闭自建 HTTP 客户端；注入的客户端由调用方负责。"""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
