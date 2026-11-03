"""bootstrap 装配测试。"""

from framework.bootstrap import build_runtime
from framework.models.message import RenderedMessage


class _R:
    def render(self, *a, **k): return RenderedMessage(body="x")


def test_build_runtime_constructs_all_three():
    rt, trader_uc = build_runtime(
        storage=None, collector=None, catalog=None,
        snap_notifiers=[], daily_notifiers=[], trader_notifiers=[],
        snap_renderer=_R(), daily_renderer=_R(), trader_renderer=_R(),
        snap_cfg=None, daily_cfg=None,
        snap_health={}, daily_health={},
    )
    names = [m.name for m in rt.modules]
    assert names == ["market_snapshot", "daily_report", "trader"]
    assert all(m.enabled for m in rt.modules)
