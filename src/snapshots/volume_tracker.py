"""RTDS 全量成交成交量累加器。

Polymarket 的 RTDS activity 流是"全市场"成交广播（客户端按钱包过滤），
快照市场并非交易员地址，原本会被过滤丢弃。本模块在过滤之前先把
每一笔成交按 (condition_id, 2h 桶) 记账，供快照采集器补算闭合桶的真实
成交量增量——精确到桶边界、实时、无需 API key，不受 Gamma volumeNum
缓存延迟与采集间隔影响。
"""

from __future__ import annotations

import time
from datetime import datetime

BUCKET_SIZE_SEC = 7200  # 与采集器口径一致


def bucket_of(ts: float) -> int:
    """时间戳 → 所在 2h 桶起点。"""
    return int(ts) // BUCKET_SIZE_SEC * BUCKET_SIZE_SEC


class VolumeTracker:
    """内存账本：{(condition_id, bucket_ts): 成交额(USDC)}。

    单事件循环内使用，无需加锁；settle 取走即删，避免重复累计。
    """

    def __init__(self) -> None:
        self._volumes: dict[tuple[str, int], float] = {}

    def record(self, condition_id: str | None, notional: float | None, ts: float | None = None) -> None:
        """记一笔成交额到其所属桶。

        condition_id 缺失、金额非正或时间非法时静默忽略。
        notional 可能是 Decimal（StandardEvent 口径），统一转 float 累加。
        """
        if not condition_id or notional is None:
            return
        try:
            total = float(notional)
        except (TypeError, ValueError):
            return
        if total <= 0:
            return
        if ts is None:
            ts = time.time()
        elif isinstance(ts, datetime):
            ts = ts.timestamp()
        try:
            bucket = bucket_of(float(ts))
        except (TypeError, ValueError):
            return
        key = (condition_id, bucket)
        self._volumes[key] = self._volumes.get(key, 0.0) + float(total)

    def bucket_volume(self, condition_id: str, bucket_ts: int) -> float | None:
        """取指定市场指定桶的累计成交额；无记录返回 None。"""
        return self._volumes.get((condition_id, bucket_ts))

    def settle(self, condition_id: str, bucket_ts: int) -> float | None:
        """取走并删除该桶累计额（补算写库后调用，防止下次重复累计）。"""
        return self._volumes.pop((condition_id, bucket_ts), None)

    def __len__(self) -> int:
        return len(self._volumes)
