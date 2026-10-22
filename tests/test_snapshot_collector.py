"""快照采集器全流程测试（mock httpx，不依赖外网）。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.snapshots.collector import SnapshotCollector
from src.storage import Storage


class FakeResponse:
    def __init__(self, json_data: Any, status: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=self  # type: ignore[arg-type]
            )

    def json(self) -> Any:
        return self._json_data


class FakeAsyncClient:
    """按路径返回预设响应；collector 用 base_url 拼接相对路径。"""

    def __init__(self, base_url: str, routes: dict[str, FakeResponse]) -> None:
        self.base_url = base_url
        self.routes = routes

    async def get(self, path: str, params: dict | None = None) -> FakeResponse:
        # 精确路径优先，其次按关键词匹配
        if path in self.routes:
            return self.routes[path]
        for key, resp in self.routes.items():
            if key in path:
                return resp
        return FakeResponse([], status=404)

    async def aclose(self) -> None:
        return


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    db = Storage(str(tmp_path / "test.db"))
    await db.init()
    return db


def _make_routes() -> dict[str, FakeResponse]:
    now = int(time.time())
    bucket = (now // 7200) * 7200
    # 价格历史：上一完整桶 + 当前桶各若干点
    history = [
        {"t": bucket - 7200 + i * 3600, "p": 0.5 + i * 0.01} for i in range(2)
    ] + [
        {"t": bucket + i * 3600, "p": 0.52 + i * 0.01} for i in range(2)
    ]
    return {
        "/markets": FakeResponse(
            [{"volumeNum": "12345.0", "conditionId": "0xcid"}]
        ),
        "/prices-history": FakeResponse({"history": history}),
        "/book": FakeResponse(
            {
                "bids": [
                    {"price": "0.50", "size": "100"},
                    {"price": "0.49", "size": "50"},
                ],
                "asks": [{"price": "0.52", "size": "30"}],
            }
        ),
        "/public-search": FakeResponse(
            [
                {
                    "slug": "slug-a",
                    "conditionId": "0xcid",
                    "clobTokenIds": '["123","456"]',
                }
            ]
        ),
    }


async def test_collect_full_flow(storage: Storage) -> None:
    routes = _make_routes()
    collector = SnapshotCollector(storage, timeout=5)
    collector._gamma = FakeAsyncClient("https://gamma", routes)  # type: ignore[assignment]
    collector._clob = FakeAsyncClient("https://clob", routes)  # type: ignore[assignment]

    now = int(time.time())
    bucket = (now // 7200) * 7200
    # 测试可能运行在桶中途，放宽到 2h，等价于"桶起点首轮"
    await collector.collect(
        condition_id="0xcid",
        yes_token_id="123",
        slug="slug-a",
        slug_id="aaaa",
        baseline_grace_seconds=7200,
    )

    last = await storage.last_snapshot("0xcid")
    assert last is not None
    assert last["slug_id"] == "aaaa"
    assert last["price_close"] > 0
    # 当前桶未闭合，量保持 0 / 未知
    assert last["volume_delta"] == 0.0
    assert last["volume_shares"] == -1.0
    assert last["bid_depth_top5"] > 0
    # 当前桶起点基线已记录（签名必须带 bucket_ts）
    assert await storage.get_snap_baseline("0xcid", bucket) == 12345.0

    # 跨桶结算：时间推进到下一个桶，累计量升到 13000 →
    # 上一闭合桶（bucket）份额 = 13000 - 12345 = 655
    import src.snapshots.collector as mod

    orig = mod._current_bucket_start
    mod._current_bucket_start = lambda: bucket + 7200  # type: ignore[assignment]
    try:
        await collector.backfill_prev_bucket("0xcid", cum_volume=13000.0)
    finally:
        mod._current_bucket_start = orig  # type: ignore[assignment]
    rows = {r["bucket_ts"]: r for r in await storage.recent_snapshots("0xcid", 10)}
    assert rows[bucket]["volume_shares"] == pytest.approx(655.0)
    # volume_delta 无 USDC 口径，保持 0
    assert rows[bucket]["volume_delta"] == 0.0


async def test_collect_gap_fills_missing_closed_bucket(storage: Storage) -> None:
    """漏桶自愈：历史里有但库里缺的闭合桶，下一轮必须补齐且不覆盖已有行。"""
    routes = _make_routes()
    collector = SnapshotCollector(storage, timeout=5)
    collector._gamma = FakeAsyncClient("https://gamma", routes)  # type: ignore[assignment]
    collector._clob = FakeAsyncClient("https://clob", routes)  # type: ignore[assignment]

    now = int(time.time())
    bucket = (now // 7200) * 7200
    # 已有市场：只在更早的桶（bucket-2h）有一行，缺 bucket-1h 这个闭合桶
    await storage.upsert_snapshot(
        condition_id="0xcid",
        slug="slug-a",
        slug_id="aaaa",
        bucket_ts=bucket - 2 * 7200,
        price_open=0.30,
        price_close=0.31,
        price_high=0.32,
        price_low=0.29,
        volume_delta=0.0,
        volume_shares=123.0,
        trend="flat",
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

    await collector.collect(
        condition_id="0xcid",
        yes_token_id="123",
        slug="slug-a",
        slug_id="aaaa",
    )

    rows = {
        r["bucket_ts"]: r
        for r in await storage.recent_snapshots("0xcid", buckets=10)
    }
    # 缺失的闭合桶（bucket-1h）已补齐
    assert bucket - 7200 in rows
    assert rows[bucket - 7200]["volume_shares"] == -1.0  # 无基线来源，量留未知
    # 当前桶已写
    assert bucket in rows
    # 旧行（含已结算量 123）未被覆盖
    assert rows[bucket - 2 * 7200]["volume_shares"] == 123.0
    assert rows[bucket - 2 * 7200]["price_close"] == 0.31


async def test_collect_baseline_recorded_without_current_bucket_points(
    storage: Storage,
) -> None:
    """当前桶无价点时不写当前桶行，但基线必须记录、上一闭合桶 OHLC 要刷新。"""
    routes = _make_routes()
    now = int(time.time())
    bucket = (now // 7200) * 7200
    # 只保留上一闭合桶的价点，当前桶为空
    routes["/prices-history"] = FakeResponse(
        {"history": [{"t": bucket - 7200 + i * 3600, "p": 0.5} for i in range(2)]}
    )
    collector = SnapshotCollector(storage, timeout=5)
    collector._gamma = FakeAsyncClient("https://gamma", routes)  # type: ignore[assignment]
    collector._clob = FakeAsyncClient("https://clob", routes)  # type: ignore[assignment]

    # 测试可能运行在桶中途，放宽到 2h，等价于"桶起点首轮"
    await collector.collect(
        condition_id="0xcid",
        yes_token_id="123",
        slug="slug-a",
        slug_id="aaaa",
        baseline_grace_seconds=7200,
    )

    # 当前桶没有行
    last = await storage.last_snapshot("0xcid")
    assert last is not None
    assert last["bucket_ts"] == bucket - 7200
    # 当前桶起点基线已记录（不再因价点缺失而丢基线）
    assert await storage.get_snap_baseline("0xcid", bucket) == 12345.0


async def test_collect_backfill_new_market(storage: Storage) -> None:
    routes = _make_routes()
    collector = SnapshotCollector(storage, timeout=5)
    collector._gamma = FakeAsyncClient("https://gamma", routes)  # type: ignore[assignment]
    collector._clob = FakeAsyncClient("https://clob", routes)  # type: ignore[assignment]

    await collector.collect(
        condition_id="0xcid",
        yes_token_id="123",
        slug="slug-a",
        slug_id="aaaa",
    )
    # 新市场回填了上一完整桶，加上当前桶 = 至少 2 条
    rows = await storage.recent_snapshots("0xcid", buckets=10)
    assert len(rows) >= 2


async def test_update_snapshot_volume(storage: Storage) -> None:
    """补算写回：更新指定闭合桶的 volume_delta，幂等且不影响其他桶。"""
    now_bucket = (int(time.time()) // 7200) * 7200
    prev_bucket = now_bucket - 7200
    for bucket in (prev_bucket, now_bucket):
        await storage.upsert_snapshot(
            condition_id="0xcid",
            slug="slug-a",
            slug_id="aaaa",
            bucket_ts=bucket,
            price_open=0.5,
            price_close=0.55,
            price_high=0.6,
            price_low=0.4,
            volume_delta=0.0,
            trend="flat",
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

    # 补算上一闭合桶
    await storage.update_snapshot_volume("0xcid", prev_bucket, 655.0)
    rows = await storage.recent_snapshots("0xcid", buckets=10)
    by_ts = {r["bucket_ts"]: r for r in rows}
    assert by_ts[prev_bucket]["volume_delta"] == pytest.approx(655.0)
    # 当前桶不受影响
    assert by_ts[now_bucket]["volume_delta"] == 0.0

    # 幂等：不存在的桶静默忽略
    await storage.update_snapshot_volume("0xcid", prev_bucket - 7200, 999.0)
    rows = await storage.recent_snapshots("0xcid", buckets=10)
    assert len(rows) == 2


async def test_resolve_market(storage: Storage) -> None:
    routes = _make_routes()
    collector = SnapshotCollector(storage, timeout=5)
    collector._gamma = FakeAsyncClient("https://gamma", routes)  # type: ignore[assignment]
    resolved = await collector.resolve_market("slug-a")
    assert resolved == ("0xcid", "123")


async def test_collect_skips_missing_price(storage: Storage) -> None:
    routes = _make_routes()
    routes["/prices-history"] = FakeResponse({"history": []})
    collector = SnapshotCollector(storage, timeout=5)
    collector._gamma = FakeAsyncClient("https://gamma", routes)  # type: ignore[assignment]
    collector._clob = FakeAsyncClient("https://clob", routes)  # type: ignore[assignment]

    # 无价格历史：不落库、不抛异常
    await collector.collect(
        condition_id="0xcid",
        yes_token_id="123",
        slug="slug-a",
        slug_id="aaaa",
    )
    assert await storage.last_snapshot("0xcid") is None


async def test_collect_recovers_missed_buckets_after_sparse_boundary(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归 2026-10-20 事故：边界轮空桶早退 → 漏桶永久冻结。

    时序（B0 为"16:00 桶"起点）：
      B0+1.5h  服务启动（桶中途）：写 B0 行，不记 B0 基线
      B0+2h+2s 边界轮：新桶 B1 无价点 → 旧版永久漏桶；新版记 B1 基线、下轮补
      B0+4h+2s 边界轮：B1 已积累价点但 B2 仍空 → B1 漏桶补齐并完成份额结算
    """
    import src.snapshots.collector as mod

    anchor = (int(time.time()) // 7200) * 7200 - 8 * 7200  # 固定历史锚点，避开真实当前桶
    b0 = anchor
    clock = {"t": b0 + 5400}
    monkeypatch.setattr(mod.time, "time", lambda: float(clock["t"]))

    def make_collector(history: list[dict], volume: str) -> SnapshotCollector:
        routes = _make_routes()
        routes["/prices-history"] = FakeResponse({"history": history})
        routes["/markets"] = FakeResponse(
            [{"volumeNum": volume, "conditionId": "0xcid"}]
        )
        c = SnapshotCollector(storage, timeout=5)
        c._gamma = FakeAsyncClient("https://gamma", routes)  # type: ignore[assignment]
        c._clob = FakeAsyncClient("https://clob", routes)  # type: ignore[assignment]
        return c

    # 预置两个更早的闭合桶，使其成为"存量市场"（不触发冷启动全量回填）
    for ts in (b0 - 2 * 7200, b0 - 4 * 7200):
        await storage.upsert_snapshot(
            condition_id="0xcid",
            slug="slug-a",
            slug_id="aaaa",
            bucket_ts=ts,
            price_open=0.5,
            price_close=0.5,
            price_high=0.5,
            price_low=0.5,
            volume_delta=0.0,
            trend="flat",
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

    def pts(*buckets: int) -> list[dict]:
        out = []
        for b in buckets:
            out += [
                {"t": b, "p": 0.5},
                {"t": b + 3600, "p": 0.51},
            ]
        return out

    # 第 1 轮：启动（B0 中途，B0 已有价点）
    c1 = make_collector(pts(b0 - 2 * 7200, b0), "10000")
    await c1.collect(
        condition_id="0xcid", yes_token_id="123", slug="slug-a", slug_id="aaaa"
    )

    # 第 2 轮：B1 边界 +2s，B1 尚无价点（低活跃市场）
    clock["t"] = b0 + 7200 + 2
    c2 = make_collector(pts(b0 - 2 * 7200, b0), "11000")
    await c2.collect(
        condition_id="0xcid", yes_token_id="123", slug="slug-a", slug_id="aaaa"
    )

    # 第 3 轮：B2 边界 +2s，B1 已积累价点、B2 仍空
    clock["t"] = b0 + 2 * 7200 + 2
    c3 = make_collector(pts(b0 - 2 * 7200, b0, b0 + 7200), "13000")
    await c3.collect(
        condition_id="0xcid", yes_token_id="123", slug="slug-a", slug_id="aaaa"
    )

    rows = {
        r["bucket_ts"]: r
        for r in await storage.recent_snapshots("0xcid", buckets=20)
    }
    # 关键：B1 这个"边界轮空桶"被补齐，没有永久冻结
    assert b0 + 7200 in rows
    # B1 量按 B1 起点基线(11000) 结算为 13000-11000=2000
    assert rows[b0 + 7200]["volume_shares"] == pytest.approx(2000.0)
    # B1 起点基线在空桶边界轮已记录（不再因价点缺失丢基线）
    assert await storage.get_snap_baseline("0xcid", b0 + 7200) == 11000.0
    # B0 为启动中途桶，起点基线确实拿不到，诚实保持未知（而非假 0）
    assert rows[b0]["volume_shares"] == -1.0
    assert await storage.get_snap_baseline("0xcid", b0) is None
