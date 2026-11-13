"""Telegram 指令处理。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.core.address import is_address
from src.core.renderer import html_escape
from src.settings import save_watchlist_yaml

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "<b>market 交易员监控</b>\n"
    "支持指令：\n"
    "/add 0x地址 名称 — 新增监控（可加多个）\n"
    "/remove 0x地址 — 移除监控\n"
    "/list — 查看监控名单\n"
    "/status — 查看运行状态\n"
    "/test — 链路测试\n"
    "/help — 帮助"
)


class CommandHandler:
    """解析并执行 Telegram 指令。"""

    def __init__(
        self,
        storage: Any,
        health: dict[str, Any],
        *,
        digest_manager: Any = None,
        config_path: str | Path | None = None,
    ) -> None:
        self.storage = storage
        self.health = health
        # digest 摘要管理器（可选）：/remove 时清理该地址的未发摘要桶
        self.digest_manager = digest_manager
        self._config_path = config_path

    async def _persist_watchlist(self) -> None:
        """以 DB 当前 active 列表回写 config.yaml；未配置路径则跳过。"""
        if self._config_path is None:
            return
        wallets = await self.storage.list_wallets(active_only=True)
        save_watchlist_yaml(self._config_path, wallets)

    async def __call__(self, text: str, chat_id: int) -> str | None:
        parts = text.strip().split()
        if not parts:
            return HELP_TEXT
        command = parts[0].split("@")[0].lower()

        if command in ("/start", "/help"):
            return HELP_TEXT
        if command == "/add":
            return await self._add(parts[1:])
        if command == "/remove":
            return await self._remove(parts[1:])
        if command == "/list":
            return await self._list()
        if command == "/status":
            return await self._status()
        if command == "/test":
            return "✅ 链路正常：监控服务运行中，可接收实时推送"
        return "未识别指令，发送 /help 查看可用指令"

    async def _add(self, args: list[str]) -> str:
        if not args:
            return "用法：/add 0x地址 名称（名称可选）"
        address = args[0].strip()
        if not is_address(address):
            return "地址格式错误，请提供合法的 0x 开头 40 位地址"
        label = " ".join(args[1:]).strip()
        ok, message, action = await self.storage.add_wallet(address, label)
        if ok:
            try:
                await self._persist_watchlist()
            except Exception as exc:  # noqa: BLE001  落盘失败：按操作类型精确回滚
                if action == "inserted":
                    await self.storage.delete_wallet(address)  # 新插入：物理删除
                else:
                    await self.storage.remove_wallet(address)  # 重新激活：恢复停用
                logger.error("watchlist 回写失败，已回滚: %s", exc)
                return f"⚠️ 配置落盘失败，已撤销本次操作：{exc}"
        return f"{'✅' if ok else 'ℹ️'} {message}"

    async def _remove(self, args: list[str]) -> str:
        if not args:
            return "用法：/remove 0x地址"
        address = args[0].strip()
        if not is_address(address):
            return "地址格式错误"
        ok, message = await self.storage.remove_wallet(address)
        if ok:
            if self.digest_manager is not None:
                # 清理该地址未到期的摘要桶（global 模式为 no-op；不可恢复）
                await self.digest_manager.discard_wallet(address)
            try:
                await self._persist_watchlist()
            except Exception as exc:  # noqa: BLE001  落盘失败：重新激活（digest 桶不恢复）
                await self.storage.reactivate_wallet(address)
                logger.error("watchlist 回写失败，已回滚: %s", exc)
                return f"⚠️ 配置落盘失败，已撤销本次操作：{exc}"
        return f"{'✅' if ok else 'ℹ️'} {message}"

    async def _list(self) -> str:
        wallets = await self.storage.list_wallets(active_only=True)
        if not wallets:
            return "监控名单为空，使用 /add 0x地址 名称 添加"
        lines = [f"<b>监控名单（{len(wallets)}）</b>"]
        for i, wallet in enumerate(wallets, 1):
            name = html_escape(wallet.get("label") or "未命名")
            lines.append(f"{i}. {name} · <code>{wallet['address']}</code>")
        return "\n".join(lines)

    async def _status(self) -> str:
        wallets = await self.storage.list_wallets(active_only=True)
        notified = await self.storage.notified_count_today()
        latest = await self.storage.latest_event_time()
        rtds = "🟢 已连接" if self.health.get("rtds_connected") else "🔴 未连接"
        l2 = self.health.get("l2_last_run", "-")
        pipeline_funnel = self.health.get("pipeline_funnel") or {}
        trader_funnel = self.health.get("trader_funnel") or {}
        funnel = (
            f"漏斗：冲突 {pipeline_funnel.get('insert_conflict', 0)}｜"
            f"类型拦截 {pipeline_funnel.get('type_blocked', 0)}｜"
            f"金额不足 {pipeline_funnel.get('notional_below', 0)}｜"
            f"金额缺失 {pipeline_funnel.get('notional_none', 0)}｜"
            f"入聚合 {pipeline_funnel.get('accepted', 0)}；"
            f"已分发 {trader_funnel.get('dispatched', 0)}｜"
            f"去重 {trader_funnel.get('dedup_hit', 0)}｜"
            f"渲染空 {trader_funnel.get('render_none', 0)}｜"
            f"失败 {trader_funnel.get('dispatch_failed', 0)}"
        )
        return (
            "<b>运行状态</b>\n"
            f"RTDS 实时通道：{rtds}\n"
            f"最近对账：{l2}\n"
            f"监控地址数：{len(wallets)}\n"
            f"今日推送：{notified} 条\n"
            f"最近事件：{latest or '-'}\n"
            + funnel
        )
