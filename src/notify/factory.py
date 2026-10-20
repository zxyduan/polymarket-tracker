"""按配置构建推送通道列表（notify.platforms：telegram | ntfy，可多平台同时启用）。"""

from __future__ import annotations

import logging
from typing import Any

from src.notify.base import Notifier
from src.notify.ntfy import NtfyNotifier
from src.notify.telegram import TelegramNotifier
from src.settings import Settings

logger = logging.getLogger(__name__)


def build_notifiers(
    settings: Settings,
    storage: Any,
    telegram_api: Any,
    auth: Any,
    *,
    kind: str = "trader",
) -> list[Notifier]:
    """按 notify.platforms 列表逐个构建推送通道。

    - ntfy：从 [ntfy] 节读取 server/topic（kind=snapshot 时优先 snapshot_topic）；
      配置缺失时记 warning 跳过该平台。
    - telegram：需要已构建的 telegram_api（即 token 已配置），否则跳过并告警。
    - 未知平台：记 warning 跳过（不阻断其他平台）。
    """
    notifiers: list[Notifier] = []
    for platform in settings.notify_platforms:
        if platform == "ntfy":
            notifier = _build_ntfy(settings, storage, kind)
            if notifier is not None:
                notifiers.append(notifier)
        elif platform == "telegram":
            if telegram_api is None:
                logger.warning(
                    "notify 含 telegram 但 BOT_TOKEN/SNAPSHOT_BOT_TOKEN 未配置，"
                    "telegram 通道禁用"
                )
                continue
            notifiers.append(TelegramNotifier(telegram_api, storage, auth))
        else:
            logger.warning("未知推送平台: %s（已忽略）", platform)
    return notifiers


def _build_ntfy(
    settings: Settings, storage: Any, kind: str
) -> NtfyNotifier | None:
    ntfy_cfg = settings.ntfy
    server = str(ntfy_cfg.get("server", "https://ntfy.sh")).strip()
    topic_key = "snapshot_topic" if kind == "snapshot" else "topic"
    topic = str(ntfy_cfg.get(topic_key, "")).strip() or str(
        ntfy_cfg.get("topic", "")
    ).strip()
    if not topic:
        logger.warning(
            "notify 含 ntfy 但 [ntfy] 未配置 %s，%s 推送禁用", topic_key, kind
        )
        return None
    return NtfyNotifier(
        server=server,
        topic=topic,
        access_token=str(ntfy_cfg.get("access_token", "")).strip(),
        title_prefix=str(ntfy_cfg.get("title_prefix", "")).strip(),
        priority=str(ntfy_cfg.get("priority", "")).strip(),
        tags=str(ntfy_cfg.get("tags", "")).strip(),
        storage=storage,
    )
