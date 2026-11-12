"""SQLite 存储层（异步包装，WAL 模式）。"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from framework.models.market_screen import CandidateMarket, ScreenRunMeta
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

CREATE TABLE IF NOT EXISTS market_screen_daily (
    run_date        TEXT    NOT NULL,
    condition_id    TEXT    NOT NULL,
    rank_no         INTEGER NOT NULL DEFAULT 0,
    slug            TEXT    NOT NULL DEFAULT '',
    question        TEXT    NOT NULL DEFAULT '',
    outcomes        TEXT    NOT NULL DEFAULT '',
    end_date_iso    TEXT,
    end_date_raw    TEXT    NOT NULL DEFAULT '',
    remaining_days  INTEGER,
    prob_yes        REAL,
    prob_no         REAL,
    minority_prob   REAL,
    volume_24h      REAL    NOT NULL DEFAULT 0.0,
    volume_metric   TEXT    NOT NULL DEFAULT 'volume24hr',
    horizon_preset  TEXT    NOT NULL DEFAULT 'balanced',
    threshold_lo    REAL,
    horizon_pass    INTEGER NOT NULL DEFAULT 0,
    pass_strict     INTEGER,
    pass_balanced   INTEGER,
    pass_loose      INTEGER,
    screen_passed   INTEGER NOT NULL DEFAULT 0,
    screen_reason   TEXT    NOT NULL DEFAULT '',
    collected_ts    REAL    NOT NULL,
    rewards_daily_rate REAL,
    rewards_asset      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (run_date, condition_id)
);
CREATE INDEX IF NOT EXISTS idx_mscreen_date_pass
    ON market_screen_daily(run_date, screen_passed, volume_24h DESC);
CREATE INDEX IF NOT EXISTS idx_mscreen_preset ON market_screen_daily(run_date, horizon_preset);
CREATE INDEX IF NOT EXISTS idx_mscreen_date_time_prob
    ON market_screen_daily(run_date, remaining_days, minority_prob);
CREATE INDEX IF NOT EXISTS idx_mscreen_date_minor_vol
    ON market_screen_daily(run_date, minority_prob, volume_24h DESC);
CREATE INDEX IF NOT EXISTS idx_mscreen_cid_date
    ON market_screen_daily(condition_id, run_date);
CREATE INDEX IF NOT EXISTS idx_mscreen_cid_date_vol
    ON market_screen_daily(condition_id, run_date, volume_24h, rank_no);

CREATE TABLE IF NOT EXISTS market_screen_run (
    run_date            TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    elapsed_ms          INTEGER NOT NULL DEFAULT 0,
    pages_fetched       INTEGER NOT NULL DEFAULT 0,
    candidate_count     INTEGER NOT NULL DEFAULT 0,
    horizon_preset      TEXT    NOT NULL DEFAULT 'balanced',
    pass_strict_count   INTEGER NOT NULL DEFAULT 0,
    pass_balanced_count INTEGER NOT NULL DEFAULT 0,
    pass_loose_count    INTEGER NOT NULL DEFAULT 0,
    passed_count        INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'running',
    error               TEXT    NOT NULL DEFAULT ''
);
"""

