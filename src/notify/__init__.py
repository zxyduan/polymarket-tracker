"""推送通道抽象：告警/快照统一走 Notifier，支持 telegram / ntfy 多平台可插拔。"""

from src.notify.base import Notifier
from src.notify.factory import build_notifiers
from src.notify.ntfy import NtfyNotifier
from src.notify.telegram import TelegramNotifier

__all__ = ["Notifier", "NtfyNotifier", "TelegramNotifier", "build_notifiers"]
