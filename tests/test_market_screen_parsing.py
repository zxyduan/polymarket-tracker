"""Market Screen 纯函数单测（设计 §10.1 #1–5、#15）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from framework.parsing.market_screen_parsing import (
    PRESET_ANCHORS,
    PRESETS,
    evaluate_presets,
    interpolate_minority_max,
    parse_market,
    resolve_anchors,
)

NOW = datetime(2026, 11, 11, 8, 0, 0)


def _good_raw(**overrides):
    raw = {
        "conditionId": "0xABC123",
        "slug": "will-x",
        "question": "Will X happen?",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.0175","0.9825"]',
        "endDateIso": "2028-11-07",
        "volume24hr": "60000.0",
    }
    raw.update(overrides)
    return raw


# ── 三档模板锚点 ──────────────────────────────────────────────────────────


def test_preset_anchor_values_and_monotonicity():
    for days, expected in ((30, None), (60, None), (365, None)):
        vals = {
            preset: interpolate_minority_max(days, PRESET_ANCHORS[preset])
            for preset in PRESETS
        }
        assert vals["strict"] <= vals["balanced"] <= vals["loose"]
    # 逐档逐点锚点值
    assert interpolate_minority_max(30, PRESET_ANCHORS["strict"]) == pytest.approx(0.005)
    assert interpolate_minority_max(60, PRESET_ANCHORS["strict"]) == pytest.approx(0.01)
    assert interpolate_minority_max(365, PRESET_ANCHORS["strict"]) == pytest.approx(0.05)
    assert interpolate_minority_max(30, PRESET_ANCHORS["balanced"]) == pytest.approx(0.01)
    assert interpolate_minority_max(60, PRESET_ANCHORS["balanced"]) == pytest.approx(0.02)
    assert interpolate_minority_max(365, PRESET_ANCHORS["balanced"]) == pytest.approx(0.10)
    assert interpolate_minority_max(30, PRESET_ANCHORS["loose"]) == pytest.approx(0.02)
    assert interpolate_minority_max(60, PRESET_ANCHORS["loose"]) == pytest.approx(0.035)
    assert interpolate_minority_max(365, PRESET_ANCHORS["loose"]) == pytest.approx(0.15)


def test_interpolation_segment_and_boundaries():
    anchors = PRESET_ANCHORS["balanced"]
    # 段内线性插值
    assert interpolate_minority_max(45, anchors) == pytest.approx(0.015, abs=1e-9)
    assert interpolate_minority_max(90, anchors) == pytest.approx(0.027869, abs=1e-3)
    assert interpolate_minority_max(180, anchors) == pytest.approx(0.051475, abs=1e-3)
    # 首锚前 nearest、末锚后 clamp
    assert interpolate_minority_max(10, anchors) == pytest.approx(0.01)
    assert interpolate_minority_max(400, anchors) == pytest.approx(0.10)
    # clamp 裁剪生效
    clamped = interpolate_minority_max(
        400, anchors, clamp_min=0.02, clamp_max=0.08
    )
    assert clamped == pytest.approx(0.08)


def test_evaluate_presets_pass_fail_and_record_all():
    anchors_by = {p: resolve_anchors(p, None) for p in PRESETS}
    # d=30、minority=1.5%：strict/balanced 否，loose 是
    ev = evaluate_presets(
        30, 0.015, anchors_by, record_all=True,
        main_preset="balanced", clamp_min=0.002, clamp_max=0.20,
    )
    assert ev.pass_strict == 0
    assert ev.pass_balanced == 0
    assert ev.pass_loose == 1
    assert ev.horizon_pass == 0
    assert ev.threshold_lo == pytest.approx(0.01)

    # record_all=False：仅主档计算，其余档标志 NULL
    ev_partial = evaluate_presets(
        30, 0.015, anchors_by, record_all=False,
        main_preset="balanced", clamp_min=0.002, clamp_max=0.20,
    )
    assert ev_partial.pass_strict is None
    assert ev_partial.pass_balanced is None
    assert ev_partial.pass_loose is None
    assert ev_partial.horizon_pass == 0


def test_custom_anchors_override_preset():
    custom = [
        {"days": 30, "minority_max": 0.02},
        {"days": 60, "minority_max": 0.035},
        {"days": 365, "minority_max": 0.15},
    ]
    anchors = resolve_anchors("strict", custom)
    assert anchors[0] == (30, 0.02)
    # 非法覆盖（非升序）防御性回退模板
    bad = [{"days": 60, "minority_max": 0.02}, {"days": 30, "minority_max": 0.01}]
    fallback = resolve_anchors("strict", bad)
    assert fallback == PRESET_ANCHORS["strict"]


# ── 报文解析 ──────────────────────────────────────────────────────────────


def test_parse_market_basic():
    row = parse_market(_good_raw(), now=NOW, tz_offset_hours=8)
    assert row is not None
    assert row.condition_id == "0xabc123"
    assert row.prob_yes == pytest.approx(0.0175)
    assert row.prob_no == pytest.approx(0.9825)
    assert row.minority_prob == pytest.approx(0.0175)
    assert row.volume_24h == pytest.approx(60000.0)
    assert row.volume_metric == "volume24hr"
    assert row.remaining_days is not None and row.remaining_days > 30


def test_parse_market_yes_index_case_insensitive_and_list():
    raw = _good_raw(
        outcomes=["yes", "no"],
        outcomePrices=["0.98", "0.02"],
    )
    row = parse_market(raw, now=NOW, tz_offset_hours=8)
    assert row is not None
    assert row.prob_yes == pytest.approx(0.98)
    assert row.prob_no == pytest.approx(0.02)
    assert row.minority_prob == pytest.approx(0.02)


def test_parse_market_end_date_with_time_and_messy_end_date():
    raw = _good_raw(endDateIso="2028-11-07T12:00:00")
    row = parse_market(raw, now=NOW, tz_offset_hours=8)
    assert row is not None and row.end_date_iso == "2028-11-07"

    raw2 = _good_raw(endDateIso=None, endDate="2027/1/1 4:59:00")
    row2 = parse_market(raw2, now=NOW, tz_offset_hours=8)
    assert row2 is not None and row2.end_date_iso == "2027-01-01"


@pytest.mark.parametrize(
    "overrides",
    [
        {"conditionId": None},
        {"outcomes": '["Yes","Yes"]'},
        {"outcomePrices": '["0.5"]'},
        {"outcomePrices": '["abc","0.5"]'},
        {"volume24hr": "abc"},
        {"volume24hr": None},
        {"endDateIso": "2020-01-01"},
        {"endDateIso": None, "endDate": None},
        {"outcomePrices": '["0.4","0.4"]'},  # 价格和偏离 1
    ],
)
def test_parse_market_dirty_rows_return_none(overrides):
    row = parse_market(_good_raw(**overrides), now=NOW, tz_offset_hours=8)
    assert row is None