# 只读视图 DDL（设计 §13.3）；独立保存便于旧库迁移时核对定义漂移并重建。
SCREEN_VIEW_SQL: dict[str, str] = {
    "v_screen_latest_done": """
CREATE VIEW IF NOT EXISTS v_screen_latest_done AS
SELECT d.*
FROM market_screen_daily AS d
WHERE d.run_date = (
    SELECT MAX(run_date) FROM market_screen_run WHERE status = 'done'
);
""",
    "v_screen_candidate_daily": """
CREATE VIEW IF NOT EXISTS v_screen_candidate_daily AS
SELECT
    d.*,
    CASE WHEN d.end_date_iso IS NOT NULL THEN
        CAST(
            julianday(d.end_date_iso)
            - julianday(date('now', '+8 hours'))
            + 0.999999
        AS INTEGER)
    END AS days_left_today,
    CASE
        WHEN d.end_date_iso IS NULL THEN NULL
        WHEN CAST(julianday(d.end_date_iso)
                  - julianday(date('now', '+8 hours')) + 0.999999 AS INTEGER) <= 30
            THEN 'near_30d'
        WHEN CAST(julianday(d.end_date_iso)
                  - julianday(date('now', '+8 hours')) + 0.999999 AS INTEGER) <= 90
            THEN 'mid_90d'
        ELSE 'far_90d_plus'
    END AS time_band,
    CASE
        WHEN d.prob_yes IS NULL THEN NULL
        WHEN d.prob_yes <= 0.5 THEN 'yes_underdog'
        ELSE 'yes_favorite'
    END AS side_label,
    CASE
        WHEN d.minority_prob IS NULL THEN NULL
        WHEN d.minority_prob <= 0.02 THEN 'extreme'
        WHEN d.minority_prob <= 0.10 THEN 'moderate'
        ELSE 'central'
    END AS prob_band
FROM market_screen_daily AS d;
""",
    "v_screen_persistent_10d": """
CREATE VIEW IF NOT EXISTS v_screen_persistent_10d AS
WITH base AS (
    SELECT d.*
    FROM market_screen_daily AS d
    WHERE d.run_date >= date('now', '+8 hours', '-9 days')
      AND d.run_date <= date('now', '+8 hours')
),
agg AS (
    SELECT
        condition_id,
        COUNT(DISTINCT run_date)                AS hit_days,
        COUNT(*)                                AS row_count,
        MIN(volume_24h)                         AS min_volume_24h,
        AVG(volume_24h)                         AS avg_volume_24h,
        MAX(volume_24h)                         AS max_volume_24h,
        MIN(rank_no)                            AS best_rank,
        MAX(rank_no)                            AS worst_rank,
        MIN(run_date)                           AS first_run_date,
        MAX(run_date)                           AS last_run_date
    FROM base
    GROUP BY condition_id
),
latest AS (
    SELECT b.*
    FROM base AS b
    JOIN (
        SELECT condition_id, MAX(run_date) AS max_date
        FROM base GROUP BY condition_id
    ) t ON t.condition_id = b.condition_id AND t.max_date = b.run_date
)
SELECT
    a.condition_id,
    a.hit_days, a.row_count,
    a.min_volume_24h, a.avg_volume_24h, a.max_volume_24h,
    a.best_rank, a.worst_rank, a.first_run_date, a.last_run_date,
    l.slug, l.question, l.end_date_iso,
    l.remaining_days,
    l.prob_yes, l.prob_no, l.minority_prob,
    l.volume_24h  AS latest_volume_24h,
    l.rank_no     AS latest_rank_no,
    l.pass_strict, l.pass_balanced, l.pass_loose, l.screen_passed,
    l.rewards_daily_rate AS latest_rewards_daily_rate,
    l.rewards_asset      AS latest_rewards_asset
FROM agg AS a
JOIN latest AS l ON l.condition_id = a.condition_id;
""",
}

# FR9 只读查询允许访问的白名单视图/表（其余名称拒绝，禁止任意 SQL 拼接）。
_SCREEN_READABLE = frozenset(
    {
        "v_screen_latest_done",
        "v_screen_candidate_daily",
        "v_screen_persistent_10d",
        "market_screen_daily",
        "market_screen_run",
    }
)

_SCREEN_PRESET_COLUMNS = {
    "strict": "pass_strict",
    "balanced": "pass_balanced",
    "loose": "pass_loose",
}

_SCREEN_DAILY_COLUMNS = (
    "run_date, condition_id, rank_no, slug, question, outcomes, "
    "end_date_iso, end_date_raw, remaining_days, prob_yes, prob_no, minority_prob, "
    "volume_24h, volume_metric, horizon_preset, threshold_lo, horizon_pass, "
    "pass_strict, pass_balanced, pass_loose, screen_passed, screen_reason, collected_ts, "
    "rewards_daily_rate, rewards_asset"
)

_RUN_UPSERT_SQL = """
INSERT INTO market_screen_run (
    run_date, started_at, finished_at, elapsed_ms, pages_fetched, candidate_count,
    horizon_preset, pass_strict_count, pass_balanced_count, pass_loose_count,
    passed_count, status, error
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(run_date) DO UPDATE SET
    started_at=excluded.started_at, finished_at=excluded.finished_at,
    elapsed_ms=excluded.elapsed_ms, pages_fetched=excluded.pages_fetched,
    candidate_count=excluded.candidate_count, horizon_preset=excluded.horizon_preset,
    pass_strict_count=excluded.pass_strict_count,
    pass_balanced_count=excluded.pass_balanced_count,
    pass_loose_count=excluded.pass_loose_count,
    passed_count=excluded.passed_count, status=excluded.status, error=excluded.error
"""

