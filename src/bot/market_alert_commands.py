"""白名单市场告警 Bot 指令处理（T9）。

鉴权由 `BotRunner` + 独立 `AuthManager` 完成（`is_allowed` / `ensure_admin`
在 runner `_dispatch` 内），本 handler 不自己判白名单；不回写 markets.toml。
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

MARKET_ALERT_HELP = (
    "<b>白名单市场告警</b>\n"
    "监控 markets.toml 清单市场的奖励变化与成交推送\n"
    "支持指令：\n"
    "/status — 查看告警运行状态\n"
    "/help — 帮助"
)


class MarketAlertCommandHandler:
    """独立轻量指令集：/start 授权登记回执 + /status 健康摘要。"""

    def __init__(
        self, storage: Any, health: MutableMapping[str, Any]
    ) -> None:
        self._storage = storage
        self._health = health

    async def __call__(self, text: str, chat_id: int) -> str | None:
        parts = text.strip().split()
        if not parts:
            return MARKET_ALERT_HELP
        command = parts[0].split("@")[0].lower()

        if command in ("/start", "/help"):
            return MARKET_ALERT_HELP
        if command == "/status":
            return self._status()
        return "未识别指令，发送 /help 查看可用指令"

    def _status(self) -> str:
        health = self._health
        dispatched = health.get("dispatched", 0)
        failures = health.get("dispatch_failed", 0)
        dedup = health.get("dedup_hit", 0)
        cross_link = health.get("cross_link_hit", 0)
        return (
            "<b>白名单市场告警状态</b>\n"
            f"已推送：{dispatched}\n"
            f"通道失败：{failures}\n"
            f"指纹去重：{dedup}\n"
            f"跨链路抑制：{cross_link}"
        )
