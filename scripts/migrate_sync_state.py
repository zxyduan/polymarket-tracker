#!/usr/bin/env python3
"""P1 收尾：sync_state 键 namespace 迁移。

旧键前缀        新 namespace
snap_push:      snapshot:
daily_report:   daily_report:   （不变）
data_api:       data_api:       （不变）

幂等：重复跑不重复迁移。
"""

import sqlite3
import sys

MIGRATIONS = {
    "snap_push:": "snapshot:",
}


def main(db: str) -> None:
    con = sqlite3.connect(db)
    cur = con.cursor()
    rows = cur.execute("SELECT key, value, updated_at FROM sync_state").fetchall()
    moved = 0
    for old, value, updated in rows:
        for old_prefix, new_prefix in MIGRATIONS.items():
            if old.startswith(old_prefix):
                new = new_prefix + old[len(old_prefix):]
                cur.execute(
                    "UPDATE sync_state SET key=? WHERE key=?", (new, old)
                )
                moved += 1
                print(f"  {old} -> {new}")
    con.commit()
    print(f"done: {moved} rows migrated")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/monitor.db")
