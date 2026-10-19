"""快照 Bot 指令处理（独立于交易员 Bot 的指令集）。"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from src.snapshots.renderer import render_market_list, render_tracker_status

SNAP_HELP = (
    "<b>市场快照推送</b>\n"
    "每 2 小时采集自选市场，北京时间 06/09/12/15/18/20/24 推送总览\n"
    "支持指令：\n"
    "/snap now — 立即采集一轮并推送\n"
    "/vol — RTDS 成交量记账实时状态\n"
    "/list — 查看监控市场\n"
    "/status — 查看快照服务状态\n"
    "/help — 帮助"
)


class SnapshotCommandHandler:
    """快照 Bot 的指令处理。"""

    def __init__(
        self,
        markets: list[Any],
        collector: Any,
        storage: Any,
        trigger_snapshot: Callable[[], Awaitable[None]],
        health: dict[str, Any],
        volume_tracker: Any | None = None,
    ) -> None:
        self.markets = markets
        self.collector = collector
        self.storage = storage
        self.trigger_snapshot = trigger_snapshot
        self.health = health
        self.volume_tracker = volume_tracker

    async def __call__(self, text: str, chat_id: int) -> str | None:
        parts = text.strip().split()
        if not parts:
            return SNAP_HELP
        command = parts[0].split("@")[0].lower()

        if command in ("/start", "/help"):
            return SNAP_HELP
        if command == "/list":
            return render_market_list(self.markets)
        if command == "/snap":
            return await self._snap_now()
        if command == "/vol":
            return await render_tracker_status(self.storage, self.volume_tracker)
        if command == "/status":
            return await self._status()
        return "未识别指令，发送 /help 查看可用指令"

    async def _snap_now(self) -> str:
        await self.trigger_snapshot()
        return "✅ 已触发一轮采集并推送，稍后查看消息"

    async def _status(self) -> str:
        rows = await self.storage.latest_complete_buckets(int(time.time()))
        return (
            "<b>快照服务状态</b>\n"
            f"监控市场：{len(self.markets)}\n"
            f"最近有数据市场：{len(rows)}\n"
            f"最近推送：{self.health.get('snap_last_push', '-')}"
        )
