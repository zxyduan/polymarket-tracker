"""FeatureRuntime 测试。"""

import asyncio

from framework.runtime.module import FeatureModule, FeatureRuntime


async def ok(): ok.called += 1
ok.called = 0


async def boom(): raise RuntimeError("x")


def test_runtime_runs_enabled_and_isolates_failure():
    rt = FeatureRuntime()
    rt.register(FeatureModule(name="a", enabled=True, run=ok))
    rt.register(FeatureModule(name="b", enabled=False, run=ok))
    rt.register(FeatureModule(name="c", enabled=True, run=boom))
    asyncio.run(rt.run_enabled())
    assert ok.called == 1  # b 跳过，c 异常不拖 a
