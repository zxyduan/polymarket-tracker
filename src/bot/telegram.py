"""Telegram Bot API 封装与收发循环。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.core.renderer import AlertMessage
from src.utils.retry import with_retry

logger = logging.getLogger(__name__)


async def run_sender(
    notifiers: list[Any],
    message_queue: asyncio.Queue[AlertMessage],
    stop_event: asyncio.Event,
) -> None:
    """消费告警队列，经全部推送通道投递（限速/重试/落库由各通道实现）。

    notifiers 为 src.notify.Notifier 实现列表（telegram / ntfy / 未来平台），
    同一告警依次发送到每个通道，单通道失败不影响其他通道。
    """
    while not stop_event.is_set():
        alert = await message_queue.get()
        for notifier in notifiers:
            try:
                await notifier.send(
                    alert.html, buttons=alert.buttons, event_keys=alert.event_keys
                )
            except Exception as exc:  # noqa: BLE001 - 通道失败不阻断队列
                logger.error("推送失败(%s): %s", notifier.describe(), exc)
        message_queue.task_done()


class TelegramApi:
    """Telegram Bot API 的最小封装。"""

    def __init__(self, token: str, *, timeout: int = 30) -> None:
        if not token:
            raise ValueError(
                "BOT_TOKEN 未配置：请在 .env 中填入 Bot Token（@BotFather 创建）"
            )
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # getUpdates 是长轮询（默认 timeout=30），客户端读取超时必须大于轮询超时，
        # 否则每次都会在服务端返回前误报超时。
        read_timeout = timeout if timeout is not None else self.timeout
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=read_timeout, write=30, pool=10)
        ) as client:
            response = await client.post(f"{self.base}/{method}", json=payload)
            data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API 错误: {data.get('description')}")
        return data.get("result", {})

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload)

    async def get_updates(
        self, offset: int | None, timeout: int = 30
    ) -> list[dict[str, Any]]:
        # 读取超时 65s > 轮询 30s，预留网络延迟余量
        return await self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout},
            timeout=65,
        )

    async def answer_callback(self, callback_id: str) -> None:
        try:
            await self._call("answerCallbackQuery", {"callback_query_id": callback_id})
        except Exception as exc:  # noqa: BLE001 - 回调应答失败不影响主流程
            logger.debug("answerCallbackQuery 失败: %s", exc)


class BotRunner:
    """Bot 收发任务管理。"""

    def __init__(self, api: TelegramApi, storage: Any, auth: Any) -> None:
        self.api = api
        self.storage = storage
        self.auth = auth

    async def run_sender(
        self,
        message_queue: asyncio.Queue[AlertMessage],
        stop_event: asyncio.Event,
    ) -> None:
        """消费消息队列，向所有授权 chat 推送；限速 + 重试 + 落库。"""
        while not stop_event.is_set():
            alert = await message_queue.get()
            try:
                reply_markup = {"inline_keyboard": alert.buttons} if alert.buttons else None
                for chat_id in self.auth.chats():
                    try:
                        await with_retry(
                            lambda: self.api.send_message(
                                chat_id, alert.html, reply_markup
                            ),
                            attempts=3,
                            description="telegram 推送",
                        )
                        await self.storage.record_notification(
                            str(chat_id),
                            alert.html,
                            ",".join(alert.event_keys),
                            "success",
                        )
                    except Exception as exc:  # noqa: BLE001 - 单 chat 失败不影响其他
                        logger.error("推送给 %s 失败: %s", chat_id, exc)
                        await self.storage.record_notification(
                            str(chat_id),
                            alert.html,
                            ",".join(alert.event_keys),
                            "failed",
                        )
                    await asyncio.sleep(0.4)  # 多 chat 限速
            finally:
                message_queue.task_done()

    async def run_updates(
        self,
        command_handler: Any,
        stop_event: asyncio.Event,
    ) -> None:
        """long polling 接收指令并分发。"""
        offset: int | None = None
        while not stop_event.is_set():
            try:
                updates = await with_retry(
                    lambda: self.api.get_updates(offset, timeout=30),
                    attempts=3,
                    base_delay=2.0,
                    description="getUpdates",
                )
            except Exception as exc:  # noqa: BLE001 - 拉取失败退避后继续
                logger.warning("getUpdates 持续失败: %s，10s 后重试", exc)
                await asyncio.sleep(10)
                continue

            for update in updates:
                offset = int(update["update_id"]) + 1
                await self._dispatch(update, command_handler)

    async def _dispatch(
        self, update: dict[str, Any], command_handler: Any
    ) -> None:
        try:
            if "message" in update:
                message = update["message"]
                chat_id = int(message["chat"]["id"])
                text = message.get("text", "")
                # 白名单为空时首个用户自动成为管理员
                if not await self.auth.is_allowed(chat_id):
                    became_admin = await self.auth.ensure_admin(chat_id)
                    if not became_admin:
                        logger.info("拒绝非授权 chat 指令: %s", chat_id)
                        return
                reply = await command_handler(text, chat_id)
                if reply:
                    await self.api.send_message(chat_id, reply)
            elif "callback_query" in update:
                cb = update["callback_query"]
                await self.api.answer_callback(str(cb["id"]))
        except Exception as exc:  # noqa: BLE001 - 单条更新处理失败不影响轮询
            logger.warning("处理更新失败: %s", exc)
