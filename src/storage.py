"""SQLite 存储层（异步包装，WAL 模式）。"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.models import StandardEvent
from src.settings import WatchlistSeed

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    address       TEXT NOT NULL UNIQUE,
    proxy_address TEXT,
    label         TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key    TEXT NOT NULL UNIQUE,
    source        TEXT NOT NULL,
    wallet        TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    tx_hash       TEXT,
    log_index     INTEGER,
    event_time    TEXT,
    payload_json  TEXT NOT NULL,
    notified      INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_wallet_time ON events(wallet, event_time);

CREATE TABLE IF NOT EXISTS market_cache (
    condition_id  TEXT PRIMARY KEY,
    question      TEXT,
    slug          TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      TEXT NOT NULL,
    message_text TEXT NOT NULL,
    event_keys   TEXT,
    status       TEXT NOT NULL,
    sent_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    condition_id      TEXT NOT NULL,
    slug              TEXT NOT NULL DEFAULT '',
    slug_id           TEXT NOT NULL DEFAULT '',
    bucket_ts         INTEGER NOT NULL,
    price_open        REAL NOT NULL,
    price_close       REAL NOT NULL,
    price_high        REAL NOT NULL,
    price_low         REAL NOT NULL,
    volume_delta      REAL NOT NULL DEFAULT 0.0,
    volume_shares     REAL NOT NULL DEFAULT -1.0,
    trend             TEXT NOT NULL DEFAULT '',
    trend_pct         REAL NOT NULL DEFAULT 0.0,
    trend_streak      INTEGER NOT NULL DEFAULT 0,
    trend_direction   TEXT NOT NULL DEFAULT '',
    drawdown_from_high REAL NOT NULL DEFAULT 0.0,
    price_percentile  REAL NOT NULL DEFAULT 0.5,
    bid_depth_top5    REAL NOT NULL DEFAULT 0.0,
    ask_depth_top5    REAL NOT NULL DEFAULT 0.0,
    bid_wall_count    INTEGER NOT NULL DEFAULT 0,
    bid_depth_change  REAL NOT NULL DEFAULT 0.0,
    vol_short         REAL NOT NULL DEFAULT 0.0,
    vol_long          REAL NOT NULL DEFAULT 0.0,
    vol_regime        TEXT NOT NULL DEFAULT '',
    our_fv            REAL NOT NULL DEFAULT 0.0,
    market_fv         REAL NOT NULL DEFAULT 0.0,
    collected_ts      REAL NOT NULL,
    PRIMARY KEY (condition_id, bucket_ts)
);
CREATE INDEX IF NOT EXISTS idx_snap_time ON market_snapshots(bucket_ts);

CREATE TABLE IF NOT EXISTS snap_volume_baseline (
    condition_id TEXT NOT NULL,
    bucket_ts    INTEGER NOT NULL,
    value        REAL NOT NULL,
    updated_ts   REAL NOT NULL,
    PRIMARY KEY (condition_id, bucket_ts)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    """SQLite 异步访问层。"""

    def __init__(self, path: str = "data/monitor.db") -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        # 所有操作串行化：sqlite3 连接非线程安全，多任务并发会触发事务竞态
        # （如 "cannot commit - no transaction is active"）
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        """初始化目录、连接与表结构。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._connect)
        await self._execute_script(SCHEMA)
        # 旧库迁移：market_snapshots 补 volume_shares 列（新库 SCHEMA 已含）
        try:
            await self._execute(
                "ALTER TABLE market_snapshots ADD COLUMN volume_shares REAL NOT NULL DEFAULT -1.0"
            )
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 语义迁移（v11）：volume_shares 默认 -1 = 未结算/未知；历史 0.0
        # 全部是"未补算"的假 0（v10b 只写 delta>0 的补算），统一改 -1 显示"-"，
        # 不再把"未知"误导为"真零成交"。
        await self._execute(
            "UPDATE market_snapshots SET volume_shares=-1.0 WHERE volume_shares=0.0"
        )
        # 旧库迁移（v10）：snap_volume_baseline 改为按桶存储（重启不丢基线），
        # 旧表无 bucket_ts 列则重建（基线是临时采样值，可重建，无历史价值）。
        try:
            cols = await self._fetchall("PRAGMA table_info(snap_volume_baseline)")
        except sqlite3.OperationalError:
            cols = []
        if cols and not any(r["name"] == "bucket_ts" for r in cols):
            await self._execute("DROP TABLE snap_volume_baseline")
            await self._execute_script(SCHEMA)
            logger.info("snap_volume_baseline 已迁移为按桶结构（v10）")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Storage 尚未初始化")
        return self._conn

    async def _execute_script(self, script: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self.conn.executescript, script)

    async def _execute(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> sqlite3.Cursor:
        async def _run() -> sqlite3.Cursor:
            async with self._lock:
                def _inner() -> sqlite3.Cursor:
                    cur = self.conn.execute(sql, params)
                    self.conn.commit()
                    return cur

                return await asyncio.to_thread(_inner)

        return await _run()

    async def _fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: self.conn.execute(sql, params).fetchall(),
            )

    async def _fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> sqlite3.Row | None:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: self.conn.execute(sql, params).fetchone(),
            )

    async def close(self) -> None:
        if self._conn is not None:
            async with self._lock:
                await asyncio.to_thread(self._conn.close)
            self._conn = None

    # ---- 监控名单 ----

    async def seed_wallets(self, seeds: list[WatchlistSeed]) -> int:
        """把配置文件中的名单种子写入（已存在不重复、不覆盖标签）。返回新增数。"""
        added = 0
        for seed in seeds:
            address = seed.address.lower()
            existing = await self._fetchone(
                "SELECT id FROM wallets WHERE address=?", (address,)
            )
            if existing is None:
                await self._execute(
                    "INSERT INTO wallets(address, proxy_address, label, active, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (address, None, seed.label, 1, _now()),
                )
                added += 1
        return added

    async def add_wallet(self, address: str, label: str = "") -> tuple[bool, str]:
        """新增监控地址，返回(是否新增, 结果说明)。"""
        address = address.lower()
        existing = await self._fetchone(
            "SELECT id FROM wallets WHERE address=?", (address,)
        )
        if existing is not None:
            return False, "该地址已在监控名单中"
        await self._execute(
            "INSERT INTO wallets(address, proxy_address, label, active, created_at) "
            "VALUES(?,?,?,?,?)",
            (address, None, label, 1, _now()),
        )
        return True, "已加入监控"

    async def remove_wallet(self, address: str) -> tuple[bool, str]:
        """软删除（停用）监控地址。"""
        address = address.lower()
        cur = await self._execute(
            "UPDATE wallets SET active=0 WHERE address=? AND active=1",
            (address,),
        )
        if cur.rowcount == 0:
            return False, "名单中未找到该地址（或已移除）"
        return True, "已移出监控"

    async def list_wallets(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM wallets"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY id"
        rows = await self._fetchall(sql)
        return [dict(row) for row in rows]

    async def active_addresses(self) -> set[str]:
        """当前生效地址集合（小写）。"""
        rows = await self._fetchall(
            "SELECT address, proxy_address FROM wallets WHERE active=1"
        )
        result: set[str] = set()
        for row in rows:
            result.add(row["address"])
            if row["proxy_address"]:
                result.add(row["proxy_address"].lower())
        return result

    async def update_proxy(self, address: str, proxy_address: str) -> None:
        await self._execute(
            "UPDATE wallets SET proxy_address=? WHERE address=?",
            (proxy_address.lower(), address.lower()),
        )

    # ---- 事件 ----

    async def insert_event(self, event: StandardEvent) -> bool:
        """落库新事件；唯一键冲突返回 False。"""
        try:
            await self._execute(
                "INSERT INTO events(dedupe_key, source, wallet, event_type, tx_hash, "
                "log_index, event_time, payload_json, notified, first_seen) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event.dedupe_key,
                    event.source,
                    event.wallet,
                    event.event_type,
                    event.tx_hash,
                    event.log_index,
                    event.event_time.isoformat() if event.event_time else None,
                    event.to_payload(),
                    0,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    async def mark_notified(self, dedupe_keys: list[str]) -> None:
        if not dedupe_keys:
            return
        placeholders = ",".join("?" for _ in dedupe_keys)
        await self._execute(
            f"UPDATE events SET notified=1 WHERE dedupe_key IN ({placeholders})",
            tuple(dedupe_keys),
        )

    # ---- 市场缓存 ----

    async def get_market_cache(self, condition_id: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT * FROM market_cache WHERE condition_id=?", (condition_id,)
        )
        return dict(row) if row else None

    async def set_market_cache(
        self, condition_id: str, question: str | None, slug: str | None
    ) -> None:
        await self._execute(
            "INSERT INTO market_cache(condition_id, question, slug, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(condition_id) DO UPDATE SET "
            "question=excluded.question, slug=excluded.slug, updated_at=excluded.updated_at",
            (condition_id, question, slug, _now()),
        )

    # ---- 通用状态 ----

    async def get_state(self, key: str) -> str | None:
        row = await self._fetchone(
            "SELECT value FROM sync_state WHERE key=?", (key,)
        )
        return row["value"] if row else None

    async def set_state(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO sync_state(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now()),
        )

    # ---- 推送记录 ----

    async def record_notification(
        self,
        chat_id: str,
        message_text: str,
        event_keys: str,
        status: str,
    ) -> None:
        await self._execute(
            "INSERT INTO notifications(chat_id, message_text, event_keys, status, sent_at) "
            "VALUES(?,?,?,?,?)",
            (chat_id, message_text, event_keys, status, _now()),
        )

    # ---- 统计与维护 ----

    async def notified_count_today(self) -> int:
        start = datetime.now(timezone.utc).date().isoformat()
        row = await self._fetchone(
            "SELECT COUNT(*) AS c FROM notifications WHERE status='success' AND sent_at LIKE ?",
            (f"{start}%",),
        )
        return int(row["c"]) if row else 0

    async def latest_event_time(self) -> str | None:
        row = await self._fetchone(
            "SELECT MAX(event_time) AS t FROM events"
        )
        return row["t"] if row else None

    async def prune_events(self, retention_days: int) -> None:
        cutoff = (
            datetime.now(timezone.utc).timestamp() - retention_days * 86400
        )
        await self._execute(
            "DELETE FROM events WHERE event_time IS NULL OR "
            "strftime('%s', event_time) < ?",
            (str(cutoff),),
        )

    async def prune_notifications(self, retention_days: int) -> None:
        """清理过期推送记录，防止 notifications 表无限增长。"""
        cutoff = (
            datetime.now(timezone.utc).timestamp() - retention_days * 86400
        )
        await self._execute(
            "DELETE FROM notifications WHERE "
            "strftime('%s', sent_at) < ?",
            (str(cutoff),),
        )

    async def vacuum(self) -> None:
        """回收 SQLite 文件空间（删除后文件本身不缩小，需 VACUUM）。"""
        async with self._lock:
            await asyncio.to_thread(self.conn.execute, "VACUUM")

    # ---- 市场快照（2h 桶）----

    async def upsert_snapshot(
        self,
        *,
        condition_id: str,
        slug: str,
        slug_id: str,
        bucket_ts: int,
        price_open: float,
        price_close: float,
        price_high: float,
        price_low: float,
        volume_delta: float,
        volume_shares: float = -1.0,
        trend: str,
        trend_pct: float,
        trend_streak: int,
        trend_direction: str,
        drawdown_from_high: float,
        price_percentile: float,
        bid_depth_top5: float,
        ask_depth_top5: float,
        bid_wall_count: int,
        bid_depth_change: float,
        vol_short: float,
        vol_long: float,
        vol_regime: str,
        our_fv: float = 0.0,
        market_fv: float = 0.0,
    ) -> None:
        """UPSERT 写入一个 2h 桶快照；同桶重跑覆盖，不产生重复行。"""
        await self._execute(
            """INSERT INTO market_snapshots (
                condition_id, slug, slug_id, bucket_ts,
                price_open, price_close, price_high, price_low,
                volume_delta, volume_shares, trend, trend_pct, trend_streak, trend_direction,
                drawdown_from_high, price_percentile,
                bid_depth_top5, ask_depth_top5, bid_wall_count, bid_depth_change,
                vol_short, vol_long, vol_regime,
                our_fv, market_fv, collected_ts
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(condition_id, bucket_ts) DO UPDATE SET
                slug=excluded.slug, slug_id=excluded.slug_id,
                price_open=excluded.price_open, price_close=excluded.price_close,
                price_high=excluded.price_high, price_low=excluded.price_low,
                volume_delta=excluded.volume_delta,
                volume_shares=excluded.volume_shares,
                trend=excluded.trend, trend_pct=excluded.trend_pct,
                trend_streak=excluded.trend_streak, trend_direction=excluded.trend_direction,
                drawdown_from_high=excluded.drawdown_from_high,
                price_percentile=excluded.price_percentile,
                bid_depth_top5=excluded.bid_depth_top5,
                ask_depth_top5=excluded.ask_depth_top5,
                bid_wall_count=excluded.bid_wall_count,
                bid_depth_change=excluded.bid_depth_change,
                vol_short=excluded.vol_short, vol_long=excluded.vol_long,
                vol_regime=excluded.vol_regime,
                our_fv=excluded.our_fv, market_fv=excluded.market_fv,
                collected_ts=excluded.collected_ts""",
            (
                condition_id.lower(), slug, slug_id, bucket_ts,
                price_open, price_close, price_high, price_low,
                volume_delta, volume_shares, trend, trend_pct, trend_streak, trend_direction,
                drawdown_from_high, price_percentile,
                bid_depth_top5, ask_depth_top5, bid_wall_count, bid_depth_change,
                vol_short, vol_long, vol_regime,
                our_fv, market_fv,
                time.time(),
            ),
        )

    async def last_snapshot(self, condition_id: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT * FROM market_snapshots WHERE condition_id=? "
            "ORDER BY bucket_ts DESC LIMIT 1",
            (condition_id.lower(),),
        )
        return dict(row) if row else None

    async def update_snapshot_volume(
        self, condition_id: str, bucket_ts: int, volume_delta: float,
        volume_shares: float = -1.0,
    ) -> None:
        """更新指定（已闭合）桶的成交量增量（USDC + 份额）；桶不存在则忽略（幂等）。

        桶闭合时其 volume_shares 尚为 -1（未知），由边界轮/推送前用
        累计量-基线补算后写回；-1 保持"未知"显示"-"，不误导为真零。
        """
        await self._execute(
            "UPDATE market_snapshots SET volume_delta=?, volume_shares=?, collected_ts=? "
            "WHERE condition_id=? AND bucket_ts=?",
            (volume_delta, volume_shares, time.time(), condition_id.lower(), bucket_ts),
        )

    async def update_snapshot_prices(
        self,
        condition_id: str,
        bucket_ts: int,
        price_open: float,
        price_close: float,
        price_high: float,
        price_low: float,
    ) -> None:
        """只更新某桶 OHLC（闭合桶收尾价点补齐用），不碰成交量等其他字段。

        与 upsert_snapshot 的区别：upsert 冲突时会把 volume_shares 重置为 -1，
        而补算已结算的量必须保留；桶不存在时静默忽略（幂等）。
        """
        await self._execute(
            "UPDATE market_snapshots SET price_open=?, price_close=?, "
            "price_high=?, price_low=?, collected_ts=? "
            "WHERE condition_id=? AND bucket_ts=?",
            (
                price_open,
                price_close,
                price_high,
                price_low,
                time.time(),
                condition_id.lower(),
                bucket_ts,
            ),
        )

    async def recent_snapshots(
        self, condition_id: str, buckets: int = 30
    ) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT * FROM market_snapshots WHERE condition_id=? "
            "ORDER BY bucket_ts DESC LIMIT ?",
            (condition_id.lower(), buckets),
        )
        return [dict(row) for row in rows]

    async def distinct_snapshot_cids(self) -> list[str]:
        """取快照表出现过的全部 condition_id（RTDS 记账白名单恢复用）。"""
        rows = await self._fetchall(
            "SELECT DISTINCT condition_id FROM market_snapshots WHERE condition_id != ''"
        )
        return [str(row["condition_id"]) for row in rows]

    async def latest_complete_buckets(
        self, now_bucket: int
    ) -> list[dict[str, Any]]:
        """取每个市场最近一个**已闭合**桶（bucket_ts < now_bucket）。

        返回按 bucket_ts 升序（旧→新）。
        """
        rows = await self._fetchall(
            "SELECT s.* FROM market_snapshots s "
            "JOIN (SELECT condition_id, MAX(bucket_ts) AS max_ts "
            "      FROM market_snapshots WHERE bucket_ts < ? "
            "      GROUP BY condition_id) m "
            "ON s.condition_id = m.condition_id AND s.bucket_ts = m.max_ts "
            "ORDER BY s.bucket_ts ASC",
            (now_bucket,),
        )
        return [dict(row) for row in rows]

    async def get_snap_baseline(self, condition_id: str, bucket_ts: int) -> float | None:
        """取某市场指定桶**起点时刻**的 Gamma 累计量（桶内成交的差值基线）。"""
        row = await self._fetchone(
            "SELECT value FROM snap_volume_baseline WHERE condition_id=? AND bucket_ts=?",
            (condition_id.lower(), bucket_ts),
        )
        return float(row["value"]) if row else None

    async def set_snap_baseline(self, condition_id: str, bucket_ts: int, volume: float) -> None:
        """记录某市场指定桶起点时刻的累计量；同桶覆盖（幂等）。"""
        await self._execute(
            "INSERT INTO snap_volume_baseline(condition_id, bucket_ts, value, updated_ts) "
            "VALUES(?,?,?,?) ON CONFLICT(condition_id, bucket_ts) DO UPDATE SET "
            "value=excluded.value, updated_ts=excluded.updated_ts",
            (condition_id.lower(), bucket_ts, volume, time.time()),
        )

    async def prune_snapshots(self, retention_days: int) -> None:
        cutoff = time.time() - retention_days * 86400
        await self._execute(
            "DELETE FROM market_snapshots WHERE bucket_ts < ?", (cutoff,)
        )
