"""watchlist_alert 配置解析测试（fail-soft / 越界回退 / env token）。"""

from __future__ import annotations

from typing import Any

import pytest

from src.settings import resolve_watchlist_alert_config


class FakeSettings:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def section(self, name: str) -> dict[str, Any]:
        return dict(self._raw.get(name, {}))

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def test_defaults_when_section_empty() -> None:
    cfg = resolve_watchlist_alert_config(FakeSettings({}))
    assert cfg.enabled is True
    assert cfg.reward.interval_seconds == 900
    assert cfg.reward.decay_pct_threshold == 0.30
    assert cfg.reward.confirm_rounds == 1
    assert cfg.reward.cooldown_seconds == 86400
    assert cfg.trade.merge_window_seconds == 30
    assert cfg.trade.max_events_per_message == 10
    assert cfg.trade.cross_link_dedup is True


def test_out_of_range_falls_back_to_default() -> None:
    raw = {
        "watchlist_alert": {
            "reward": {
                "watch_interval_seconds": 9999,
                "decay_pct_threshold": 1.5,
                "confirm_rounds": -3,
            },
            "trade": {"merge_window_seconds": 999, "max_events_per_message": 0},
        }
    }
    cfg = resolve_watchlist_alert_config(FakeSettings(raw))
    assert cfg.reward.interval_seconds == 900
    assert cfg.reward.decay_pct_threshold == 0.30
    assert cfg.reward.confirm_rounds == 1
    assert cfg.trade.merge_window_seconds == 30
    assert cfg.trade.max_events_per_message == 10
    assert cfg.warnings  # 越界均记 warnings


def test_switches_propagate() -> None:
    raw = {
        "watchlist_alert": {
            "enabled": False,
            "reward": {"alert_on_exit": False, "alert_on_rebound": True},
            "trade": {"cross_link_dedup": False},
        }
    }
    cfg = resolve_watchlist_alert_config(FakeSettings(raw))
    assert cfg.enabled is False
    assert cfg.reward.alert_on_exit is False
    assert cfg.reward.alert_on_rebound is True
    assert cfg.trade.cross_link_dedup is False


def test_market_alert_bot_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.settings import Settings

    # token property 仅读环境变量，跳过 Settings 重型构造。
    obj = Settings.__new__(Settings)
    monkeypatch.delenv("MARKET_ALERT_BOT_TOKEN", raising=False)
    assert obj.market_alert_bot_token == ""
    monkeypatch.setenv("MARKET_ALERT_BOT_TOKEN", "  tg-123  ")
    assert obj.market_alert_bot_token == "tg-123"
