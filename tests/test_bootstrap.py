"""bootstrap 装配测试。"""

import asyncio

from framework.bootstrap import build_runtime


class _Prov:
    def targets(self): return []


def test_build_runtime_switch_off():
    rt = build_runtime(storage=None, channels=[], provider=_Prov(), health={})
    assert all(not m.enabled for m in rt.modules)
    asyncio.run(rt.run_enabled())


def test_build_runtime_switch_on_needs_deps():
    # switch=True 但没传 collector/renderer → 仍降级 noop，不崩
    rt = build_runtime(storage=None, channels=[], provider=_Prov(),
                       health={}, switch=True)
    assert all(m.enabled for m in rt.modules)
    asyncio.run(rt.run_enabled())
