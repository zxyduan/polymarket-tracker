"""2 小时粒度市场快照采集器。

数据来源：
  - Gamma /markets          → 累计成交量 volumeNum（基线差兜底）
  - RTDS 全量成交流          → 每笔成交按桶记账（精确成交量增量，主来源）
  - CLOB /prices-history    → 历史价格序列，按 2h 桶聚合 OHLC
  - CLOB /book              → 订单簿快照，前 5 档深度 + 墙数量

说明：CLOB /trades 逐笔接口已要求 API key（2026 起），故成交量增量
主来源改为 RTDS activity 全量成交记账（VolumeTracker），无需认证、
精确到桶边界；Gamma volumeNum 基线差仅作兜底。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── 常量（与参考源一致，禁止改动口径）────────────────────────────────────

BUCKET_SIZE_SEC = 7200  # 2 小时一个桶
RETENTION_DAYS = 90  # 保留 90 天

WINDOW_DRAWDOWN_DAYS = 7  # 回撤窗口
WINDOW_PERCENTILE_DAYS = 30  # 分位窗口
WINDOW_VOL_SHORT_BUCKETS = 6  # 短期波动：最近 6 桶（12h）
WINDOW_VOL_LONG_BUCKETS = 24  # 长期波动：最近 24 桶（2 天）
WINDOW_TREND_BUCKETS = 24  # 趋势方向窗口

TREND_FLAT_TICKS = 0.5  # 半个 tick（0.0005）以内算 flat
TREND_DIRECTION_RATIO = 0.6  # 同向桶占比 ≥60% 算趋势

VOL_REGIME_LOW = 0.7
VOL_REGIME_HIGH = 1.3

DEPTH_LEVELS = 5
WALL_MIN_NOTIONAL = 100.0


@dataclass(frozen=True)
class BookDepth:
    """订单簿深度快照。"""

    bid_top5: float = 0.0
    ask_top5: float = 0.0
    bid_wall_count: int = 0


class SnapshotCollector:
    """采集器：从 Gamma + CLOB 拉数据，预计算特征，写入现有 Storage。"""

    def __init__(
        self,
        storage: Any,
        *,
        gamma_host: str = "https://gamma-api.polymarket.com",
        clob_host: str = "https://clob.polymarket.com",
        timeout: float = 15.0,
        price_fidelity_minutes: int = 60,
        volume_tracker: Any | None = None,
    ) -> None:
        self._storage = storage
        self._gamma = httpx.AsyncClient(base_url=gamma_host, timeout=timeout)
        self._clob = httpx.AsyncClient(base_url=clob_host, timeout=timeout)
        self._fidelity = price_fidelity_minutes
        self._resolved: dict[str, tuple[str, str]] = {}
        self._volume_tracker = volume_tracker

    async def aclose(self) -> None:
        await self._gamma.aclose()
        await self._clob.aclose()

    # ── 市场解析 ──────────────────────────────────────────────────────

    async def resolve_market(self, slug: str) -> tuple[str, str] | None:
        """按 slug 解析 (condition_id, yes_token_id)，结果内存缓存。

        优先查 /markets/slug/{slug}；失败回退 /public-search。
        """
        if slug in self._resolved:
            return self._resolved[slug]
        for params in (
            None,
            {"q": slug, "limit": 10},
        ):
            try:
                if params is None:
                    r = await self._gamma.get(f"/markets/slug/{slug}")
                    r.raise_for_status()
                    item = r.json()
                else:
                    r = await self._gamma.get("/public-search", params=params)
                    r.raise_for_status()
                    data = r.json() or []
                    item = next(
                        (m for m in data if m.get("slug") == slug),
                        (data[0] if data else None),
                    )
                    if item is None:
                        continue
                cid = item.get("conditionId")
                token_ids = _json_list(item.get("clobTokenIds"))
                if cid and token_ids:
                    result = (str(cid), str(token_ids[0]))
                    self._resolved[slug] = result
                    return result
            except Exception as exc:  # noqa: BLE001 - 解析失败降级
                logger.debug("解析 slug %s 失败: %s", slug, exc)
        return None

    # ── 主流程：一次采集 ──────────────────────────────────────────────

    async def collect(
        self,
        *,
        condition_id: str,
        yes_token_id: str,
        slug: str,
        slug_id: str,
        fv_initial: float = 0.0,
        fv_end_date: str = "",
    ) -> None:
        """跑一次完整采集：新市场回填 → 拉数据 → 算特征 → 写入 → 清理。"""
        # 0. 新市场自动回填历史（库里没有任何桶才跑）
        existing = await self._storage.recent_snapshots(condition_id, buckets=1)
        if not existing:
            await self.backfill_history(
                condition_id=condition_id,
                yes_token_id=yes_token_id,
                slug=slug,
                slug_id=slug_id,
            )

        bucket_ts = _current_bucket_start()

        # 1. 拉累计成交量
        cum_volume = await self._fetch_cumulative_volume(condition_id)

        # 2. 算自上次采集以来的增量（上一闭合桶的最终成交量）
        prev_vol = await self._storage.get_snap_baseline(condition_id)
        volume_delta = _calc_volume_delta(cum_volume, prev_vol)

        # 2b. 补算上一闭合桶：RTDS 全量成交记账优先（精确、实时、无需认证）。
        #     低频市场 1-2h 才一笔，成交常发生在两轮采集之间，Gamma 基线差
        #     会被上一轮基线"吃掉"（基线已含该成交 → 差值 0），RTDS 按
        #     成交时间分桶记账可精确捞出整个闭合桶的成交量。
        prev_bucket_ts = bucket_ts - BUCKET_SIZE_SEC
        if self._volume_tracker is not None:
            tracked = self._volume_tracker.settle(condition_id, prev_bucket_ts)
            if tracked is not None:
                volume_delta = tracked
        # 已被 RTDS 补算过的桶（volume_delta>0）不再用基线差覆盖：
        # 同一桶内连续多轮（启动轮+手动轮）时基线差会把正确值写坏
        recent2 = await self._storage.recent_snapshots(condition_id, buckets=2)
        prev_row = next(
            (r for r in recent2 if int(r["bucket_ts"]) == prev_bucket_ts), None
        )
        if prev_row is not None and float(prev_row["volume_delta"]) > 0:
            volume_delta = 0.0
        # 有正值增量就补算写库（RTDS 记账不依赖基线是否存在）
        if volume_delta > 0:
            await self._storage.update_snapshot_volume(
                condition_id, prev_bucket_ts, volume_delta
            )
            logger.info(
                "snap prev-bucket cid=%s bucket=%s vol=%.0f",
                condition_id[:8],
                prev_bucket_ts,
                volume_delta,
            )
        # 当前桶刚开，本桶写入 0；闭合后由下一轮补算真实增量
        current_volume_delta = 0.0

        # 3. 拉价格历史，提取本桶 OHLC
        price_points = await self._fetch_price_history(
            yes_token_id, days=WINDOW_PERCENTILE_DAYS
        )
        if not price_points:
            logger.warning("snap_no_price cid=%s", condition_id[:8])
            return

        buckets = _bucketize_prices(price_points)
        current_prices = buckets.get(bucket_ts)
        if current_prices is None:
            logger.warning("snap_no_current_bucket cid=%s", condition_id[:8])
            return

        price_open = current_prices[0]
        price_close = current_prices[-1]
        price_high = max(current_prices)
        price_low = min(current_prices)

        # 4. 拉订单簿快照
        book = await self._fetch_book_depth(yes_token_id)

        # 5. 查历史快照，预计算特征
        history = await self._storage.recent_snapshots(
            condition_id, buckets=WINDOW_PERCENTILE_DAYS * 12
        )
        trend, trend_pct, trend_streak = _calc_trend(price_close, history)
        trend_direction = _calc_trend_direction(history)
        drawdown = _calc_drawdown(price_close, history, days=WINDOW_DRAWDOWN_DAYS)
        percentile = _calc_percentile(price_close, history, days=WINDOW_PERCENTILE_DAYS)
        vol_short, vol_long, vol_regime = _calc_volatility(history)
        bid_depth_change = _calc_depth_change(book.bid_top5, history)

        # 6. 合理价值（本期简化：market_fv=中间价；our_fv 按到期衰减）
        market_fv = (price_open + price_close) / 2 if price_open > 0 and price_close > 0 else 0.0
        our_fv = _calc_our_fv(fv_initial, fv_end_date)

        # 7. 写入（幂等 UPSERT）
        await self._storage.upsert_snapshot(
            condition_id=condition_id,
            slug=slug,
            slug_id=slug_id,
            bucket_ts=bucket_ts,
            price_open=price_open,
            price_close=price_close,
            price_high=price_high,
            price_low=price_low,
            volume_delta=current_volume_delta,
            trend=trend,
            trend_pct=trend_pct,
            trend_streak=trend_streak,
            trend_direction=trend_direction,
            drawdown_from_high=drawdown,
            price_percentile=percentile,
            bid_depth_top5=book.bid_top5,
            ask_depth_top5=book.ask_top5,
            bid_wall_count=book.bid_wall_count,
            bid_depth_change=bid_depth_change,
            vol_short=vol_short,
            vol_long=vol_long,
            vol_regime=vol_regime,
            our_fv=our_fv,
            market_fv=market_fv,
        )

        # 保存成交量基线
        if cum_volume is not None:
            await self._storage.set_snap_baseline(condition_id, cum_volume)

        # 8. 清理旧数据
        await self._storage.prune_snapshots(RETENTION_DAYS)
        logger.info(
            "snapshot collected cid=%s bucket=%s vol=%.0f regime=%s",
            condition_id[:8],
            bucket_ts,
            current_volume_delta,
            vol_regime,
        )

    async def backfill_history(
        self,
        *,
        condition_id: str,
        yes_token_id: str,
        slug: str,
        slug_id: str,
        days: int = WINDOW_PERCENTILE_DAYS,
    ) -> int:
        """冷启动：一次性回填最近 N 天历史桶（盘口/量写 0，特征默认值）。"""
        price_points = await self._fetch_price_history(yes_token_id, days=days)
        if not price_points:
            return 0

        buckets = _bucketize_prices(price_points)
        now_bucket = _current_bucket_start()
        count = 0
        for bucket_ts, prices in sorted(buckets.items()):
            if bucket_ts >= now_bucket:
                continue  # 当前桶留给正常采集
            await self._storage.upsert_snapshot(
                condition_id=condition_id,
                slug=slug,
                slug_id=slug_id,
                bucket_ts=bucket_ts,
                price_open=prices[0],
                price_close=prices[-1],
                price_high=max(prices),
                price_low=min(prices),
                volume_delta=0.0,
                trend="",
                trend_pct=0.0,
                trend_streak=0,
                trend_direction="",
                drawdown_from_high=0.0,
                price_percentile=0.5,
                bid_depth_top5=0.0,
                ask_depth_top5=0.0,
                bid_wall_count=0,
                bid_depth_change=0.0,
                vol_short=0.0,
                vol_long=0.0,
                vol_regime="",
            )
            count += 1
        logger.info("backfill_done cid=%s buckets=%d", condition_id[:8], count)
        return count

    # ── fetcher ──────────────────────────────────────────────────────

    async def fetch_live_prices(self, rows: list[dict[str, Any]]) -> dict[str, float]:
        """推送前取每个市场 Gamma 最新价 → {condition_id: price}。

        失败的市场降级跳过（渲染时回退 price_close）；单市场超时/报错
        不拖垮整次推送，逐个并发拉取。
        """
        cids = [str(r.get("condition_id") or "") for r in rows if r.get("condition_id")]
        if not cids:
            return {}

        async def _one(cid: str) -> tuple[str, float | None]:
            try:
                r = await self._gamma.get(
                    "/markets", params={"condition_ids": cid, "limit": 1}
                )
                r.raise_for_status()
                data = r.json()
                if not data:
                    return (cid, None)
                price = data[0].get("price")
                if price is None:
                    return (cid, None)
                return (cid, float(price))
            except Exception as exc:  # noqa: BLE001 - 单市场失败降级
                logger.warning("snap_live_price_failed cid=%s err=%s", cid[:8], exc)
                return (cid, None)

        results = await asyncio.gather(*(_one(c) for c in cids))
        return {cid: price for cid, price in results if price is not None}

    async def _fetch_cumulative_volume(self, condition_id: str) -> float | None:
        try:
            r = await self._gamma.get(
                "/markets",
                params={"condition_ids": condition_id, "limit": 1},
            )
            r.raise_for_status()
            data = r.json()
            if not data:
                return None
            return float(data[0].get("volumeNum") or 0.0)
        except Exception as exc:  # noqa: BLE001 - 单市场失败跳过
            logger.warning("snap_volume_fetch_failed cid=%s err=%s", condition_id[:8], exc)
            return None

    async def _fetch_price_history(self, token_id: str, days: int = 30) -> list[dict[str, Any]]:
        now = int(time.time())
        start_ts = now - days * 86400
        try:
            r = await self._clob.get(
                "/prices-history",
                params={
                    "market": token_id,
                    "startTs": start_ts,
                    "fidelity": self._fidelity,
                },
            )
            r.raise_for_status()
            return r.json().get("history", [])
        except Exception as exc:  # noqa: BLE001 - 单市场失败跳过
            logger.warning("snap_price_fetch_failed token=%s err=%s", token_id[:12], exc)
            return []

    async def _fetch_book_depth(self, token_id: str) -> BookDepth:
        try:
            r = await self._clob.get(f"/book?token_id={token_id}")
            r.raise_for_status()
            return _parse_book_depth(r.json())
        except Exception as exc:  # noqa: BLE001 - 单市场失败跳过
            logger.warning("snap_book_fetch_failed token=%s err=%s", token_id[:12], exc)
            return BookDepth()


# ── 特征计算工具（算法与参考源逐一对应）──────────────────────────────────


def _current_bucket_start() -> int:
    """当前 2h 桶起始时间戳（UTC 对齐）。"""
    now = int(time.time())
    return (now // BUCKET_SIZE_SEC) * BUCKET_SIZE_SEC


def _json_list(value: Any) -> list[Any]:
    """clobTokenIds 是 JSON 字符串化数组，统一解析为列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    import json

    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _bucketize_prices(points: list[dict[str, Any]]) -> dict[int, list[float]]:
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


