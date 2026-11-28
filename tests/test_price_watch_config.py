"""price_watch 配置解析测试（fail-soft / 越界回退 / 非法条目跳过）。"""

from __future__ import annotations

from typing import Any

from src.settings import resolve_price_watch_config


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
    cfg = resolve_price_watch_config(FakeSettings({}))
    assert cfg.enabled is True
    assert cfg.interval_seconds == 60
    assert cfg.cooldown_seconds == 3600
    assert cfg.symbols == []
    assert cfg.warnings == []


def test_enabled_false_propagates() -> None:
    raw = {"price_watch": {"enabled": False}}
    cfg = resolve_price_watch_config(FakeSettings(raw))
    assert cfg.enabled is False
    assert cfg.symbols == []


def test_out_of_range_falls_back_to_default() -> None:
    raw = {
        "price_watch": {
            "watch_interval_seconds": 9999,
            "cooldown_seconds": -5,
        }
    }
    cfg = resolve_price_watch_config(FakeSettings(raw))
    assert cfg.interval_seconds == 60
    assert cfg.cooldown_seconds == 3600
    assert cfg.warnings


def test_symbols_not_list_falls_back_to_empty() -> None:
    raw = {"price_watch": {"symbols": "BTCUSDT"}}
    cfg = resolve_price_watch_config(FakeSettings(raw))
    assert cfg.symbols == []
    assert cfg.warnings


def test_valid_symbol_parses_with_defaults() -> None:
    raw = {
        "price_watch": {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "category": "crypto",
                    "price_above": 120000,
                    "price_below": 95000,
                }
            ]
        }
    }
    cfg = resolve_price_watch_config(FakeSettings(raw))
    assert len(cfg.symbols) == 1
    s = cfg.symbols[0]
    assert s.label == "BTCUSDT"
    assert s.enabled is True
    assert s.interval_seconds is None
    assert s.price_above == 120000
    assert s.price_below == 95000
    assert s.issuer == "coinbase"
    assert s.chain == "base"
    assert s.price_kind == "dex"


def test_disabled_symbol_is_skipped() -> None:
    raw = {
        "price_watch": {
            "symbols": [
                {"symbol": "OFF", "category": "crypto", "enabled": False},
                {"symbol": "ON", "category": "crypto"},
            ]
        }
    }
    cfg = resolve_price_watch_config(FakeSettings(raw))
    assert [s.symbol for s in cfg.symbols] == ["ON"]
    assert any("已禁用" in w for w in cfg.warnings)


def test_missing_symbol_and_invalid_category_are_skipped() -> None:
    raw = {
        "price_watch": {
            "symbols": [
                {"category": "crypto"},  # 缺 symbol
                {"symbol": "XXX", "category": "forex"},  # 非法 category
                {"symbol": "NVDA", "category": "stock"},
            ]
        }
    }
    cfg = resolve_price_watch_config(FakeSettings(raw))
    assert len(cfg.symbols) == 1
    assert cfg.symbols[0].symbol == "NVDA"
    assert len(cfg.warnings) == 2


def test_invalid_thresholds_become_none_with_warning() -> None:
    raw = {
        "price_watch": {
            "symbols": [
                {
                    "symbol": "BAD",
                    "category": "crypto",
                    "price_above": "abc",
                    "price_below": float("-inf"),
                },
                {
                    "symbol": "NAN",
                    "category": "crypto",
                    "price_above": float("nan"),
                },
            ]
        }
    }
    cfg = resolve_price_watch_config(FakeSettings(raw))
    assert len(cfg.symbols) == 2
    assert cfg.symbols[0].price_above is None
    assert cfg.symbols[0].price_below is None
    assert cfg.symbols[1].price_above is None
    assert len(cfg.warnings) >= 3


def test_above_not_greater_than_below_is_skipped() -> None:
    raw = {
        "price_watch": {
            "symbols": [
                {"symbol": "INV", "category": "crypto",
                 "price_above": 100, "price_below": 100},
                {"symbol": "INV2", "category": "crypto",
                 "price_above": 90, "price_below": 100},
            ]
        }
    }
    cfg = resolve_price_watch_config(FakeSettings(raw))
    assert cfg.symbols == []
    assert len(cfg.warnings) == 2


def test_symbol_interval_out_of_range_falls_back_to_none() -> None:
    raw = {
        "price_watch": {
            "symbols": [
                {"symbol": "BTCUSDT", "category": "crypto", "interval_seconds": 2},
            ]
        }
    }
    cfg = resolve_price_watch_config(FakeSettings(raw))
    assert cfg.symbols[0].interval_seconds is None
    assert cfg.warnings


def test_tokenized_fields_override() -> None:
    raw = {
        "price_watch": {
            "symbols": [
                {
                    "symbol": "NVDAc",
                    "label": "英伟达(链上代币)",
                    "category": "tokenized_stock",
                    "issuer": "coinbase",
                    "chain": "base",
                    "contract": "0xb20000000000000000000078ee7ce2fe4908108c",
                    "price_kind": "dex",
                }
            ]
        }
    }
    cfg = resolve_price_watch_config(FakeSettings(raw))
    s = cfg.symbols[0]
    assert s.label == "英伟达(链上代币)"
    assert s.category == "tokenized_stock"
    assert s.contract == "0xb20000000000000000000078ee7ce2fe4908108c"
    assert s.pool_address == ""
