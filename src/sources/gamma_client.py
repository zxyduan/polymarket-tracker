"""Gamma API 市场元数据客户端。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GammaClient:
    """按 condition_id 获取市场问题文案与 slug，结果走 SQLite 缓存。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 10,
        cache_ttl_hours: int = 24,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self.cache_ttl = timedelta(hours=cache_ttl_hours)

    async def get_market(
        self, storage: Any, condition_id: str
    ) -> tuple[str | None, str | None]:
        """返回 (问题文案, slug)，任何失败都降级为 None。"""
        if not condition_id:
            return None, None

        cached = await storage.get_market_cache(condition_id)
        if cached and self._is_fresh(cached.get("updated_at")):
            return cached.get("question"), cached.get("slug")

        question, slug = await self._fetch_market(condition_id)
        if question is not None or slug is not None:
            await storage.set_market_cache(condition_id, question, slug)
        return question, slug

    async def _fetch_market(
        self, condition_id: str
    ) -> tuple[str | None, str | None]:
        params_variants = [
            {"condition_id": condition_id},
            {"condition_ids": condition_id},
        ]
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for params in params_variants:
                    response = await client.get(
                        f"{self.base_url}/markets", params=params
                    )
                    response.raise_for_status()
                    data = response.json()
                    items = data if isinstance(data, list) else data.get("data", [])
                    for item in items:
                        if item.get("conditionId") == condition_id:
                            question = item.get("question") or item.get("title")
                            return question, item.get("slug")
        except Exception as exc:  # noqa: BLE001 - 富化是增强能力，失败降级
            logger.debug("Gamma 查询 %s 失败: %s", condition_id, exc)
        return None, None

    def _is_fresh(self, updated_at: str | None) -> bool:
        if not updated_at:
            return False
        try:
            dt = datetime.fromisoformat(updated_at)
        except ValueError:
            return False
        return datetime.now(timezone.utc) - dt < self.cache_ttl
