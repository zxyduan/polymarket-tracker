"""快照纯函数平移（R-06，设计 v3.5 §4.1）。

本模块为无 IO 纯函数，函数体从 src/snapshots/collector.py 逐字平移，
不增不减；collector 只保留编排。常量与现状保持一致。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

BUCKET_SIZE_SEC = 7200  # 2 小时一个桶
WINDOW_VOL_SHORT_BUCKETS = 6  # 短期波动：最近 6 桶（12h）
WINDOW_VOL_LONG_BUCKETS = 24  # 长期波动：最近 24 桶（2 天）
WINDOW_TREND_BUCKETS = 24  # 趋势方向窗口
TREND_DIRECTION_RATIO = 0.6  # 同向桶占比 ≥60% 算趋势
VOL_REGIME_LOW = 0.7
VOL_REGIME_HIGH = 1.3
DEPTH_LEVELS = 5
WALL_MIN_NOTIONAL = 100.0


@dataclass(frozen=True)
class BookDepth:
    bid_top5: float
    ask_top5: float
    bid_wall_count: int


def bucketize_prices(points: list[dict[str, Any]]) -> dict[int, list[float]]:
    """价格点按 2h 桶分组 → {bucket_ts: [price, ...]}。"""
    buckets: dict[int, list[float]] = {}
    for pt in points:
        ts, price = pt.get("t"), pt.get("p")
        if ts is None or price is None:
            continue
        try:
            ts_int = int(ts)
            price_f = float(price)
        except (TypeError, ValueError):
            continue
        bucket = (ts_int // BUCKET_SIZE_SEC) * BUCKET_SIZE_SEC
        buckets.setdefault(bucket, []).append(price_f)
    return buckets


def calc_volume_delta(current: float | None, prev: float | None) -> float:
    if current is None or prev is None:
        return 0.0
    return max(0.0, current - prev)


def calc_trend(
    current_close: float, history: list[dict[str, Any]]
) -> tuple[str, float, int]:
    """单桶趋势、涨跌幅、连桶数。history 新的在前，上一桶为 history[0]。"""
    if not history:
        return ("flat", 0.0, 0)
    prev = history[0]
    prev_close = float(prev["price_close"])
    if prev_close <= 0:
        return ("flat", 0.0, 0)

    diff = current_close - prev_close
    pct = diff / prev_close

    if abs(diff) <= 0.0005:
        trend = "flat"
        streak = 0 if prev["trend"] == "flat" else int(prev["trend_streak"])
    elif diff > 0:
        trend = "up"
        streak = int(prev["trend_streak"]) + 1 if prev["trend"] == "up" else 1
    else:
        trend = "down"
        streak = int(prev["trend_streak"]) - 1 if prev["trend"] == "down" else -1
    return (trend, pct, streak)


def calc_trend_direction(history: list[dict[str, Any]]) -> str:
    """最近 N 桶涨跌占比 → long_up / long_down / sideways。"""
    if len(history) < WINDOW_TREND_BUCKETS // 2:
        return ""
    recent = history[:WINDOW_TREND_BUCKETS]
    up_count = sum(1 for s in recent if s["trend"] == "up")
    down_count = sum(1 for s in recent if s["trend"] == "down")
    total = len(recent)
    if total == 0:
        return ""
    if up_count / total >= TREND_DIRECTION_RATIO:
        return "long_up"
    if down_count / total >= TREND_DIRECTION_RATIO:
        return "long_down"
    return "sideways"


def calc_drawdown(current_close: float, history: list[dict[str, Any]], days: int = 7) -> float:
    """最近 N 天最高点回撤（负数），0 = 仍在最高点。"""
    if not history:
        return 0.0
    cutoff = time.time() - days * 86400
    highs = [current_close]
    for s in history:
        if int(s["bucket_ts"]) < cutoff:
            break
        highs.append(float(s["price_high"]))
    highest = max(highs)
    if highest <= 0:
        return 0.0
    return (current_close - highest) / highest


def calc_percentile(
    current_close: float, history: list[dict[str, Any]], days: int = 30
) -> float:
    """当前价在最近 N 天价格区间的百分位（0~1）。"""
    if not history:
        return 0.5
    cutoff = time.time() - days * 86400
    prices = [current_close]
    for s in history:
        if int(s["bucket_ts"]) < cutoff:
            break
        prices.extend([float(s["price_low"]), float(s["price_high"])])
    if len(prices) < 2:
        return 0.5
    prices.sort()
    low, high = prices[0], prices[-1]
    if high <= low:
        return 0.5
    return max(0.0, min(1.0, (current_close - low) / (high - low)))


def calc_volatility(history: list[dict[str, Any]]) -> tuple[float, float, str]:
    """短/长期波动率与分档（low/normal/high）。"""
    if len(history) < WINDOW_VOL_SHORT_BUCKETS:
        return (0.0, 0.0, "")
    closes_short = [float(s["price_close"]) for s in history[:WINDOW_VOL_SHORT_BUCKETS]]
    closes_long = [float(s["price_close"]) for s in history[:WINDOW_VOL_LONG_BUCKETS]]
    vol_short = stddev(closes_short)
    vol_long = stddev(closes_long)
    if vol_long <= 0:
        return (vol_short, vol_long, "")
    ratio = vol_short / vol_long
    if ratio < VOL_REGIME_LOW:
        regime = "low"
    elif ratio > VOL_REGIME_HIGH:
        regime = "high"
    else:
        regime = "normal"
    return (vol_short, vol_long, regime)


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def calc_depth_change(current_bid_depth: float, history: list[dict[str, Any]]) -> float:
    """买盘深度变化率（vs 上一桶）。"""
    if not history:
        return 0.0
    prev_depth = float(history[0]["bid_depth_top5"])
    if prev_depth <= 0:
        return 0.0
    return (current_bid_depth - prev_depth) / prev_depth


def parse_book_depth(data: dict[str, Any]) -> BookDepth:
    """解析订单簿：bids 高价在前取 5 档、asks 低价在前取 5 档。"""
    bid_levels: list[tuple[float, float]] = []
    for level in data.get("bids") or []:
        try:
            bid_levels.append((float(level.get("price", 0)), float(level.get("size", 0))))
        except (TypeError, ValueError):
            continue
    ask_levels: list[tuple[float, float]] = []
    for level in data.get("asks") or []:
        try:
            ask_levels.append((float(level.get("price", 0)), float(level.get("size", 0))))
        except (TypeError, ValueError):
            continue

    bid_levels.sort(key=lambda x: x[0], reverse=True)
    ask_levels.sort(key=lambda x: x[0])
    bid_top5 = bid_levels[:DEPTH_LEVELS]
    ask_top5 = ask_levels[:DEPTH_LEVELS]

    return BookDepth(
        bid_top5=sum(price * size for price, size in bid_top5),
        ask_top5=sum(price * size for price, size in ask_top5),
        bid_wall_count=sum(
            1 for price, size in bid_top5 if price * size >= WALL_MIN_NOTIONAL
        ),
    )


def calc_our_fv(fv_initial: float, fv_end_date: str) -> float:
    """our_fv：到期衰减简化版（未到期=初值，到期=0）。"""
    if fv_initial <= 0 or not fv_end_date:
        return 0.0
    try:
        from datetime import datetime, timezone

        end = datetime.fromisoformat(fv_end_date).replace(tzinfo=timezone.utc)
        if (end - datetime.now(timezone.utc)).total_seconds() > 0:
            return fv_initial
        return 0.0
    except (ValueError, TypeError):
        return fv_initial
