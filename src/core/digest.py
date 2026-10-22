"""交易告警批量摘要（digest）：窗口攒批 + 累计份额/USDC 阈值提前触发。

状态机（每个摘要桶一个实例）：
- 首条消息到达开窗口并启动 window_seconds 定时器；后续消息只累计，不重置计时；
- 累计份额 ≥ flush_shares 或累计 USDC ≥ flush_usdc（阈值为 0 表示该方向不启用；
  两个方向是「或」关系）立即 flush，不等窗口结束；
- 定时器到期，桶内非空则 flush；空桶不发任何消息；
- flush 后清空事件/计数/定时器，下一条消息到达再开新窗口重新累计；
- 阈值触发不限次数：连续大单连续触发、连续推送，不合并、不延迟大单。

scope=global（已确认生产配置）：全系统一个桶，key 固定 __all__；
scope=wallet：每个活跃地址一个桶，代码保留该分支。

注意：本模块只负责 TRADE 成交的攒批。生命周期事件（SPLIT/MERGE/REDEEM/
CONVERT）不进摘要，由调用方（pipeline）按原逐笔路径即时推送。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from src.core.models import StandardEvent
from src.core.renderer import AlertMessage, render_digest

logger = logging.getLogger("src.core.digest")

# global 模式下全局唯一桶的 key
GLOBAL_BUCKET_KEY = "__all__"


@dataclass(frozen=True)
class DigestConfig:
    """digest 节配置（不可变，启动时解析校验）。"""

    enabled: bool
    scope: str  # "global" | "wallet"
    window_seconds: float
    flush_shares: Decimal  # 0 = 不启用份额方向
    flush_usdc: Decimal  # 0 = 不启用 USDC 方向

    @classmethod
    def from_section(cls, section: dict[str, object]) -> "DigestConfig":
        """从 settings.section("digest") 解析，非法值直接 ValueError（启动失败）。"""
        enabled = bool(section.get("enabled", False))
        scope = str(section.get("scope", "global")).strip().lower()
        if scope not in ("global", "wallet"):
            raise ValueError(
                f"digest.scope 非法: {scope!r}，只支持 global / wallet"
            )
        try:
            window = float(section.get("window_seconds", 60))
        except (TypeError, ValueError) as exc:
            raise ValueError("digest.window_seconds 必须是数字（秒）") from exc
        try:
            flush_shares = Decimal(str(section.get("flush_shares", 0) or 0))
            flush_usdc = Decimal(str(section.get("flush_usdc", 0) or 0))
        except Exception as exc:  # Decimal 解析异常
            raise ValueError(
                "digest.flush_shares / flush_usdc 必须是数字"
            ) from exc
        if window < 5:
            raise ValueError(
                f"digest.window_seconds 必须 ≥ 5 秒，当前 {window}"
            )
        if flush_shares < 0 or flush_usdc < 0:
            raise ValueError(
                "digest.flush_shares / flush_usdc 不能为负数（0 表示不启用该方向）"
            )
        return cls(
            enabled=enabled,
            scope=scope,
            window_seconds=window,
            flush_shares=flush_shares,
            flush_usdc=flush_usdc,
        )


@dataclass
class DigestBucket:
    """一个摘要累计桶（全局桶或单地址桶）。"""

    key: str
    started_at: datetime | None = None  # 窗口起点（首条消息到达时刻，UTC）
    # (wallet, label, 同笔结算事件组)
    groups: list[tuple[str, str, list[StandardEvent]]] = field(default_factory=list)
    total_shares: Decimal = Decimal("0")
    total_usdc: Decimal = Decimal("0")
    buy_count: int = 0
    sell_count: int = 0
    timer: asyncio.Task[None] | None = None

    def add(self, wallet: str, label: str, events: list[StandardEvent]) -> None:
        """入桶并累加；只累计，不改变窗口起点。"""
        if self.started_at is None:
            self.started_at = datetime.now(timezone.utc)
        self.groups.append((wallet, label, events))
        for event in events:
            if event.size is not None:
                self.total_shares += event.size
            if event.notional is not None:
                self.total_usdc += event.notional
            side = (event.side or "").upper()
            if side == "BUY":
                self.buy_count += 1
            elif side == "SELL":
                self.sell_count += 1

    @property
    def is_empty(self) -> bool:
        return not self.groups

    def event_count(self) -> int:
        return sum(len(events) for _, _, events in self.groups)


class DigestManager:
    """摘要桶管理：入桶、定时器、阈值判定、flush 渲染入队。

    纯 asyncio 逻辑：不碰网络、不直接发送；渲染出的 AlertMessage 投递到与
    逐笔告警相同的 out_queue，由现有 run_sender 走全部通道。
    """

    def __init__(
        self,
        config: DigestConfig,
        out_queue: asyncio.Queue[AlertMessage],
        *,
        tz_offset_hours: int = 8,
        polygonscan_base: str = "https://polygonscan.com",
    ) -> None:
        self.cfg = config
        self.out_queue = out_queue
        self.tz_offset_hours = tz_offset_hours
        self.polygonscan_base = polygonscan_base
        self._buckets: dict[str, DigestBucket] = {}

    async def add(
        self,
        wallet: str,
        label: str,
        events: list[StandardEvent],
    ) -> None:
        """一组（同笔结算的）TRADE 事件入桶；命中阈值立即 flush。"""
        if not events:
            return
        key = (
            GLOBAL_BUCKET_KEY
            if self.cfg.scope == "global"
            else (wallet or "").lower()
        )
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = DigestBucket(key=key)
            self._buckets[key] = bucket
            bucket.timer = asyncio.create_task(self._wait_window(key))
            logger.info(
                "digest 窗口开启 key=%s window=%ss",
                key,
                self.cfg.window_seconds,
            )
        bucket.add(wallet, label, events)
        if self._threshold_reached(bucket):
            logger.info(
                "digest 阈值触发 key=%s shares=%s usdc=%s",
                key,
                bucket.total_shares,
                bucket.total_usdc,
            )
            await self._flush(key, reason="threshold")

    async def _wait_window(self, key: str) -> None:
        """固定等待窗口：不被后续消息重置。到点非空则 flush，空桶静默。"""
        try:
            await asyncio.sleep(self.cfg.window_seconds)
        except asyncio.CancelledError:
            return
        await self._flush(key, reason="window")

    def _threshold_reached(self, bucket: DigestBucket) -> bool:
        """两个方向任一启用且达到即触发（或关系）。"""
        if (
            self.cfg.flush_shares > 0
            and bucket.total_shares >= self.cfg.flush_shares
        ):
            return True
        if (
            self.cfg.flush_usdc > 0
            and bucket.total_usdc >= self.cfg.flush_usdc
        ):
            return True
        return False

    async def _flush(self, key: str, *, reason: str) -> None:
        """取出桶 → 渲染 → 投递。先 pop 保证定时器与阈值两路不会重复发。"""
        bucket = self._buckets.pop(key, None)
        if bucket is None or bucket.is_empty:
            return
        if bucket.timer is not None:
            bucket.timer.cancel()
            bucket.timer = None
        elapsed_seconds = 0.0
        if bucket.started_at is not None:
            elapsed_seconds = (
                datetime.now(timezone.utc) - bucket.started_at
            ).total_seconds()
        try:
            message = render_digest(
                bucket,
                scope=self.cfg.scope,
                window_seconds=self.cfg.window_seconds,
                reason=reason,
                elapsed_seconds=elapsed_seconds,
                tz_offset_hours=self.tz_offset_hours,
                polygonscan_base=self.polygonscan_base,
            )
        except Exception:  # noqa: BLE001 - 渲染失败不拖垮流水线
            logger.exception("digest 渲染失败 key=%s，放弃该批", key)
            return
        await self.out_queue.put(message)
        logger.info(
            "digest 已发送 reason=%s key=%s events=%d shares=%s usdc=%s",
            reason,
            key,
            bucket.event_count(),
            bucket.total_shares,
            bucket.total_usdc,
        )

    async def flush_all(self) -> None:
        """停机：把未到期桶尽量补发一次。"""
        for key in list(self._buckets):
            await self._flush(key, reason="shutdown")

    async def discard_wallet(self, wallet: str) -> None:
        """/remove 后清理：global 桶含其他地址，不处理；wallet 模式非空桶补发。"""
        if self.cfg.scope != "wallet":
            return
        key = (wallet or "").lower()
        if key in self._buckets:
            await self._flush(key, reason="remove")

    def pending_keys(self) -> list[str]:
        """当前未 flush 的桶 key（测试/诊断用）。"""
        return list(self._buckets)
