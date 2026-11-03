"""StateStore（设计 v3.5 §3.3，T01）。

对旧 sync_state 表的带命名空间门面：新代码一律按 (namespace, key) 读写，
不再拼裸字符串；底层暂仍写旧表（旧键平铺），待 §2.2 迁移脚本把表结构
切到 (namespace, key, value, updated_at) 后仅替换本类的存储后端，
上层调用点不动。namespace 取值：trader / snapshot / daily_report /
data_api / auth；未知键在迁移期隔离进 legacy。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.storage import Storage


class StateStore:
    def __init__(self, storage: "Storage") -> None:
        self._storage = storage

    @staticmethod
    def _compose(namespace: str, key: str) -> str:
        # 现状平铺键：f"{namespace}:{key}"。
        # 与 §2.2 map_state_key 的逆运算在迁移后仍保持一致（如 data_api 游标族）。
        return f"{namespace}:{key}"

    async def get(self, namespace: str, key: str) -> str | None:
        return await self._storage.get_state(self._compose(namespace, key))

    async def set(self, namespace: str, key: str, value: str) -> None:
        await self._storage.set_state(self._compose(namespace, key), value)
