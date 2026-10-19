"""Telegram 指令处理。"""

from __future__ import annotations

from typing import Any

from src.core.address import is_address
from src.core.renderer import html_escape

HELP_TEXT = (
    "<b>Polymarket 交易员监控</b>\n"
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

    def __init__(self, storage: Any, health: dict[str, Any]) -> None:
        self.storage = storage
        self.health = health

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
        ok, message = await self.storage.add_wallet(address, label)
        return f"{'✅' if ok else 'ℹ️'} {message}"

    async def _remove(self, args: list[str]) -> str:
        if not args:
            return "用法：/remove 0x地址"
        address = args[0].strip()
        if not is_address(address):
            return "地址格式错误"
        ok, message = await self.storage.remove_wallet(address)
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
        return (
            "<b>运行状态</b>\n"
            f"RTDS 实时通道：{rtds}\n"
            f"最近对账：{l2}\n"
            f"监控地址数：{len(wallets)}\n"
            f"今日推送：{notified} 条\n"
            f"最近事件：{latest or '-'}"
        )
