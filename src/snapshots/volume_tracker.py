"""RTDS 全量成交成交量累加器。

Polymarket 的 RTDS activity 流是"全市场"成交广播（客户端按钱包过滤），
快照市场并非交易员地址，原本会被过滤丢弃。本模块在过滤之前先把每一笔
成交按 (condition_id, 2h 桶) 记账（USDC 成交额 + 份额双账本），供快照
采集器补算闭合桶的真实成交量增量——精确到桶边界、实时、无需 API key，
不受 Gamma volumeNum 缓存延迟与采集间隔影响。
"""

from __future__ import annotations

import time
from datetime import datetime

BUCKET_SIZE_SEC = 7200  # 与采集器口径一致


def bucket_of(ts: float) -> int:
    """时间戳 → 所在 2h 桶起点。"""
    return int(ts) // BUCKET_SIZE_SEC * BUCKET_SIZE_SEC


class VolumeTracker:
    """内存账本：{(condition_id, bucket_ts): (成交额 USDC, 份额)}。

    单事件循环内使用，无需加锁；settle 取走 USDC 即删（写库后防重复），
    份额账本保留供渲染/对比（桶闭合后不再有新成交进入该桶）。
    """

    def __init__(self) -> None:
        self._volumes: dict[tuple[str, int], float] = {}
        self._shares: dict[tuple[str, int], float] = {}

    def record(
        self,
        condition_id: str | None,
        notional: float | None,
        ts: float | None = None,
        price: float | None = None,
    ) -> None:
        """记一笔成交到其所属桶（USDC 成交额 + 份额）。

        RTDS 口径：size 是份额；成交额 notional = price × size；份额账本 = notional / price = size。
        condition_id 缺失、金额非正或时间非法时静默忽略。
        notional/price 可能是 Decimal（StandardEvent 口径），统一转 float。
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
        self._volumes[key] = self._volumes.get(key, 0.0) + total
        if price is not None:
            try:
                px = float(price)
            except (TypeError, ValueError):
                px = 0.0
            if px > 0:
                self._shares[key] = self._shares.get(key, 0.0) + total / px

    def bucket_volume(self, condition_id: str, bucket_ts: int) -> float | None:
        """取指定市场指定桶的累计成交额（USDC）；无记录返回 None。"""
        return self._volumes.get((condition_id, bucket_ts))

    def bucket_shares(self, condition_id: str, bucket_ts: int) -> float | None:
        """取指定市场指定桶的累计份额；无记录返回 None。"""
        return self._shares.get((condition_id, bucket_ts))

    def settle(self, condition_id: str, bucket_ts: int) -> float | None:
        """取走并删除该桶累计成交额（补算写库后调用，防止下次重复累计）。"""
        return self._volumes.pop((condition_id, bucket_ts), None)

    def __len__(self) -> int:
        return len(self._volumes)
