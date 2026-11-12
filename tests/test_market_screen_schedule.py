"""market_screen 配置解析与调度口径测试（设计 §10.4）。"""

from __future__ import annotations

from typing import Any

from src.settings import resolve_market_screen_config


class _Settings:
    """最小 Settings 桩：只提供 section / get 两个解析所需接口。"""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    @property
    def market_screen(self) -> dict[str, Any]:
        return dict(self.raw.get("market_screen", {}) or {})

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name, {}) or {})

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _resolve(raw: dict[str, Any]):
    return resolve_market_screen_config(_Settings({"market_screen": raw}))


def test_defaults_fallback():
    cfg = _resolve({})
    assert cfg.enabled is True
    assert cfg.schedule == "08:00"
    assert (cfg.schedule_hour, cfg.schedule_minute) == (8, 0)
    assert cfg.main_preset == "balanced"
    assert cfg.record_all_presets is True
    assert cfg.min_value == 50000.0
    assert cfg.top_n == 200
    assert cfg.page_limit == 100
    assert cfg.max_pages == 4
    assert cfg.retention_days == 30
    assert (cfg.clamp_min, cfg.clamp_max) == (0.002, 0.20)
    assert cfg.request_timeout_seconds == 10
    assert cfg.retry_attempts == 2
    assert cfg.retry_backoff_seconds == 1.0
    assert cfg.page_delay_seconds == 0.2
    assert cfg.horizon_enabled is True
    assert cfg.binary_only is True
    assert cfg.persist_non_passed is True
    assert cfg.notify_enabled is False
    assert cfg.mode == "anchor_linear"
    qd = cfg.query_defaults
    assert qd.window_days == 10 and qd.min_hit_days == 10
    assert qd.min_volume_24h == 50000.0 and qd.max_rank == 200
    assert qd.min_remaining_days == 31 and qd.prob_max == 0.15
    assert qd.prob_side == "minority" and qd.require_consecutive is False
    assert qd.min_reward_daily_rate == 0.0
    assert cfg.reward_min_daily_rate == 30.0
    assert (
        cfg.reward_tier_low_min, cfg.reward_tier_mid_min, cfg.reward_tier_high_min
    ) == (30.0, 100.0, 300.0)


def test_fatal_error_disables_with_errors():
    raw = {"horizon_prob": {"preset": "nope"}, "volume": {"top_n": 0}}
    cfg = _resolve(raw)
    assert cfg.enabled is False
    assert cfg.errors
    assert cfg.main_preset == "balanced"
    assert cfg.top_n == 200


def test_invalid_schedule_falls_back():
    cfg = _resolve({"schedule": "99:99"})
    assert cfg.enabled is False
    assert (cfg.schedule_hour, cfg.schedule_minute) == (8, 0)
    assert any("schedule" in e for e in cfg.errors)


def test_invalid_anchors_disable_task():
    raw = {
        "horizon_prob": {
            "anchors": [
                {"days": 60, "minority_max": 0.02},
                {"days": 30, "minority_max": 0.01},  # 非升序
            ]
        }
    }
    cfg = _resolve(raw)
    assert cfg.custom_anchors is None
    assert cfg.errors
    assert cfg.enabled is False


def test_valid_custom_anchors_kept():
    raw = {
        "horizon_prob": {
            "anchors": [
                {"days": 30, "minority_max": 0.01},
                {"days": 60, "minority_max": 0.02},
                {"days": 365, "minority_max": 0.10},
            ]
        }
    }
    cfg = _resolve(raw)
    assert cfg.custom_anchors is not None
    assert cfg.custom_anchors[0]["minority_max"] == 0.01


def test_rule_switch_off_keeps_task_enabled():
    raw = {"horizon_prob": {"enabled": False}}
    cfg = _resolve(raw)
    assert cfg.enabled is True  # 任务仍注册，规则判定关闭由用例读取 horizon_enabled
    assert cfg.horizon_enabled is False


def test_prob_max_out_of_range_warns_and_falls_back():
    cfg = _resolve({"query_defaults": {"prob_max": 0.8}})
    assert cfg.query_defaults.prob_max == 0.15
    assert cfg.warnings


def test_prob_side_invalid_falls_back():
    cfg = _resolve({"query_defaults": {"prob_side": "big"}})
    assert cfg.query_defaults.prob_side == "minority"
    assert cfg.warnings


def test_reward_negative_min_falls_back():
    cfg = _resolve({"reward": {"min_daily_rate": -5}})
    assert cfg.reward_min_daily_rate == 30.0
    assert any("min_daily_rate" in w for w in cfg.warnings)


def test_reward_tiers_non_monotonic_fallback():
    cfg = _resolve(
        {"reward": {"tiers": {"low_min": 100, "mid_min": 30, "high_min": 300}}}
    )
    assert (
        cfg.reward_tier_low_min, cfg.reward_tier_mid_min, cfg.reward_tier_high_min
    ) == (30.0, 100.0, 300.0)
    assert any("tiers" in w for w in cfg.warnings)


def test_query_defaults_min_reward_invalid_falls_back():
    cfg = _resolve({"query_defaults": {"min_reward_daily_rate": "abc"}})
    assert cfg.query_defaults.min_reward_daily_rate == 0.0
    assert any("min_reward_daily_rate" in w for w in cfg.warnings)
