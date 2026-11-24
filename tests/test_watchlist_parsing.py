"""watchlist 奖励解析与两轮 diff 纯函数测试。"""

from __future__ import annotations

from framework.parsing.watchlist_parsing import (
    ParsedReward,
    diff_rewards,
    parse_clob_reward,
)

USDC = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

THRESH = dict(low_min=30.0, mid_min=100.0, high_min=300.0)


def _raw(rate: str | None, address: str = USDC) -> dict[str, object]:
    return {
        "rewards_config": [
            {
                "rate_per_day": rate,
                "asset_address": address,
                "start_date": "2026-11-01",
                "end_date": "2026-12-01",
            }
        ]
    }


def _reward(rate: float | None, tier: str | None = None) -> ParsedReward:
    return ParsedReward(
        rate=rate, asset="USDC.e", start_date="", end_date="2026-12-01", tier=tier
    )


# ── parse_clob_reward ────────────────────────────────────────────────────


def test_parse_normal_reward_maps_asset_and_tier() -> None:
    parsed = parse_clob_reward(_raw("120"), **THRESH)
    assert parsed is not None
    assert parsed.rate == 120.0
    assert parsed.asset == "USDC.e"
    assert parsed.tier == "mid"


def test_parse_empty_and_dirty_returns_none() -> None:
    assert parse_clob_reward({}, **THRESH) is None
    assert parse_clob_reward({"rewards_config": []}, **THRESH) is None
    assert parse_clob_reward({"rewards_config": "not-a-list"}, **THRESH) is None
    assert parse_clob_reward({"rewards_config": [123]}, **THRESH) is None
    assert parse_clob_reward({"rewards_config": [{"rate_per_day": "abc"}]}, **THRESH) is None


def test_parse_unknown_address_keeps_lowercase_address() -> None:
    parsed = parse_clob_reward(_raw("50", "0xdeadbeef"), **THRESH)
    assert parsed is not None
    assert parsed.asset == "0xdeadbeef"


# ── diff_rewards ────────────────────────────────────────────────────────


def test_diff_exit_when_prev_online_cur_gone() -> None:
    events = diff_rewards(
        _reward(120.0, "mid"), None, cid="c1",
        min_daily_rate=30.0, decay_pct_threshold=0.30,
    )
    codes = [e.event_code for e in events]
    assert codes == ["exit"]


def test_diff_floor_break_when_cur_below_floor() -> None:
    events = diff_rewards(
        _reward(120.0, "mid"), _reward(10.0), cid="c1",
        min_daily_rate=30.0, decay_pct_threshold=0.30,
    )
    assert [e.event_code for e in events] == ["floor_break"]


def test_diff_decay_pct_when_drop_above_threshold() -> None:
    events = diff_rewards(
        _reward(120.0, "mid"), _reward(60.0, "low"), cid="c1",
        min_daily_rate=30.0, decay_pct_threshold=0.30,
    )
    assert "decay_pct" in [e.event_code for e in events]


def test_diff_no_event_when_drop_below_threshold() -> None:
    events = diff_rewards(
        _reward(120.0, "mid"), _reward(100.0, "mid"), cid="c1",
        min_daily_rate=30.0, decay_pct_threshold=0.30,
    )
    assert events == []


def test_diff_tier_drop_high_to_mid() -> None:
    events = diff_rewards(
        _reward(300.0, "high"), _reward(120.0, "mid"), cid="c1",
        min_daily_rate=30.0, decay_pct_threshold=0.30,
    )
    assert "tier_drop" in [e.event_code for e in events]


def test_diff_rebound_when_prev_offline_cur_online() -> None:
    events = diff_rewards(
        None, _reward(120.0, "mid"), cid="c1",
        min_daily_rate=30.0, decay_pct_threshold=0.30,
    )
    assert [e.event_code for e in events] == ["rebound"]


def test_diff_no_event_when_unchanged() -> None:
    events = diff_rewards(
        _reward(120.0, "mid"), _reward(120.0, "mid"), cid="c1",
        min_daily_rate=30.0, decay_pct_threshold=0.30,
    )
    assert events == []