# 模板⑦-b：hit_days 聚合（允许偶缺一天）。占位顺序：
# start_date, end_date, max_rank, min_hit_days, min_volume_24h,
# min_remaining_days, prob_max。
_SQL_PERSISTENT_HIT = """
WITH base AS (
    SELECT * FROM market_screen_daily
    WHERE run_date BETWEEN ? AND ?
      AND rank_no <= ?
),
agg AS (
    SELECT condition_id,
        COUNT(DISTINCT run_date) AS hit_days,
        COUNT(*) AS row_count,
        MIN(volume_24h) AS min_volume_24h,
        AVG(volume_24h) AS avg_volume_24h,
        MAX(volume_24h) AS max_volume_24h,
        MIN(rank_no) AS best_rank,
        MAX(rank_no) AS worst_rank,
        MIN(run_date) AS first_run_date,
        MAX(run_date) AS last_run_date
    FROM base GROUP BY condition_id
    HAVING COUNT(DISTINCT run_date) >= ?
       AND MIN(volume_24h) >= ?
)
SELECT a.condition_id, a.hit_days, a.row_count,
    a.min_volume_24h, a.avg_volume_24h, a.max_volume_24h,
    a.best_rank, a.worst_rank, a.first_run_date, a.last_run_date,
    l.slug, l.question, l.end_date_iso, l.remaining_days,
    l.prob_yes, l.prob_no, l.minority_prob,
    l.volume_24h AS latest_volume_24h, l.rank_no AS latest_rank_no,
    l.pass_strict, l.pass_balanced, l.pass_loose, l.screen_passed,
    l.rewards_daily_rate AS latest_rewards_daily_rate,
    l.rewards_asset      AS latest_rewards_asset
FROM agg AS a
JOIN market_screen_daily AS l
  ON l.condition_id = a.condition_id AND l.run_date = a.last_run_date
WHERE l.remaining_days >= ?
  AND l.{prob_col} <= ?
{reward_clause}
{order_by}
"""

# 排序模式白名单（注入 ORDER BY，不拼接自由文本）。{prob_col} 由查询方法填充。
_PERSISTENT_ORDER: dict[str, dict[str, str]] = {
    "persistence": {
        "hit": "ORDER BY a.hit_days DESC, a.min_volume_24h DESC, l.minority_prob ASC",
        "streak": "ORDER BY s.max_streak DESC, l.volume_24h DESC",
    },
    "reward": {
        "hit": "ORDER BY l.rewards_daily_rate DESC, l.remaining_days DESC, "
               "l.{prob_col} ASC, a.hit_days DESC",
        "streak": "ORDER BY l.rewards_daily_rate DESC, l.remaining_days DESC, "
                  "l.{prob_col} ASC, s.max_streak DESC",
    },
}

