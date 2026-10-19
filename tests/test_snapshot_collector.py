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

    await collector.collect(
        condition_id="0xcid",
        yes_token_id="123",
        slug="slug-a",
        slug_id="aaaa",
    )

    last = await storage.last_snapshot("0xcid")
    assert last is not None
    assert last["slug_id"] == "aaaa"
    assert last["price_close"] > 0
    # 无基线 → 首轮增量=0（与参考源口径一致，基线从下轮起生效）
    assert last["volume_delta"] == 0.0
    assert last["bid_depth_top5"] > 0
    # 基线已保存
    assert await storage.get_snap_baseline("0xcid") == 12345.0

    # 第二轮：累计量升到 13000 → 上一闭合桶增量 = 13000 - 12345 = 655
    routes["/markets"] = FakeResponse(
        [{"volumeNum": "13000.0", "conditionId": "0xcid"}]
    )
    await collector.collect(
        condition_id="0xcid",
        yes_token_id="123",
        slug="slug-a",
        slug_id="aaaa",
    )
    last2 = await storage.last_snapshot("0xcid")
    assert last2 is not None
    # 当前桶刚开 → 写入 0，闭合后由下一轮补算
    assert last2["volume_delta"] == 0.0
    # 上一闭合桶被补算为真实增量
    prev_rows = await storage.recent_snapshots("0xcid", buckets=10)
    prev_bucket = next(
        r for r in prev_rows if r["bucket_ts"] == last2["bucket_ts"] - 7200
    )
    assert prev_bucket["volume_delta"] == pytest.approx(655.0)


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
