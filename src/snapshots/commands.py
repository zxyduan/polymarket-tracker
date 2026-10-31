"""快照 Bot 指令处理（独立于交易员 Bot 的指令集）。"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from src.settings import MarketEntry, save_markets_toml
from src.snapshots.renderer import render_market_list, render_tracker_status

SNAP_HELP = (
    "<b>市场快照推送</b>\n"
    "每 2 小时采集自选市场，时间 06/09/12/15/18/20/24 推送总览\n"
    "支持指令：\n"
    "/snap now — 立即采集一轮并推送\n"
    "/vol — RTDS 成交量记账实时状态\n"
    "/list — 查看监控市场\n"
    "/add &lt;slug&gt; [slug_id] — 新增盘口（slug 为 market 市场完整 slug）\n"
    "/remove &lt;slug_id|slug&gt; — 删除盘口\n"
    "/status — 查看快照服务状态\n"
    "/daily now — 立即生成并推送每日成交份额汇总\n"
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
        markets_path: str | None = None,
        *,
        trigger_daily: Callable[[], Awaitable[str]] | None = None,
        daily_health: dict[str, Any] | None = None,
    ) -> None:
        self.markets = markets
        self.collector = collector
        self.storage = storage
        self.trigger_snapshot = trigger_snapshot
        self.health = health
        self.markets_path = markets_path
        self.trigger_daily = trigger_daily
        self.daily_health = daily_health

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
            return await render_tracker_status(self.storage, self.collector)
        if command == "/status":
            return await self._status()
        if command == "/add":
            return await self._add_market(parts[1:])
        if command == "/remove":
            return await self._remove_market(parts[1:])
        if command == "/daily":
            return await self._daily_now(parts[1:])
        return "未识别指令，发送 /help 查看可用指令"

    async def _daily_now(self, args: list[str]) -> str:
        """/daily now：立即生成并推送一次日报；写入当日 state，参与防重。"""
        if self.trigger_daily is None:
            return "日报功能未接入"
        if args and args[0].lower() != "now":
            return "用法：/daily now"
        result = await self.trigger_daily()
        if self.daily_health is not None:
            self.daily_health["daily_result"] = result
        return daily_result_reply(result)

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

    # ── 盘口动态管理 ─────────────────────────────────────────────────

    async def _add_market(self, args: list[str]) -> str:
        """/add <slug> [slug_id]：解析 slug → 落盘 markets.toml → 内存生效。"""
        if not args:
            return (
                "<b>/add 用法</b>\n"
                "/add &lt;slug&gt; [slug_id]\n"
                "slug：market 市场完整 slug（如 clarity-act-signed-into-law-in-2026）\n"
                "slug_id：推送中显示的短码，不填则用 slug"
            )
        slug = args[0].strip().lower()
        slug_id = (args[1].strip() if len(args) > 1 else "") or slug

        if any(m.slug == slug for m in self.markets):
            return f"⚠️ 市场已存在：{slug}（/list 查看）"
        if any(m.slug_id == slug_id for m in self.markets):
            return f"⚠️ slug_id 已存在：{slug_id}（/list 查看）"

        resolved = await self.collector.resolve_market(slug)
        if not resolved:
            return (
                f"❌ 解析失败：{slug}\n"
                "确认 slug 正确（market 页面 URL 末尾），或稍后重试"
            )
        condition_id, _ = resolved

        entry = MarketEntry(
            slug_id=slug_id,
            slug=slug,
            condition_id=condition_id,
            enabled=True,
            profile="",
        )
        self.markets.append(entry)
        # 落盘（写前自动 .bak）；失败回滚内存，保证内存与磁盘一致
        if self.markets_path:
            try:
                save_markets_toml(self.markets_path, self.markets)
            except Exception as exc:  # noqa: BLE001 - 落盘失败必须回滚
                self.markets.remove(entry)
                return f"❌ 写入配置失败，未添加：{exc}"
        return (
            f"✅ 已新增盘口：{slug_id}\n"
            f"slug：{slug}\n"
            f"condition_id：{condition_id[:12]}…\n"
            "已落盘，重启后仍保留；下轮采集（≤2h）自动纳入，"
            "或发 /snap now 立即采集"
        )

    async def _remove_market(self, args: list[str]) -> str:
        """/remove <slug_id|slug>：按 slug_id 或 slug 匹配并删除。"""
        if not args:
            return (
                "<b>/remove 用法</b>\n"
                "/remove &lt;slug_id|slug&gt;\n"
                "按 /list 显示的 slug_id 或市场 slug 删除"
            )
        key = args[0].strip().lower()
        matched = [m for m in self.markets if m.slug_id == key or m.slug == key]
        if not matched:
            return (
                f"⚠️ 未找到：{key}\n"
                "用 /list 查看当前 slug_id / slug，复制后重试"
            )
        removed = matched[0]
        self.markets.remove(removed)
        if self.markets_path:
            try:
                save_markets_toml(self.markets_path, self.markets)
            except Exception as exc:  # noqa: BLE001 - 落盘失败必须回滚
                self.markets.insert(0, removed)
                return f"❌ 写入配置失败，未删除：{exc}"
        return (
            f"🗑️ 已删除盘口：{removed.slug_id}（{removed.slug}）\n"
            "已落盘，重启后仍生效；历史快照数据保留"
        )


# ── 每日成交份额汇总指令 ────────────────────────────────────────────────


def daily_result_reply(result: str) -> str:
    """把 trigger_daily_report 结果码映射为用户可见回执（两套 handler 共用）。"""
    return {
        "sent": "✅ 日报已推送",
        "degraded": "⚠️ 日报已推送（降级正文）",
        "selfcheck_failed": "❌ 单调自检未通过，日报已拒发（见日志）",
        "sent_dup": "ℹ️ 正文与上一条完全一致，已抑制",
        "send_failed": "❌ 日报发送失败，可稍后重试",
    }.get(result, f"日报结果：{result}")


DAILY_HELP = (
    "<b>每日成交份额汇总</b>\n"
    "/daily now — 立即生成并推送一次日报\n"
    "/help — 帮助"
)


class DailyCommandHandler:
    """独立日报 Bot 的指令处理（不暴露 /snap /vol /add 等快照指令）。"""

    def __init__(
        self,
        *,
        trigger_daily: Callable[[], Awaitable[str]],
        daily_health: dict[str, Any] | None = None,
    ) -> None:
        self.trigger_daily = trigger_daily
        self.daily_health = daily_health

    async def __call__(self, text: str, chat_id: int) -> str | None:
        parts = text.strip().split()
        if not parts:
            return DAILY_HELP
        command = parts[0].split("@")[0].lower()

        if command in ("/start", "/help"):
            return DAILY_HELP
        if command == "/daily":
            if parts[1:] and parts[1].lower() != "now":
                return "用法：/daily now"
            result = await self.trigger_daily()
            if self.daily_health is not None:
                self.daily_health["daily_result"] = result
            return daily_result_reply(result)
        return "未识别指令，发送 /help 查看可用指令"