# 模板⑧：严格连续 max_streak。占位顺序：
# start_date, end_date, min_volume_24h, max_rank, min_streak,
# min_remaining_days, prob_max。
_SQL_PERSISTENT_STREAK = """
WITH eligible AS (
    SELECT condition_id, run_date, question, end_date_iso,
           remaining_days, prob_yes, minority_prob, volume_24h, rank_no,
           rewards_daily_rate, rewards_asset
    FROM market_screen_daily
    WHERE run_date BETWEEN ? AND ?
      AND volume_24h >= ?
      AND rank_no <= ?
),
numbered AS (
    SELECT condition_id, run_date,
        ROW_NUMBER() OVER (
            PARTITION BY condition_id ORDER BY run_date
        ) AS rn
    FROM eligible
),
islands AS (
    SELECT condition_id,
           CAST(julianday(run_date) - rn AS INTEGER) AS grp,
           COUNT(*) AS streak
    FROM numbered GROUP BY condition_id, grp
),
streak AS (
    SELECT condition_id, MAX(streak) AS max_streak
    FROM islands GROUP BY condition_id
),
latest_in_window AS (
    SELECT e.* FROM eligible e
    JOIN (
        SELECT condition_id, MAX(run_date) AS last_date FROM eligible
        GROUP BY condition_id
    ) t ON t.condition_id = e.condition_id AND t.last_date = e.run_date
)
SELECT s.condition_id, s.max_streak,
    l.question, l.end_date_iso, l.remaining_days,
    l.prob_yes, l.minority_prob,
    l.volume_24h AS latest_volume_24h, l.rank_no AS latest_rank_no,
    l.rewards_daily_rate AS latest_rewards_daily_rate,
    l.rewards_asset      AS latest_rewards_asset
FROM streak AS s
JOIN latest_in_window AS l ON l.condition_id = s.condition_id
WHERE s.max_streak >= ?
  AND l.remaining_days >= ?
  AND l.{prob_col} <= ?
{reward_clause}
{order_by}
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
        # 旧库迁移：market_screen_daily 补奖励列（新库 SCHEMA 已含），历史行不回填。
        for _screen_alter in (
            "ALTER TABLE market_screen_daily ADD COLUMN rewards_daily_rate REAL",
            "ALTER TABLE market_screen_daily ADD COLUMN rewards_asset TEXT NOT NULL DEFAULT ''",
        ):
            try:
                await self._execute(_screen_alter)
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

        # 只读视图：新库幂等创建；旧库定义漂移则 DROP 后重建（G7）。
        for view_name, view_sql in SCREEN_VIEW_SQL.items():
            await self._ensure_view(view_name, view_sql)

    async def _ensure_view(self, name: str, sql: str) -> None:
        """核对视图定义；不存在则建，定义漂移则重建。"""
        row = await self._fetchone(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name=?", (name,)
        )
        if row is None:
            await self._execute_script(sql)
            return
        existing = (row["sql"] or "").strip()
        if existing != sql.strip():
            await self._execute(f"DROP VIEW IF EXISTS {name}")
            await self._execute_script(sql)

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

    async def _transaction(self, work: Callable[[], None]) -> None:
        """锁内单事务：work() 内多条写语句累积提交；异常回滚后重抛。

        连接未显式设置 isolation_level（sqlite3 默认 deferred 隐式事务），
        这里不先 execute("BEGIN")，避免与隐式事务冲突；事务内不跨 await 做 IO。
        """
        async with self._lock:

            def _inner() -> None:
                try:
                    work()
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise

            await asyncio.to_thread(_inner)

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

    # ---- 每日成交份额汇总（日报专用只读查询）----

    async def volume_by_window(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        """窗口 [start_ts, end_ts) 内各市场成交份额聚合（仅份额口径）。

        返回窗口内至少有一行的市场：
          condition_id, slug, slug_id,
          shares  = SUM(volume_shares >= 0 的桶)  # -1 未知桶与缺失行贡献 0
          buckets = 命中行数（仅供日志诊断，不进正文）

        时间过滤与聚合全部在 SQL 内完成（复用 idx_snap_time）；
        报表显示名以调用方 markets.toml 为准，这里的历史 slug 仅诊断用。
        """
        rows = await self._fetchall(
            """
            SELECT condition_id,
                   MAX(slug)    AS slug,
                   MAX(slug_id) AS slug_id,
                   SUM(CASE WHEN volume_shares >= 0 THEN volume_shares
                            ELSE 0 END) AS shares,
                   COUNT(*) AS buckets
            FROM market_snapshots
            WHERE bucket_ts >= ? AND bucket_ts < ?
            GROUP BY condition_id
            """,
            (start_ts, end_ts),
        )
        return [dict(row) for row in rows]

    async def latest_notification_id(self) -> int:
        """当前 notifications 表最大自增 id（日报发送前的水位起点）。"""
        row = await self._fetchone("SELECT MAX(id) AS max_id FROM notifications")
        if row is None or row["max_id"] is None:
            return 0
        return int(row["max_id"])

    async def count_notifications_since(
        self, after_id: int, event_key: str
    ) -> dict[str, int]:
        """统计 id > after_id 且归属 event_key 的 success / failed 条数。

        notifications 表为全任务共用，event_keys 为逗号拼接字符串，
        用 LIKE 子串匹配做归属隔离：同时刻交易员摘要、快照推送的落痕
        因 key 不同被排除，故日报成败判定不受其他任务影响。
        """
        like = f"%{event_key}%"
        row = await self._fetchone(
            """
            SELECT
              SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
              SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed
            FROM notifications
            WHERE id > ? AND event_keys LIKE ?
            """,
            (after_id, like),
        )
        if row is None:
            return {"success": 0, "failed": 0}
        return {
            "success": int(row["success"] or 0),
            "failed": int(row["failed"] or 0),
        }

    # ---- 每日市场筛选（Market Screen）----

    @staticmethod
    def _daily_params(row: CandidateMarket) -> tuple[Any, ...]:
        return (
            row.run_date, row.condition_id, row.rank_no, row.slug, row.question,
            row.outcomes_json, row.end_date_iso, row.end_date_raw, row.remaining_days,
            row.prob_yes, row.prob_no, row.minority_prob, row.volume_24h,
            row.volume_metric, row.horizon_preset, row.threshold_lo, row.horizon_pass,
            row.pass_strict, row.pass_balanced, row.pass_loose, row.screen_passed,
            row.screen_reason, row.collected_ts,
            row.rewards_daily_rate, row.rewards_asset,
        )

    @staticmethod
    def _run_params(meta: ScreenRunMeta) -> tuple[Any, ...]:
        return (
            meta.run_date, meta.started_at, meta.finished_at, meta.elapsed_ms,
            meta.pages_fetched, meta.candidate_count, meta.horizon_preset,
            meta.pass_strict_count, meta.pass_balanced_count, meta.pass_loose_count,
            meta.passed_count, meta.status, meta.error,
        )

    async def replace_screen_day(
        self,
        run_date: str,
        rows: list[CandidateMarket],
        meta: ScreenRunMeta,
    ) -> None:
        """单事务替换当日分区：删旧明细 → 批量插本轮候选 → UPSERT run 元信息。

        任一阶段失败回滚，不留半写入明细；事务内不跨 await 做 IO。
        """

        def _work() -> None:
            conn = self.conn
            conn.execute(
                "DELETE FROM market_screen_daily WHERE run_date=?", (run_date,)
            )
            if rows:
                placeholder_count = len(_SCREEN_DAILY_COLUMNS.split(","))
                placeholders = ",".join("?" for _ in range(placeholder_count))
                conn.executemany(
                    f"INSERT INTO market_screen_daily ({_SCREEN_DAILY_COLUMNS}) "
                    f"VALUES ({placeholders})",
                    [self._daily_params(row) for row in rows],
                )
            conn.execute(_RUN_UPSERT_SQL, self._run_params(meta))

        await self._transaction(_work)

    async def list_screen_daily(
        self,
        run_date: str,
        *,
        passed_only: bool = False,
        preset: str | None = None,
    ) -> list[dict[str, object]]:
        sql = "SELECT * FROM market_screen_daily WHERE run_date=?"
        params: list[Any] = [run_date]
        if passed_only:
            sql += " AND screen_passed=1"
        if preset is not None:
            column = _SCREEN_PRESET_COLUMNS.get(preset)
            if column is not None:
                sql += f" AND {column}=1"
        rows = await self._fetchall(sql, tuple(params))
        return [dict(row) for row in rows]

    async def query_screen_rows(
        self, view_name: str, params: dict[str, object]
    ) -> list[dict[str, object]]:
        """FR9 通用只读查询封装；视图/表白名单，值一律 ? 绑定。"""
        if view_name not in _SCREEN_READABLE:
            raise ValueError(f"非法只读视图/表: {view_name!r}")
        rows = await self._fetchall(
            f"SELECT * FROM {view_name}", tuple(params.values())
        )
        return [dict(row) for row in rows]

    async def query_persistent_rows(
        self,
        *,
        start_date: str,
        end_date: str,
        max_rank: int,
        min_hit_days: int,
        min_volume_24h: float,
        min_remaining_days: int,
        prob_max: float,
        prob_side: str = "minority",
        require_consecutive: bool = False,
        min_reward_daily_rate: float = 0.0,
        sort_mode: str = "persistence",
    ) -> list[dict[str, object]]:
        """持续性主场景只读聚合；⑦-b hit_days 或 ⑧ max_streak，参数全部绑定。"""
        prob_col = "prob_yes" if prob_side == "yes" else "minority_prob"
        kind = "streak" if require_consecutive else "hit"

        if require_consecutive:
            base_params: list[Any] = [
                start_date, end_date, min_volume_24h, max_rank, min_hit_days,
                min_remaining_days, prob_max,
            ]
            template = _SQL_PERSISTENT_STREAK
        else:
            base_params = [
                start_date, end_date, max_rank, min_hit_days, min_volume_24h,
                min_remaining_days, prob_max,
            ]
            template = _SQL_PERSISTENT_HIT

        # 0 = 不额外限奖励：跳过奖励 WHERE，避免 NULL 历史行被 `>= 0` 误剔。
        reward_clause = ""
        params: list[Any] = list(base_params)
        if min_reward_daily_rate > 0:
            reward_clause = "AND l.rewards_daily_rate >= ?"
            params.append(min_reward_daily_rate)

        mode = sort_mode if sort_mode in ("persistence", "reward") else "persistence"
        order_by = _PERSISTENT_ORDER[mode][kind].format(prob_col=prob_col)
        sql = template.format(
            prob_col=prob_col,
            reward_clause=reward_clause,
            order_by=order_by,
        )
        rows = await self._fetchall(sql, tuple(params))
        return [dict(row) for row in rows]

    async def prune_screen(self, retention_days: int) -> None:
        """删除早于「今天 − retention_days」的明细行与对应 run 行；临界当日保留。"""
        days = retention_days if retention_days > 0 else 30
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
        await self._execute(
            "DELETE FROM market_screen_daily WHERE run_date < ?", (cutoff,)
        )
        await self._execute(
            "DELETE FROM market_screen_run WHERE run_date < ?", (cutoff,)
        )
