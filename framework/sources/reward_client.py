"""CLOB 奖励段采集客户端（T3.2）。

只做协议翻译：GET `/rewards/markets/{cid}` 原始 dict，不解析速率 / 代币 / 分档
（那些在 `framework/parsing/watchlist_parsing.py`）。429 优先按 `Retry-After`
退避，5xx 指数退避重试；单市场失败隔离返回 None，不拖垮整轮。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.utils.retry import backoff_delay

logger = logging.getLogger(__name__)

CLOB_BASE_URL = "https://clob.polymarket.com"


class RewardFetchError(RuntimeError):
    """奖励段拉取失败（重试耗尽 / 不可恢复的 4xx）。"""


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头为秒数；非整数秒 / 日期格式返回 None。"""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


class RewardMarketClient:
    """按 condition_id 串行拉取 CLOB 奖励段原始 JSON。"""

    def __init__(
        self,
        base_url: str = CLOB_BASE_URL,
        *,
        timeout_seconds: int = 10,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        request_interval_seconds: float = 0.2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(1, max_retries)
        self.retry_backoff = retry_backoff_seconds
        self.request_interval = request_interval_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def get_reward(self, condition_id: str) -> dict[str, Any] | None:
        """GET 单市场奖励段；失败 / 超时 / 4xx 隔离返回 None。"""
        if not condition_id:
            return None
        url = f"{self.base_url}/rewards/markets/{condition_id}"
        data: dict[str, Any] | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.get(url)
            except Exception as exc:  # noqa: BLE001 - 网络 / 超时：隔离本轮该市场
                logger.warning("奖励段请求失败 %s: %s", condition_id, exc)
                if attempt == self.max_retries:
                    return None
                await asyncio.sleep(backoff_delay(attempt, self.retry_backoff, 30.0))
                continue

            if response.status_code == 429:
                retry_after = _parse_retry_after(
                    response.headers.get("retry-after")
                )
                delay = (
                    retry_after
                    if retry_after is not None
                    else backoff_delay(attempt, self.retry_backoff, 30.0)
                )
                logger.warning(
                    "奖励段 429 %s，%.1fs 后重试（%d/%d）",
                    condition_id, delay, attempt, self.max_retries,
                )
                if attempt == self.max_retries:
                    return None
                await asyncio.sleep(min(delay, 30.0))
                continue

            if response.status_code >= 500:
                logger.warning(
                    "奖励段 5xx HTTP %s %s（%d/%d）",
                    response.status_code, condition_id, attempt, self.max_retries,
                )
                if attempt == self.max_retries:
                    return None
                await asyncio.sleep(backoff_delay(attempt, self.retry_backoff, 30.0))
                continue

            if response.status_code >= 400:
                # 其余 4xx 不重试，直接收敛
                logger.warning(
                    "奖励段不可用 HTTP %s %s", response.status_code, condition_id
                )
                return None

            try:
                payload = response.json()
            except ValueError:
                logger.warning("奖励段响应非 JSON: %s", condition_id)
                return None
            if isinstance(payload, list):
                data = {"rewards_config": payload}
            elif isinstance(payload, dict):
                data = payload
            else:
                data = None
            break

        if self.request_interval > 0:
            await asyncio.sleep(self.request_interval)
        return data

    async def aclose(self) -> None:
        """关闭自建 HTTP 客户端；注入的客户端由调用方负责。"""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
