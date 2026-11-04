"""F2 快照推送用例（设计 v3.5 §5，S-01/S-02）。

10 步流程：cid 解析 → backfill → 最新完整桶 → 空返回 → 陈旧守卫 →
实时价 → display 归一 → 渲染 → body_hash 去重 → router 分发。
send_failed 只看通道异常或无通道；成功才写指纹。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, MutableMapping, Protocol

from framework.delivery.router import ChannelRouter
from framework.models.message import RenderedMessage
from framework.models.ports import SnapshotQuery
from framework.scheduler.guard import BodyHashGuard
from features.market_snapshot.ports import SnapshotCollectorPort, SnapshotRenderer

logger = logging.getLogger(__name__)


class MarketCatalog(Protocol):
    def slugs(self) -> list[str]: ...


@dataclass(frozen=True)
class SnapshotPushConfig:
    dedup: bool = True
    max_stale_buckets: int = 24
    bucket_seconds: int = 7200  # 2h 桶大小，用于计算滞后桶数
    now_bucket: int = 0   # 0 = 由注入的时钟计算；测试可覆盖


class SnapshotPushUsecase:
    def __init__(
        self,
        *,
        catalog: MarketCatalog,
        collector: SnapshotCollectorPort,
        query: SnapshotQuery,
        renderer: SnapshotRenderer,
        router: ChannelRouter,
        body_hash: BodyHashGuard,
        config: SnapshotPushConfig,
        health: MutableMapping[str, Any],
    ) -> None:
        self._catalog = catalog
        self._collector = collector
        self._query = query
        self._renderer = renderer
        self._router = router
        self._hash = body_hash
        self._cfg = config
        self._health = health

    async def run(self) -> None:
        now_bucket = self._cfg.now_bucket or 0

        # 1) 逐市场解析 cid，失败跳过
        cids: list[str] = []
        for slug in self._catalog.slugs():
            try:
                resolved = await self._collector.resolve_market(slug)
            except Exception as exc:  # 解析失败跳过
                logger.warning("resolve_market %s 失败: %s", slug, exc)
                continue
            if resolved is None:
                continue
            cids.append(resolved[0])

        # 2) 逐市场 backfill，失败仅告警
        for cid in cids:
            try:
                await self._collector.backfill_prev_bucket(cid)
            except Exception as exc:
                logger.warning("backfill %s 失败: %s", cid, exc)

        # 3) 最新完整桶
        rows = await self._query.latest_complete_buckets(now_bucket)

        # 4) 无数据直接返回（调度器仍写 state）
        if not rows:
            return

        # 5) 陈旧守卫：最新闭合桶落后 max_stale_buckets 个桶则跳过推送
        #    避免采集中断后，整点仍照常推送过时数据误导用户
        self._health["last_rows"] = len(rows)
        if self._cfg.max_stale_buckets > 0 and now_bucket:
            latest_bucket = max(int(r.get("bucket_ts", 0)) for r in rows)
            lag_buckets = (now_bucket - latest_bucket) // self._cfg.bucket_seconds
            if lag_buckets >= self._cfg.max_stale_buckets:
                logger.warning(
                    "快照陈旧守卫触发：最新桶落后 %d 个桶（阈值 %d），跳过本次推送",
                    lag_buckets, self._cfg.max_stale_buckets,
                )
                self._health["stale_skipped"] = lag_buckets
                return

        # 6) 实时价
        try:
            live = dict(await self._collector.fetch_live_prices(rows))
        except Exception as exc:
            logger.warning("live prices 失败，回退收盘价: %s", exc)
            live = {}

        # 8) 渲染
        msg = self._renderer.render(rows, live_prices=live, now_bucket=now_bucket)
        if msg is None:
            return

        # 9) dedup 开启才读指纹（S-01）
        if self._cfg.dedup:
            if await self._hash.matches("snapshot", f"push:{now_bucket}", msg.body):
                return

        # 10) 逐通道分发；send_failed = 通道失败或无通道
        result = await self._router.dispatch("market_snapshot", msg)
        send_failed = bool(result.channel_failures) or result.configured_channels == 0
        self._health["send_failed"] = send_failed

        if self._cfg.dedup and not send_failed:
            await self._hash.record("snapshot", f"push:{now_bucket}", msg.body)
