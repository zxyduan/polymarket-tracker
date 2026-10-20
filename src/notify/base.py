"""推送通道抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Notifier(ABC):
    """消息推送通道。send() 负责向全部接收方投递并记录推送状态。

    约定：投递失败必须抛异常，由上层（sender 循环）统一降级；
    通道内部负责多接收方遍历、限速、重试与落库（如适用）。
    """

    @abstractmethod
    async def send(
        self,
        text: str,
        *,
        buttons: list[dict[str, Any]] | None = None,
        event_keys: list[str] | None = None,
    ) -> None:
        """投递一条消息。buttons 为可选交互按钮（不支持交互的平台忽略）。"""

    @abstractmethod
    def describe(self) -> str:
        """通道描述（日志用），如 "telegram@2 chats" / "ntfy@https://…/topic"。"""
