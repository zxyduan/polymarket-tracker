"""bootstrap 装配测试。"""

import asyncio

from framework.bootstrap import build_runtime


class _Prov:
    def targets(self): return []


def test_build_runtime_registers_disabled_features():
    rt = build_runtime(storage=None, cfg=None, channels=[],
                       provider=_Prov(), health={})
    names = [m.name for m in rt.modules]
    assert names == ["market_snapshot", "daily_report"]
    assert all(not m.enabled for m in rt.modules)
    asyncio.run(rt.run_enabled())  # disabled → 不跑、不报错
