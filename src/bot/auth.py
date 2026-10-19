"""Bot 指令鉴权（chat 白名单）。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

STATE_KEY = "allowed_chats"


class AuthManager:
    """授权 chat 管理；环境变量种子 + 持久化；空名单时首个用户成为管理员。

    state_key 可自定义，供多 Bot（交易员 Bot / 快照 Bot）隔离各自白名单。
    """

    def __init__(
        self,
        seed_chats: list[int],
        storage: Any,
        state_key: str = STATE_KEY,
    ) -> None:
        self.storage = storage
        self._state_key = state_key
        self._chats: set[int] = set(seed_chats)

    async def load(self) -> None:
        raw = await self.storage.get_state(self._state_key)
        if raw:
            for item in raw.split(","):
                item = item.strip()
                if item.lstrip("-").isdigit():
                    self._chats.add(int(item))
        logger.info("已授权 chat 数: %d", len(self._chats))

    async def _persist(self) -> None:
        await self.storage.set_state(
            self._state_key, ",".join(str(chat) for chat in sorted(self._chats))
        )

    async def is_allowed(self, chat_id: int) -> bool:
        return chat_id in self._chats

    async def ensure_admin(self, chat_id: int) -> bool:
        """白名单为空时，将首个对话用户登记为管理员。返回是否登记成功。"""
        if not self._chats:
            self._chats.add(chat_id)
            await self._persist()
            logger.warning("已登记首个用户为管理员: %s", chat_id)
            return True
        return False

    def chats(self) -> list[int]:
        return sorted(self._chats)
