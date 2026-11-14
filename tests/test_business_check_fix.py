"""business-check 改造点验证测试（P1/P2 改造后补充）。

覆盖 3 个改造点：
1. F1 TraderAlertUsecase body_hash 去重
2. F2 SnapshotPushUsecase 陈旧守卫
3. 模块导入无报错（语法/类型自检）
"""

from __future__ import annotations

import asyncio

import pytest

from framework.delivery.router import ChannelRouter, DeliveryResult
from framework.delivery.targets import DeliveryTarget
from framework.models.message import RenderedMessage


# ── 公共桩 ──────────────────────────────────────────────────────────────

class _Provider:
    """返回所有常见 channel 的 target，确保 router 不跳过。"""
    def targets(self):
        return [
            DeliveryTarget(channel="telegram", address=""),
            DeliveryTarget(channel="ntfy", address=""),
        ]


class _FakeHash:
    """模拟 BodyHashGuard：matches 查已写集合，record 写入集合。"""
    def __init__(self):
        self.hits: set[str] = set()
        self.recorded: list[str] = []

    async def matches(self, ns: str, key: str, body: str) -> bool:
        return body in self.hits

    async def record(self, ns: str, key: str, body: str) -> None:
        self.hits.add(body)
        self.recorded.append(body)


class _GoodChannel:
    name = "telegram"
    def describe(self): return "telegram@1"
    async def deliver(self, msg, targets):
        return DeliveryResult(outcomes=())


class _BadChannel:
    name = "ntfy"
    def describe(self): return "ntfy@test"
    async def deliver(self, msg, targets):
        return DeliveryResult(
            outcomes=(),
            channel_failures=((_BadChannel.name, "boom", "ntfy@test"),),
        )


# ── 改造点 1：F1 body_hash 去重 ─────────────────────────────────────────

class _TraderRenderer:
    def render(self, event):
        return RenderedMessage(
            body=event.get("body", "alert-body"),
            event_keys=("trader",),
        )


def test_f1_body_hash_dedup_skips_second_send():
    """相同正文第二次 handle 应被 dedup 跳过，不再走 router，不再 record。"""
    from features.trader.usecase import TraderAlertUsecase, TraderConfig

    h = _FakeHash()
    uc = TraderAlertUsecase(
        renderer=_TraderRenderer(),
        router=ChannelRouter([_GoodChannel()], _Provider()),
        guard=h,
        config=TraderConfig(dedup=True),
        health={},
    )

    # 第一次发送成功，写指纹
    result1 = asyncio.run(uc.handle({"body": "same-body", "dedupe_key": "tx-1"}))
    assert result1.channel_failures == ()
    assert len(h.recorded) == 1

    # 第二次相同 body，应命中 dedup，不再 record、不再 dispatch
    result2 = asyncio.run(uc.handle({"body": "same-body", "dedupe_key": "tx-2"}))
    assert result2.channel_failures == ()
    assert len(h.recorded) == 1  # 没增加


def test_f1_failed_send_does_not_record_hash():
    """发送失败时不写指纹，下次应重试（不被 dedup 挡住）。"""
    from features.trader.usecase import TraderAlertUsecase, TraderConfig

    h = _FakeHash()
    uc = TraderAlertUsecase(
        renderer=_TraderRenderer(),
        router=ChannelRouter([_BadChannel()], _Provider()),
        guard=h,
        config=TraderConfig(dedup=True),
        health={},
    )

    result = asyncio.run(uc.handle({"body": "fail-body", "dedupe_key": "tx-3"}))
    assert len(result.channel_failures) == 1
    assert h.recorded == []  # 失败不写指纹


def test_f1_dedup_disabled_bypasses_hash():
    """dedup=False 时不走 body_hash 检查，直接 dispatch。"""
    from features.trader.usecase import TraderAlertUsecase, TraderConfig

    h = _FakeHash()
    uc = TraderAlertUsecase(
        renderer=_TraderRenderer(),
        router=ChannelRouter([_GoodChannel()], _Provider()),
        guard=h,
        config=TraderConfig(dedup=False),
        health={},
    )

    asyncio.run(uc.handle({"body": "no-dedup", "dedupe_key": "tx-4"}))
    # dedup 关闭时也不写指纹
    assert h.recorded == []


