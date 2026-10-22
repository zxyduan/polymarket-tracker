"""快照停更现场取证脚本（容器内 / 宿主机均可运行）。

容器内：docker exec -i polymarket-tracker python - < snap_diagnose.py
宿主机：python3 snap_diagnose.py
        （在 docker-compose.yml 所在目录，读 ./data/monitor.db 与 ./config/markets.toml）

只读：先把 DB 复制到临时文件再查询（避免 -wal 权限问题，不碰原库）；
对 Gamma / CLOB 各发一次只读请求。输出四段：
A 各市场最新桶落后情况 / B 最新桶明细 / C 基线覆盖 / D 市场实时状态。
"""

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.request
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))


def bj(ts: float, fmt: str = "%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, BJ).strftime(fmt)


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "snap-diagnose/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def pick_path(env: str, container: str, host: str) -> str:
    if os.environ.get(env):
        return os.environ[env]
    return container if os.path.exists(container) else host


def load_markets(path: str) -> list[dict]:
    """优先 tomllib；解释器较旧时退化为最小 [[markets]] 解析（够用且只读）。"""
    try:
        import tomllib

        with open(path, "rb") as fh:
            return tomllib.load(fh).get("markets", [])
    except ModuleNotFoundError:
        markets: list[dict] = []
        cur: dict | None = None
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("[[markets]]"):
                cur = {}
                markets.append(cur)
                continue
            m = re.match(r'^([a-z_]+)\s*=\s*(.+?)\s*$', line)
            if cur is not None and m:
                key, raw = m.group(1), m.group(2)
                if raw in ("true", "false"):
                    cur[key] = raw == "true"
                elif raw.startswith('"'):
                    cur[key] = raw.strip('"')
                else:
                    cur[key] = raw
        return markets


db_path = pick_path("SNAP_DB", "/app/data/monitor.db", "data/monitor.db")
cfg_path = pick_path("SNAP_MARKETS", "/app/config/markets.toml", "config/markets.toml")
print(f"DB: {db_path}")
print(f"配置: {cfg_path}")

# 复制到临时文件再打开：WAL 模式下只读直连可能因 -wal 权限报错，副本可正常回放
tmpdir = tempfile.mkdtemp(prefix="snapdiag_")
tmp_db = os.path.join(tmpdir, "monitor.db")
shutil.copy2(db_path, tmp_db)
for suffix in ("-wal", "-shm"):
    src = db_path + suffix
    if os.path.exists(src):
        shutil.copy2(src, tmp_db + suffix)

now = int(time.time())
cur = (now // 7200) * 7200
print("现在(BJ):", datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"), "| 当前桶起:", bj(cur))

con = sqlite3.connect(tmp_db)
con.row_factory = sqlite3.Row

print("\n[A] 每个市场最新桶落后当前桶多少（>0 即停更）")
for r in con.execute(
    """SELECT slug_id, condition_id, MAX(bucket_ts) AS mb,
              SUM(CASE WHEN volume_shares < 0 THEN 1 ELSE 0 END) AS unknown_cnt,
              COUNT(*) AS n
       FROM market_snapshots GROUP BY condition_id ORDER BY mb"""
):
    lag = (cur - r["mb"]) // 7200
    print(
        f"  {r['slug_id']:<12} 最新桶 {bj(r['mb'])}  落后 {lag} 个桶  "
        f"未知量行数 {r['unknown_cnt']}/{r['n']}"
    )

print("\n[B] 各市场最新桶明细（量=-1 表示未知；落库时间应在桶起点附近）")
for r in con.execute(
    """SELECT s.slug_id, s.bucket_ts, s.volume_shares, s.price_close, s.collected_ts
       FROM market_snapshots s
       JOIN (SELECT condition_id, MAX(bucket_ts) AS m
             FROM market_snapshots GROUP BY condition_id) x
       ON s.condition_id = x.condition_id AND s.bucket_ts = x.m
       ORDER BY s.bucket_ts"""
):
    print(
        f"  {r['slug_id']:<12} 桶 {bj(r['bucket_ts'], '%m-%d %H:%M')}  "
        f"量={r['volume_shares']:.0f} 收盘={r['price_close']:.4f}  "
        f"落库={bj(r['collected_ts'], '%m-%d %H:%M:%S')}"
    )

print("\n[C] 基线覆盖（最近 8 个桶，每桶几个市场记录了起点基线）")
for r in con.execute(
    """SELECT bucket_ts, COUNT(*) AS c FROM snap_volume_baseline
       GROUP BY bucket_ts ORDER BY bucket_ts DESC LIMIT 8"""
):
    print(f"  {bj(r['bucket_ts'], '%m-%d %H:%M')}  基线市场数={r['c']}")

print("\n[D] 配置市场实时探测（closed/accepting=是否还在交易；最后价点=价格流是否已停）")
try:
    markets = load_markets(cfg_path)
except FileNotFoundError:
    print(f"  找不到 markets.toml: {cfg_path}")
    markets = []
for m in markets:
    if m.get("enabled") is False:
        continue
    slug = str(m.get("slug", ""))
    sid = m.get("slug_id") or slug
    if not slug:
        continue
    try:
        g = http_json(f"https://gamma-api.polymarket.com/markets/slug/{slug}")
        tokens = json.loads(g.get("clobTokenIds") or "[]")
        hist = []
        if tokens:
            url = (
                f"https://clob.polymarket.com/prices-history?market={tokens[0]}"
                f"&startTs={now - 3 * 86400}&fidelity=60"
            )
            hist = http_json(url).get("history", [])
        last = hist[-1] if hist else None
        last_s = bj(last["t"], "%m-%d %H:%M") if last else "无价格点"
        in_cur = sum(1 for p in hist if p["t"] >= cur)
        print(
            f"  {sid:<12} closed={g.get('closed')} accepting={g.get('acceptingOrders')} "
            f"price={g.get('price')} 最后价点={last_s} 当前桶点数={in_cur}"
        )
    except Exception as exc:  # noqa: BLE001 - 诊断脚本需打印全部失败
        print(f"  {sid:<12} 请求失败: {type(exc).__name__}: {exc}")

con.close()
shutil.rmtree(tmpdir, ignore_errors=True)
