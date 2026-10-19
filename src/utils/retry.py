"""退避重试工具。"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


async def with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    description: str = "request",
) -> T:
    """协程任务有限重试，指数退避 + 抖动；全部失败抛出最后一次异常。"""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001 - 重试边界，需保留上下文
            last_exc = exc
            if attempt == attempts:
                break
            delay = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.2)
            logger.warning(
                "%s 失败，第 %d/%d 次重试，%.1fs 后重试: %s",
                description,
                attempt,
                attempts,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def backoff_delay(attempt: int, min_seconds: float = 1.0, max_seconds: float = 30.0) -> float:
    """计算连接重连退避时长（1,2,4... 上限，加抖动）。"""
    delay = min(max_seconds, min_seconds * 2 ** max(0, attempt - 1))
    return delay + random.uniform(0, delay * 0.2)
