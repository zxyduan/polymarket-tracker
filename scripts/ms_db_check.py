"""market_screen 落库只读体检（不写库）。

EC2 未安装 sqlite3 CLI 时，用 .venv 自带的 sqlite3 标准库只读查看：
- 最近几轮运行元信息（market_screen_run）
- 指定日期明细概况、奖励代币分布、分档分布、过门情况、候选清单 Top N

用法（仓库根目录、.venv 激活）：
    python scripts/ms_db_check.py
    python scripts/ms_db_check.py --date 2026-11-13 --top 30
    python scripts/ms_db_check.py --all
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 直接 `python scripts/ms_db_check.py` 运行时，把仓库根加入模块搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = "data/monitor.db"

# 分档阈值与 config.yaml 中 market_screen.tiers 保持一致（low [30,100) / mid [100,300) / high [300,+∞)）。
TIER_LOW_MIN = 30.0
TIER_MID_MIN = 100.0
TIER_HIGH_MIN = 300.0

QUESTION_LEN = 60


def _short(text: str | None, limit: int = QUESTION_LEN) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main() -> None:
    parser = argparse.ArgumentParser(description="market_screen 落库只读体检（不写库）")
    parser.add_argument("--date", help="run_date YYYY-MM-DD，默认今天（UTC+8）")
    parser.add_argument("--top", type=int, default=20, help="候选清单 Top N，默认 20")
    parser.add_argument("--all", action="store_true", help="列出当日全部候选（覆盖 --top）")
    parser.add_argument("--db", default=DB_PATH, help="SQLite 库路径")
    args = parser.parse_args()

    tz = timezone(timedelta(hours=8))
    today = args.date or datetime.now(tz).date().isoformat()
    limit = 10_000 if args.all else args.top

    # 只执行 SELECT，不 commit；连接关闭后不产生写事务。
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    print("== 最近 5 轮运行（market_screen_run）==")
    runs = con.execute(
        """
        SELECT run_date, started_at, finished_at, elapsed_ms, pages_fetched,
               candidate_count, passed_count, status, error
        FROM market_screen_run
        ORDER BY run_date DESC LIMIT 5
        """
    ).fetchall()
    if not runs:
        print("  (无记录)")
    for row in runs:
        print(dict(row))

    print(f"\n== {today} 明细概况 ==")
    summary = con.execute(
        """
        SELECT COUNT(*) AS rows,
               COUNT(rewards_daily_rate) AS with_reward,
               ROUND(AVG(rewards_daily_rate), 2) AS avg_rate
        FROM market_screen_daily
        WHERE run_date = ?
        """,
        (today,),
    ).fetchone()
    print(dict(summary))

    print("\n== 过门情况（screen_passed / screen_reason）==")
    gates = con.execute(
        """
        SELECT screen_passed, screen_reason, COUNT(*) AS n
        FROM market_screen_daily
        WHERE run_date = ?
        GROUP BY screen_passed, screen_reason
        ORDER BY screen_passed DESC, n DESC
        """,
        (today,),
    ).fetchall()
    if not gates:
        print("  (今日无明细)")
    for row in gates:
        print(dict(row))

    print("\n== 奖励代币分布 ==")
    assets = con.execute(
        """
        SELECT rewards_asset, COUNT(*) AS n,
               ROUND(MIN(rewards_daily_rate), 2) AS min_rate,
               ROUND(MAX(rewards_daily_rate), 2) AS max_rate
        FROM market_screen_daily
        WHERE run_date = ?
        GROUP BY rewards_asset
        ORDER BY n DESC
        """,
        (today,),
    ).fetchall()
    if not assets:
        print("  (今日无明细)")
    for row in assets:
        print(dict(row))

    print("\n== 分档分布（low [30,100) / mid [100,300) / high ≥300）==")
    tiers = con.execute(
        """
        SELECT CASE WHEN rewards_daily_rate >= ? THEN 'high'
                    WHEN rewards_daily_rate >= ? THEN 'mid'
                    WHEN rewards_daily_rate >= ? THEN 'low' END AS tier,
               COUNT(*) AS n
        FROM market_screen_daily
        WHERE run_date = ?
        GROUP BY tier
        ORDER BY n DESC
        """,
        (TIER_HIGH_MIN, TIER_MID_MIN, TIER_LOW_MIN, today),
    ).fetchall()
    if not tiers:
        print("  (今日无明细)")
    for row in tiers:
        print(dict(row))

    print(f"\n== 当日候选清单（limit={limit}，奖励优先，同速率按成交额）==")
    top = con.execute(
        """
        SELECT rank_no, rewards_daily_rate, rewards_asset,
               ROUND(volume_24h) AS vol24h, remaining_days,
               ROUND(minority_prob, 4) AS minority, screen_passed, slug, question
        FROM market_screen_daily
        WHERE run_date = ?
        ORDER BY rewards_daily_rate DESC, volume_24h DESC
        LIMIT ?
        """,
        (today, limit),
    ).fetchall()
    if not top:
        print("  (今日无明细)")
    for row in top:
        item = dict(row)
        item["question"] = _short(item.get("question"))
        print(item)

    con.close()


if __name__ == "__main__":
    main()
