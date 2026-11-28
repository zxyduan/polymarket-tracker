"""price_watch 用例状态机测试（mock feed / renderer / router / store，不依赖外网）。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from features.price_watch.usecase import PriceWatchUsecase
from framework.delivery.router import DeliveryResult
from framework.models.price_watch import Quote
from framework.scheduler.guard import BodyHashGuard

_STATE_NS = "price_watch"
_GUARD_NS = "price_watch_msg"


class FakeStore:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    async def get(self, namespace: str, key: str) -> str | None:
        return self._d.get(f"{namespace}:{key}")

    async def set(self, namespace: str, key: str, value: str) -> None:
        self._d[f"{namespace}:{key}"] = value


class FakeFeed:
    def __init__(self, quotes: dict[str, Quote | None]) -> None:
        self.quotes = quotes
        self.calls: list[str] = []

    async def get_quote(self, symbol: str, category: str, *, contract: str = "") -> Quote | None:
        self.calls.append(symbol)
        return self.quotes.get(symbol)

    async def aclose(self) -> None:
        return


class FakeRenderer:
    def __init__(self, msg: Any = "ok") -> None:
        self.events: list[Any] = []
        self._msg = msg

    def render(self, event: Any) -> Any:
        self.events.append(event)
        return None if self._msg is None else _msg(self._msg)


class FakeRouter:
    def __init__(self, failures: int = 0) -> None:
        self.msgs: list[Any] = []
        self._failures = failures

    async def dispatch(self, feature: str, msg: Any) -> DeliveryResult:
        self.msgs.append(msg)
        failures = tuple(f"err-{i}" for i in range(self._failures))
        return DeliveryResult(outcomes=(), channel_failures=failures)


def _msg(body: str) -> Any:
    from framework.models.message import RenderedMessage

    return RenderedMessage(body=body, title="")


def _quote(symbol: str, price: float) -> Quote:
    return Quote(
        symbol=symbol,
        label=symbol,
        category="crypto",
        price=price,
        price_kind="crypto",
        source="test",
        fetched_at=datetime.now(timezone.utc),
    )


def _cfg(symbol: str = "BTCUSDT", above: float | None = None,
         below: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol, label=symbol, category="crypto", contract="",
        price_above=above, price_below=below,
    )


def _make_usecase(
    feed: FakeFeed,
    *,
    cooldown: int = 0,
    renderer_msg: Any = "ok",
    router_failures: int = 0,
) -> tuple[PriceWatchUsecase, FakeStore, FakeRouter, FakeRenderer]:
    store = FakeStore()
    renderer = FakeRenderer(renderer_msg)
    router = FakeRouter(router_failures)
    guard = BodyHashGuard(store)
    config = SimpleNamespace(cooldown_seconds=cooldown)
    uc = PriceWatchUsecase(
        feed=feed, store=store, renderer=renderer, router=router,
        guard=guard, config=config, health={},
    )
    return uc, store, router, renderer


async def test_first_round_builds_baseline_without_alert() -> None:
    feed = FakeFeed({"BTCUSDT": _quote("BTCUSDT", 100.0)})
    uc, store, router, _ = _make_usecase(feed)
    await uc.run_round([_cfg(above=120, below=90)])
    assert router.msgs == []
    assert await store.get(_STATE_NS, "last:BTCUSDT") is not None


async def test_break_above_dispatches_once() -> None:
    feed = FakeFeed({"BTCUSDT": _quote("BTCUSDT", 100.0)})
    uc, _store, router, renderer = _make_usecase(feed)
    await uc.run_round([_cfg(above=120, below=90)])
    feed.quotes["BTCUSDT"] = _quote("BTCUSDT", 130.0)
    await uc.run_round([_cfg(above=120, below=90)])
    assert len(router.msgs) == 1
    assert renderer.events[0].event_code == "break_above"
    assert renderer.events[0].threshold == 120


async def test_break_below_dispatches() -> None:
    feed = FakeFeed({"BTCUSDT": _quote("BTCUSDT", 100.0)})
    uc, _store, router, renderer = _make_usecase(feed)
    await uc.run_round([_cfg(above=120, below=90)])
    feed.quotes["BTCUSDT"] = _quote("BTCUSDT", 80.0)
    await uc.run_round([_cfg(above=120, below=90)])
    assert len(router.msgs) == 1
    assert renderer.events[0].event_code == "break_below"


async def test_inside_range_refreshes_baseline_no_alert() -> None:
    feed = FakeFeed({"BTCUSDT": _quote("BTCUSDT", 100.0)})
    uc, _store, router, _ = _make_usecase(feed)
    await uc.run_round([_cfg(above=120, below=90)])
    feed.quotes["BTCUSDT"] = _quote("BTCUSDT", 105.0)
    await uc.run_round([_cfg(above=120, below=90)])
    assert router.msgs == []


async def test_cooldown_blocks_repeat() -> None:
    feed = FakeFeed({"BTCUSDT": _quote("BTCUSDT", 100.0)})
    uc, _store, router, _ = _make_usecase(feed, cooldown=3600)
    await uc.run_round([_cfg(above=120, below=90)])
    feed.quotes["BTCUSDT"] = _quote("BTCUSDT", 130.0)
    await uc.run_round([_cfg(above=120, below=90)])  # 首次推送
    feed.quotes["BTCUSDT"] = _quote("BTCUSDT", 131.0)
    await uc.run_round([_cfg(above=120, below=90)])  # 冷却内
    assert len(router.msgs) == 1


async def test_same_body_dedup_hit() -> None:
    feed = FakeFeed({"BTCUSDT": _quote("BTCUSDT", 100.0)})
    uc, _store, router, _ = _make_usecase(feed, cooldown=0)
    await uc.run_round([_cfg(above=120, below=90)])
    feed.quotes["BTCUSDT"] = _quote("BTCUSDT", 130.0)
    await uc.run_round([_cfg(above=120, below=90)])
    feed.quotes["BTCUSDT"] = _quote("BTCUSDT", 130.0)
    await uc.run_round([_cfg(above=120, below=90)])
    assert len(router.msgs) == 1  # 指纹相同，第二次不重推


async def test_no_threshold_direction_not_judged() -> None:
    feed = FakeFeed({"NVDA": _quote("NVDA", 200.0)})
    uc, _store, router, _ = _make_usecase(feed)
    await uc.run_round([_cfg(symbol="NVDA")])  # above/below 均 None
    assert router.msgs == []


async def test_feed_none_records_failure_no_alert() -> None:
    feed = FakeFeed({"BTCUSDT": None})
    uc, _store, router, _ = _make_usecase(feed)
    await uc.run_round([_cfg(above=120, below=90)])
    assert router.msgs == []
    assert uc.funnel.get("fetch_failed") == 1


async def test_render_none_skips_dispatch() -> None:
    feed = FakeFeed({"BTCUSDT": _quote("BTCUSDT", 100.0)})
    uc, _store, router, _ = _make_usecase(feed, renderer_msg=None)
    await uc.run_round([_cfg(above=120, below=90)])
    feed.quotes["BTCUSDT"] = _quote("BTCUSDT", 130.0)
    await uc.run_round([_cfg(above=120, below=90)])
    assert router.msgs == []
    assert uc.funnel.get("render_none") == 1


async def test_dispatch_failure_does_not_commit_state() -> None:
    feed = FakeFeed({"BTCUSDT": _quote("BTCUSDT", 100.0)})
    uc, store, router, _ = _make_usecase(feed, router_failures=1)
    await uc.run_round([_cfg(above=120, below=90)])
    feed.quotes["BTCUSDT"] = _quote("BTCUSDT", 130.0)
    await uc.run_round([_cfg(above=120, below=90)])
    assert len(router.msgs) == 1
    # 推送失败不写指纹：下一轮相同正文仍会再试
    assert await store.get(_GUARD_NS, "break_above:BTCUSDT") is None


async def test_close_is_noop_without_timers() -> None:
    feed = FakeFeed({})
    uc, _store, _router, _ = _make_usecase(feed)
    await uc.close()  # 不抛、无悬挂任务
