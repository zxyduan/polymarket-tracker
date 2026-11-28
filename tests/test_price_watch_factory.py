"""price_watch 工厂 topic 分支测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.notify.factory import build_notifiers
from src.notify.ntfy import NtfyNotifier
from src.settings import Settings


def _settings(tmp_path: Path, raw: dict[str, Any]) -> Settings:
    p = tmp_path / "config.yaml"
    return Settings(
        bot_token="", allowed_chat_ids=[], log_level="INFO",
        config_path=p, raw=raw,
    )


def test_price_topic_used_when_present(tmp_path: Path) -> None:
    raw = {
        "notify": {"platform": "ntfy"},
        "ntfy": {"server": "https://ntfy.sh", "topic": "general",
                 "price_topic": "price-alerts"},
    }
    notifiers = build_notifiers(_settings(tmp_path, raw), None, None, None,
                                kind="price_watch")
    assert len(notifiers) == 1
    assert isinstance(notifiers[0], NtfyNotifier)
    assert "price-alerts" in notifiers[0].describe()


def test_fallback_to_general_topic(tmp_path: Path) -> None:
    raw = {
        "notify": {"platform": "ntfy"},
        "ntfy": {"server": "https://ntfy.sh", "topic": "general"},
    }
    notifiers = build_notifiers(_settings(tmp_path, raw), None, None, None,
                                kind="price_watch")
    assert len(notifiers) == 1
    assert "general" in notifiers[0].describe()


def test_empty_topics_disabled(tmp_path: Path) -> None:
    raw = {"notify": {"platform": "ntfy"}, "ntfy": {"server": "https://ntfy.sh"}}
    assert build_notifiers(_settings(tmp_path, raw), None, None, None,
                           kind="price_watch") == []


def test_no_fallback_to_other_special_topics(tmp_path: Path) -> None:
    """price_watch 不得回退 snapshot/daily/market 专用 topic。"""
    raw = {
        "notify": {"platform": "ntfy"},
        "ntfy": {
            "server": "https://ntfy.sh",
            "snapshot_topic": "snap",
            "daily_topic": "daily",
            "market_topic": "market",
        },
    }
    assert build_notifiers(_settings(tmp_path, raw), None, None, None,
                           kind="price_watch") == []


def test_telegram_without_token_skipped_ntfy_kept(tmp_path: Path) -> None:
    raw = {
        "notify": {"platforms": ["ntfy", "telegram"]},
        "ntfy": {"server": "https://ntfy.sh", "topic": "general"},
    }
    notifiers = build_notifiers(_settings(tmp_path, raw), None, None, None,
                                kind="price_watch")
    assert len(notifiers) == 1
    assert isinstance(notifiers[0], NtfyNotifier)
