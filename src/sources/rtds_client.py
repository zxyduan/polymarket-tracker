"""RTDS WebSocket 实时数据客户端（L1）。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import websockets

from src.core.models import StandardEvent, TRADE
from src.core.parsing import parse_rtds_message
from src.utils.retry import backoff_delay

if TYPE_CHECKING:
    from src.storage import Storage

logger = logging.getLogger(__name__)

SUBSCRIBE_MESSAGE = {
    "action": "subscribe",
    "subscriptions": [
        {"topic": "activity", "type": "trades"},
        {"topic": "activity", "type": "orders_matched"},
    ],
}


class RtdsClient:
    """RTDS 连接管理：自动重连、订阅、本地过滤。"""

    def __init__(
        self,
        url: str,
        *,
        heartbeat_seconds: int = 15,
        reconnect_min_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        volume_tracker: Any | None = None,
    ) -> None:
        self.url = url
        self.heartbeat_seconds = heartbeat_seconds
        self.reconnect_min = reconnect_min_seconds
        self.reconnect_max = reconnect_max_seconds
        self.connected = False
        # 快照成交量累加器：全量成交先记账，再按钱包过滤（供快照闭合桶补算）
        self._volume_tracker = volume_tracker

    async def run(
        self,
        event_queue: asyncio.Queue[StandardEvent],
        storage: "Storage",
        reconcile_event: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> None:
        """持续运行；连接断开后指数退避重连，重连后立即触发对账。"""
        attempt = 0
        while not stop_event.is_set():
            try:
                await self._stream(event_queue, storage)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 连接层兜底，保证常驻
                logger.warning("RTDS 连接异常: %s", exc)
            finally:
                self.connected = False

            attempt += 1
            delay = backoff_delay(attempt, self.reconnect_min, self.reconnect_max)
            logger.info("RTDS %.1fs 后重连", delay)
            reconcile_event.set()
            await asyncio.sleep(delay)

    async def _stream(
        self,
        event_queue: asyncio.Queue[StandardEvent],
        storage: "Storage",
    ) -> None:
        watched = await storage.active_addresses()
        last_refresh = asyncio.get_event_loop().time()

        async with websockets.connect(
            self.url,
            open_timeout=15,
            ping_interval=self.heartbeat_seconds,
            ping_timeout=10,
            max_queue=1024,
        ) as ws:
            await ws.send(json.dumps(SUBSCRIBE_MESSAGE))
            self.connected = True
            logger.info("RTDS 已连接并订阅 activity")

            async for raw_message in ws:
                loop_time = asyncio.get_event_loop().time()
                if loop_time - last_refresh > 5:
                    watched = await storage.active_addresses()
                    last_refresh = loop_time

                if isinstance(raw_message, bytes):
                    text = raw_message.decode("utf-8", errors="ignore")
                else:
                    text = raw_message
                text = text.strip()
                if not text or text in ("PING", "pong", "PONG"):
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("RTDS 非 JSON 消息，已忽略: %.80s", text)
                    continue

                for event in parse_rtds_message(data):
                    # 快照成交量记账：全量成交（含非监控钱包）按桶累计，
                    # 供快照采集器补算上一闭合桶的真实成交量增量。
                    # 记账是附属功能，异常必须隔离，绝不能拖垮 RTDS 主链路。
                    if (
                        self._volume_tracker is not None
                        and event.event_type == TRADE
                    ):
                        try:
                            self._volume_tracker.record(
                                event.condition_id,
                                event.notional,
                                event.event_time,
                                event.price,
                            )
                        except Exception as exc:  # noqa: BLE001 - 记账失败不影响交易员监控
                            logger.warning("snap volume record failed: %s", exc)
                    if event.wallet in watched:
                        event_queue.put_nowait(event)