def _calc_volume_delta(current: float | None, prev: float | None) -> float:
    if current is None or prev is None:
        return 0.0
    return max(0.0, current - prev)


def _calc_trend(
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


def _calc_trend_direction(history: list[dict[str, Any]]) -> str:
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


def _calc_drawdown(
    current_close: float, history: list[dict[str, Any]], days: int = 7
) -> float:
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


def _calc_percentile(
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


def _calc_volatility(
    history: list[dict[str, Any]],
) -> tuple[float, float, str]:
    """短/长期波动率与分档（low/normal/high）。"""
    if len(history) < WINDOW_VOL_SHORT_BUCKETS:
        return (0.0, 0.0, "")
    closes_short = [float(s["price_close"]) for s in history[:WINDOW_VOL_SHORT_BUCKETS]]
    closes_long = [float(s["price_close"]) for s in history[:WINDOW_VOL_LONG_BUCKETS]]
    vol_short = _stddev(closes_short)
    vol_long = _stddev(closes_long)
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


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def _calc_depth_change(current_bid_depth: float, history: list[dict[str, Any]]) -> float:
    """买盘深度变化率（vs 上一桶）。"""
    if not history:
        return 0.0
    prev_depth = float(history[0]["bid_depth_top5"])
    if prev_depth <= 0:
        return 0.0
    return (current_bid_depth - prev_depth) / prev_depth


def _parse_book_depth(data: dict[str, Any]) -> BookDepth:
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


def _calc_our_fv(fv_initial: float, fv_end_date: str) -> float:
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
