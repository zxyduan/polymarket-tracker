"""逐目标结果与逐通道隔离路由（设计 v3.5 §7.4，S-02/T-01/T-02/N-04）。

作用域（N-04）：每个 feature 独立构造一个 ChannelRouter（F1/F2/F3 三套），
self._channels 与 self._provider 只含本 feature 的通道；
dispatch(feature, msg) 的 feature 参数只用于落痕归属与日志，不承担跨 feature 路由；
禁止用一个全局 router 混投。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence, runtime_checkable

from framework.models.message import RenderedMessage
from framework.delivery.targets import DeliveryTarget


@dataclass(frozen=True)
class TargetOutcome:
    target: DeliveryTarget
    status: Literal["success", "failed"]
    error: str | None = None
    error_kind: Literal["target", "channel"] = "target"


@dataclass(frozen=True)
class ChannelFailure:
    channel: str                 # 通道名，如 "ntfy"
    error: str
    reference: str = ""           # 通道自述，如 "ntfy@https://..."（T-02）


@dataclass(frozen=True)
class DeliveryResult:
    outcomes: tuple[TargetOutcome, ...]
    channel_failures: tuple[ChannelFailure, ...] = ()
    configured_channels: int = 0  # 构造时的通道数，与 targets 是否存在无关（T-01）


@runtime_checkable
class Channel(Protocol):
    name: str

    def describe(self) -> str:
        """Telegram: "telegram@{N} chats"；ntfy: "ntfy@{url}"（T-02）。"""
        ...

    async def deliver(
        self, msg: RenderedMessage, targets: Sequence[DeliveryTarget]
    ) -> DeliveryResult:
        ...


class RecipientProvider(Protocol):
    def targets(self) -> list[DeliveryTarget]:
        ...


class ChannelRouter:
    """按 feature 构造；逐通道 catch 后继续，不抛异常（S-02b）。"""

    def __init__(self, channels: Sequence[Channel], provider: RecipientProvider) -> None:
        self._channels = tuple(channels)
        self._provider = provider

    async def dispatch(self, feature: str, msg: RenderedMessage) -> DeliveryResult:
        outcomes: list[TargetOutcome] = []
        failures: list[ChannelFailure] = []
        for channel in self._channels:
            targets = [t for t in self._provider.targets() if t.channel == channel.name]
            if not targets:
                continue
            try:
                result = await channel.deliver(msg, targets)
                outcomes.extend(result.outcomes)
                failures.extend(result.channel_failures)
            except Exception as exc:  # 单通道异常不阻断其他通道
                failures.append(ChannelFailure(
                    channel=channel.name,
                    error=str(exc),
                    reference=channel.describe(),   # T-02
                ))
        return DeliveryResult(
            outcomes=tuple(outcomes),
            channel_failures=tuple(failures),
            configured_channels=len(self._channels),  # T-01
        )
