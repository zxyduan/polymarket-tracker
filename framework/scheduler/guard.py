"""调度守卫（设计 v3.5 §8，关闭 F08）。

- FireOnceGuard：纯配置 frozen 数据类，只描述节奏；已删 body_hash 字段。
  取值只在 09 §3.4.1 定义，本文件不复制常量表。
- BodyHashGuard：用例级正文指纹去重，只依赖 CursorStore 协议，
  不 import L3 实现；matches 只读、record 只写、仅发送成功才 record。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from framework.models.ports import CursorStore


class CommitCondition(str, Enum):
    RETURN_OK = "return_ok"
    FINAL_CODE_IN = "final_code_in"


@dataclass(frozen=True)
class FireOnceGuard:
    commit_when: CommitCondition
    commit_state_on: frozenset[str]
    retry_poll_seconds: int      # 命中分钟内未落 state 的轮询
    cooldown_seconds: int        # 已落 state 后的冷却


class BodyHashGuard:
    def __init__(self, states: CursorStore) -> None:
        self._states = states

    @staticmethod
    def _fingerprint(body: str) -> str:
        # 与现状逐字一致：sha1(body.encode())
        return hashlib.sha1(body.encode()).hexdigest()

    async def matches(self, namespace: str, key: str, body: str) -> bool:
        """正文指纹与已存记录一致即命中（重复），命中则跳过发送。只读。"""
        old = await self._states.get(namespace, key)
        return old is not None and old == self._fingerprint(body)

    async def record(self, namespace: str, key: str, body: str) -> None:
        """仅发送成功后调用。"""
        await self._states.set(namespace, key, self._fingerprint(body))