# ── 改造点 2：F2 陈旧守卫 ──────────────────────────────────────────────

class _SnapCatalog:
    def slugs(self): return ["a"]


class _SnapCollector:
    async def resolve_market(self, slug): return (f"cid-{slug}", slug)
    async def backfill_prev_bucket(self, cid): pass
    async def fetch_live_prices(self, rows): return {}


class _SnapQuery:
    def __init__(self, rows): self._rows = rows
    async def latest_complete_buckets(self, now_bucket, condition_ids=None): return self._rows


class _SnapRenderer:
    def render(self, rows, *, live_prices, now_bucket):
        return RenderedMessage(body="snap-body", event_keys=("snapshot",))


def test_f2_stale_guard_skips_push():
    """最新闭合桶落后 25 个桶（阈值 24）→ 跳过推送，不写 hash。"""
    from features.market_snapshot.usecase import SnapshotPushConfig, SnapshotPushUsecase

    BUCKET = 7200
    now_bucket = 100_000
    stale_rows = [{"bucket_ts": now_bucket - 25 * BUCKET}]  # lag=25

    h = _FakeHash()
    uc = SnapshotPushUsecase(
        catalog=_SnapCatalog(),
        collector=_SnapCollector(),
        query=_SnapQuery(stale_rows),
        renderer=_SnapRenderer(),
        router=ChannelRouter([_GoodChannel()], _Provider()),
        body_hash=h,
        config=SnapshotPushConfig(
            dedup=True,
            now_bucket=now_bucket,
            bucket_seconds=BUCKET,
            max_stale_buckets=24,
        ),
        health={},
    )

    status = asyncio.run(uc.run())
    assert status == "stale_skip"
    assert h.recorded == []  # 陈旧守卫触发，提前 return，没走到 dispatch/record
    assert uc._health["stale_skipped"] == 1
    assert uc._health["last_stale_lag"] == 25


def test_f2_normal_lag_pushes_ok():
    """最新桶落后 1 个桶（正常采集间隔）→ 正常推送并写指纹。"""
    from features.market_snapshot.usecase import SnapshotPushConfig, SnapshotPushUsecase

    BUCKET = 7200
    now_bucket = 100_000
    fresh_rows = [{"bucket_ts": now_bucket - BUCKET}]  # lag=1

    h = _FakeHash()
    uc = SnapshotPushUsecase(
        catalog=_SnapCatalog(),
        collector=_SnapCollector(),
        query=_SnapQuery(fresh_rows),
        renderer=_SnapRenderer(),
        router=ChannelRouter([_GoodChannel()], _Provider()),
        body_hash=h,
        config=SnapshotPushConfig(
            dedup=True,
            now_bucket=now_bucket,
            bucket_seconds=BUCKET,
            max_stale_buckets=24,
        ),
        health={},
    )

    asyncio.run(uc.run())
    assert h.recorded == ["snap-body"]  # 正常推送并写指纹


def test_f2_stale_guard_disabled_when_zero():
    """max_stale_buckets=0 时不启用陈旧守卫。"""
    from features.market_snapshot.usecase import SnapshotPushConfig, SnapshotPushUsecase

    BUCKET = 7200
    now_bucket = 100_000
    very_stale = [{"bucket_ts": now_bucket - 100 * BUCKET}]  # lag=100

    h = _FakeHash()
    uc = SnapshotPushUsecase(
        catalog=_SnapCatalog(),
        collector=_SnapCollector(),
        query=_SnapQuery(very_stale),
        renderer=_SnapRenderer(),
        router=ChannelRouter([_GoodChannel()], _Provider()),
        body_hash=h,
        config=SnapshotPushConfig(
            dedup=True,
            now_bucket=now_bucket,
            bucket_seconds=BUCKET,
            max_stale_buckets=0,  # 关闭
        ),
        health={},
    )

    asyncio.run(uc.run())
    assert h.recorded == ["snap-body"]  # 关闭时正常推送


# ── 改造点 3：模块导入自检 ─────────────────────────────────────────────

def test_modules_import_cleanly():
    """所有改动过的模块能正常 import（语法/类型/导入无报错）。"""
    import features.trader.usecase  # noqa: F401
    import features.market_snapshot.usecase  # noqa: F401
    import framework.bootstrap  # noqa: F401
    import framework.app  # noqa: F401
