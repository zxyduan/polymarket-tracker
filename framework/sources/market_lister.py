"""Gamma 市场发现层采集器（设计 v3.5 §7.1，dev §4.3）。

只负责翻页拉取公开 Gamma `/markets` 原始条目，不做解析、量阈值或档位
判定（那些在 parsing / usecase）。429 与 5xx 指数退避重试，页间限速。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.utils.retry import with_retry

logger = logging.getLogger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


class ListerFetchError(RuntimeError):
    """Gamma 列表拉取失败（重试耗尽 / 不可恢复的 4xx）。"""


class GammaMarketLister:
    """按 24h 成交量倒序翻页拉取活跃未结算市场的原始 JSON 条目。"""

    def __init__(
        self,
        base_url: str = GAMMA_BASE_URL,
        *,
        timeout_seconds: int = 10,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        page_interval_seconds: float = 0.2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff_seconds
        self.page_interval = page_interval_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def list_active_markets(
        self,
        *,
        page_limit: int = 100,
        max_pages: int = 4,
    ) -> list[dict[str, Any]]:
        """翻页聚合活跃市场原始条目；空页或不足一页即停止。"""
        items: list[dict[str, Any]] = []
        offset = 0
        for page in range(max_pages):
            page_items = await self._fetch_page(offset=offset, limit=page_limit)
            if not page_items:
                break
            items.extend(page_items)
            if len(page_items) < page_limit:
                break
            offset += page_limit
            if page < max_pages - 1:
                await asyncio.sleep(self.page_interval)
        return items

    async def _fetch_page(self, *, offset: int, limit: int) -> list[dict[str, Any]]:
        params = {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": limit,
            "offset": offset,
        }

        async def _do() -> list[dict[str, Any]]:
            response = await self._client.get(f"{self.base_url}/markets", params=params)
            if response.status_code == 429 or response.status_code >= 500:
                raise ListerFetchError(f"HTTP {response.status_code}")
            if response.status_code >= 400:
                # 其余 4xx 不重试，直接收敛为 ListerFetchError
                raise ListerFetchError(f"HTTP {response.status_code}")
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else data.get("data", [])

        return await with_retry(
            _do,
            attempts=self.max_retries,
            base_delay=self.retry_backoff,
            description="gamma markets 列表",
        )

    async def aclose(self) -> None:
        """关闭自建 HTTP 客户端；注入的客户端由调用方负责。"""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
