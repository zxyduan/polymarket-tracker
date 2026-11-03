"""P3: T14 F2/F3 guard 节奏测试（39 §5.5）。"""

import pytest

from framework.scheduler.guard import BodyHashGuard, FireOnceGuard, CommitCondition


class _FakeStore:
    def __init__(self): self.d = {}
    async def get(self, ns, k): return self.d.get(f"{ns}:{k}")
    async def set(self, ns, k, v): self.d[f"{ns}:{k}"] = v


@pytest.mark.asyncio
async def test_t14_f2_success_writes_state():
    g = BodyHashGuard(_FakeStore())
    assert await g.matches("ns", "k", "body") is False
    await g.record("ns", "k", "body")
    assert await g.matches("ns", "k", "body") is True


def test_t14_f3_fire_once_config():
    g = FireOnceGuard(
        commit_when=CommitCondition.RETURN_OK,
        commit_state_on=frozenset({"ok"}),
        retry_poll_seconds=5,
        cooldown_seconds=60,
    )
    assert g.cooldown_seconds == 60
