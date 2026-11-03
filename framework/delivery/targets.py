"""投递目标泛化（设计 v3.5 §7.3）。

targets.py 是全项目唯一定义点；新增通道不必修改联合类型。
RecipientProvider.targets() 返回 list[DeliveryTarget]，
各通道按 target.channel 过滤；用构造器与测试约束，不用 isinstance 分支。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class DeliveryTarget:
    channel: str                 # "telegram" / "ntfy" / future
    address: str                 # chat_id / topic / webhook URL
    options: Mapping[str, str] = field(default_factory=dict)


def telegram_chat(chat_id: str) -> DeliveryTarget:
    return DeliveryTarget(channel="telegram", address=chat_id)


def ntfy_topic(name: str) -> DeliveryTarget:
    return DeliveryTarget(channel="ntfy", address=name)
