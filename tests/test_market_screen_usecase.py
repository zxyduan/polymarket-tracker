"""MarketScreenUsecase 单测（设计 §10.3）。"""

from __future__ import annotations

import types

import pytest

from features.market_screen.usecase import MarketScreenUsecase


def _cfg(**overrides) -> types.SimpleNamespace:
    base = types.SimpleNamespace(
        enabled=True, horizon_enabled=True, main_preset="balanced",
        record_all_presets=True,
        min_value=50000.0, top_n=200, page_limit=100, max_pages=4,
        timezone_offset_hours=8, clamp_min=0.002, clamp_max=0.20,
        persist_non_passed=True, custom_anchors=None,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


_USDC_E = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"


def _raw(
    cid: str,
    vol: float,
    prob_yes: str,
    end: str = "2027-12-31",
    reward_rate: float = 100.0,
) -> dict:
    raw: dict = {
        "conditionId": cid, "slug": f"slug-{cid}", "question": f"Q {cid}",
        "outcomes": '["Yes","No"]',
        "outcomePrices": f'["{prob_yes}", "{1 - float(prob_yes):.4f}"]',
        "endDateIso": end, "volume24hr": str(vol),
    }
    if reward_rate is not None:
        raw["clobRewards"] = [
            {"assetAddress": _USDC_E, "rewardsDailyRate": reward_rate, "rewardsAmount": 0}
        ]
    return raw


class FakeLister:
    def __init__(self, raws: list[dict] | Exception) -> None:
        self._raws = raws
        self.closed = False

    async def list_active_markets(self, *, page_limit: int, max_pages: int) -> list[dict]:
        if isinstance(self._raws, Exception):
            raise self._raws
        return list(self._raws)

    async def aclose(self) -> None:
        self.closed = True


class FakeStore:
    def __init__(self) -> None:
        self.rows: list = []
        self.metas: list = []

    async def replace_screen_day(self, run_date: str, rows: list, meta: object) -> None:
        self.rows = list(rows)
        self.metas.append(meta)


def _usecase(raws: list[dict] | Exception, **cfg_overrides) -> tuple[MarketScreenUsecase, FakeStore]:
    store = FakeStore()
    usecase = MarketScreenUsecase(FakeLister(raws), store, config=_cfg(**cfg_overrides))
    return usecase, store


async def test_done_persists_ranked_rows_and_counts():
    raws = [
        _raw("0xa", 60000.0, "0.02"),   # 低概率通过
        _raw("0xb", 70000.0, "0.30"),   # 高概率不通过
        _raw("0xc", 30000.0, "0.01"),    # 量低于阈值剔除
    ]
    usecase, store = _usecase(raws)
    result = await usecase.run()

    assert result == "done"
    assert len(store.rows) == 2
    # 按成交量降序：0xb(70000) rank1 不通过，0xa(60000) rank2 通过
    by_cid = {r.condition_id: r for r in store.rows}
    assert by_cid["0xb"].rank_no == 1 and by_cid["0xb"].screen_passed == 0
    assert by_cid["0xa"].rank_no == 2 and by_cid["0xa"].screen_passed == 1
    assert by_cid["0xa"].screen_reason == "pass"
    assert by_cid["0xb"].screen_reason == "neutral_band"

    meta = store.metas[-1]
    assert meta.status == "done"
    assert meta.candidate_count == 2
    assert meta.passed_count == 1
    assert meta.pass_balanced_count == 1


async def test_empty_when_no_candidate():
    raws = [_raw("0xc", 30000.0, "0.01"), {"conditionId": None}]
    usecase, store = _usecase(raws)
    result = await usecase.run()

    assert result == "empty"
    assert store.rows == []
    meta = store.metas[-1]
    assert meta.status == "done"
    assert meta.candidate_count == 0
    assert meta.passed_count == 0


async def test_failed_on_lister_error_records_failed_meta():
    usecase, store = _usecase(RuntimeError("boom"))
    result = await usecase.run()

    assert result == "failed"
    meta = store.metas[-1]
    assert meta.status == "failed"
    assert meta.candidate_count == 0
    assert "boom" in meta.error


async def test_disabled_rule_marks_topn_only_null_presets():
    raws = [_raw("0xa", 60000.0, "0.30")]  # 高概率；规则关闭仍通过
    usecase, store = _usecase(raws, horizon_enabled=False)
    result = await usecase.run()

    assert result == "done"
    row = store.rows[0]
    assert row.screen_passed == 1
    assert row.screen_reason == "volume_topn_only"
    assert row.pass_strict is None and row.pass_balanced is None and row.pass_loose is None
    assert row.threshold_lo is None
    meta = store.metas[-1]
    assert meta.pass_balanced_count == 0 and meta.passed_count == 1


async def test_non_passed_dropped_when_persist_disabled():
    raws = [
        _raw("0xa", 60000.0, "0.02"),   # 低概率通过
        _raw("0xb", 70000.0, "0.30"),   # 高概率不通过
    ]
    usecase, store = _usecase(raws, persist_non_passed=False)
    result = await usecase.run()

    assert result == "done"
    assert [r.condition_id for r in store.rows] == ["0xa"]
    meta = store.metas[-1]
    assert meta.candidate_count == 1 and meta.passed_count == 1


async def test_topn_truncation():
    raws = [_raw(f"0x{i}", 50000.0 + i * 1000, "0.02") for i in range(5)]
    usecase, store = _usecase(raws, top_n=2)
    result = await usecase.run()

    assert result == "done"
    assert len(store.rows) == 2
    assert [r.rank_no for r in store.rows] == [1, 2]


# ── 奖励准入与复合排序 ──────────────────────────────────────────────────────


async def test_reward_below_gate_not_persisted():
    raws = [
        _raw("0xa", 60000.0, "0.02", reward_rate=None),   # 无 clobRewards
        _raw("0xb", 60000.0, "0.02", reward_rate=0.0),
        _raw("0xc", 60000.0, "0.02", reward_rate=-1.0),
        _raw("0xd", 60000.0, "0.02", reward_rate=29.9),
    ]
    usecase, store = _usecase(raws)
    assert await usecase.run() == "empty"
    assert store.rows == []


async def test_composite_sort_reward_then_volume():
    raws = [
        _raw("0xa", 60000.0, "0.02", reward_rate=50.0),
        _raw("0xb", 90000.0, "0.02", reward_rate=100.0),
        _raw("0xc", 80000.0, "0.02", reward_rate=50.0),
    ]
    usecase, store = _usecase(raws)
    await usecase.run()
    # 奖励 100 在前；同 50 按成交额 80k 在前、60k 在后。
    assert [r.condition_id for r in store.rows] == ["0xb", "0xc", "0xa"]


async def test_topn_after_composite_sort():
    raws = [
        _raw("0xa", 60000.0, "0.02", reward_rate=40.0),
        _raw("0xb", 70000.0, "0.02", reward_rate=200.0),
        _raw("0xc", 80000.0, "0.02", reward_rate=120.0),
    ]
    usecase, store = _usecase(raws, top_n=2)
    await usecase.run()
    assert [r.condition_id for r in store.rows] == ["0xb", "0xc"]


async def test_reward_fields_persisted():
    raws = [_raw("0xa", 60000.0, "0.02", reward_rate=100.0)]
    usecase, store = _usecase(raws)
    await usecase.run()
    row = store.rows[0]
    assert row.rewards_daily_rate == pytest.approx(100.0)
    assert row.rewards_asset == "USDC.e"


async def test_reward_gate_follows_config():
    raws = [_raw("0xa", 60000.0, "0.02", reward_rate=40.0)]
    usecase, store = _usecase(raws, reward_min_daily_rate=50.0)
    assert await usecase.run() == "empty"
    assert store.rows == []
