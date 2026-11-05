#!/usr/bin/env bash
# Polymarket-Tracker 只读自检脚本：服务、提交基线、F1 maker 覆盖、代码标记、
# 启动日志、C1 漏斗、DB 通知覆盖、快照/日报 state。
# 用法：
#   ./docs/self-check.sh
#   ./docs/self-check.sh /home/ec2-user/polymarket-tracker
# 本脚本不重启服务、不写代码、不修改数据库；数据库以只读方式打开。

set -u

REPO="${1:-/home/ec2-user/polymarket-tracker}"
SERVICE="${SERVICE_NAME:-polymarket-tracker}"
DB_PATH="$REPO/data/monitor.db"
# 462fbe0：统一修复；c8c3017：F1 maker/非 taker 成交兜底覆盖。
EXPECTED_HEADS=("462fbe0" "c8c3017")

PASS_N=0
WARN_N=0
FAIL_N=0
WARN_ITEMS=()
FAIL_ITEMS=()

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  RED=$'\033[31m'
  YELLOW=$'\033[33m'
  GREEN=$'\033[32m'
  BOLD=$'\033[1m'
  RESET=$'\033[0m'
else
  RED=""
  YELLOW=""
  GREEN=""
  BOLD=""
  RESET=""
fi

section() { printf '\n===== %s =====\n' "$1"; }
ok()   { printf '%s[OK]%s   %s\n' "$GREEN" "$RESET" "$1"; PASS_N=$((PASS_N + 1)); }
warn() {
  printf '%s[WARN]%s %s\n' "$YELLOW" "$RESET" "$1"
  WARN_N=$((WARN_N + 1))
  WARN_ITEMS+=("$1")
}
fail() {
  printf '%s[FAIL]%s %s\n' "$RED" "$RESET" "$1"
  FAIL_N=$((FAIL_N + 1))
  FAIL_ITEMS+=("$1")
}
add_hint() {
  local item="$1"
  case "$item" in
    *RTDS*) echo "    建议：检查 journalctl 中 RTDS 连接、订阅与重连记录" ;;
    *通道*) echo "    建议：检查 config/config.yaml 对应通道 enabled 与 ntfy target" ;;
    *漏斗*) echo "    建议：等待第一个统计周期；仍无则检查 funnel_watch_loop 日志" ;;
    *数据库不存在*) echo "    建议：非 EC2 环境可忽略；EC2 上必须检查 data/monitor.db" ;;
    *未跟踪*) echo "    建议：bash docs/cleanup-working-tree.sh 归档临时补丁/重复脚本" ;;
    *未提交修改*) echo "    建议：git status 核对源码改动，确认后提交或 stash" ;;
  esac
}

if [[ ! -d "$REPO" ]]; then
  echo "仓库目录不存在: $REPO"
  exit 2
fi
cd "$REPO" || exit 2

PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

section "1. 仓库与服务基线"
PID=""
HAS_SYSTEMCTL=0
if command -v systemctl >/dev/null 2>&1; then
  ACTIVE=$(systemctl is-active "$SERVICE" 2>/dev/null || true)
  PID=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || true)
  RESTARTS=$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || true)
  ACTIVE_TS=$(systemctl show -p ActiveEnterTimestamp --value "$SERVICE" 2>/dev/null || true)
  echo "service=$SERVICE active=$ACTIVE pid=${PID:-?} restarts=${RESTARTS:-?} since=$ACTIVE_TS"
  if [[ "$ACTIVE" == "active" ]]; then
    HAS_SYSTEMCTL=1
    ok "systemd 服务 active"
    systemctl cat "$SERVICE" 2>/dev/null | grep -E '^(WorkingDirectory|ExecStart)=' || warn "无法读取 systemd ExecStart/WorkingDirectory"
  elif [[ -z "$ACTIVE" ]]; then
    warn "systemctl 不可用或无该服务，跳过服务状态与启动日志判定（EC2 上必须查看本节）"
  else
    fail "systemd 服务不是 active: $ACTIVE"
  fi
else
  warn "当前环境没有 systemctl，跳过服务状态与启动日志判定（EC2 上必须查看本节）"
fi

HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
BRANCH=$(git branch --show-current 2>/dev/null || echo unknown)
EXPECTED_JOINED=$(IFS=","; echo "${EXPECTED_HEADS[*]}")
echo "branch=$BRANCH head=$HEAD expected_heads=$EXPECTED_JOINED"
HEAD_HAS_F1=0
HEAD_HAS_BASE=0
if git merge-base --is-ancestor c8c3017 HEAD 2>/dev/null; then
  HEAD_HAS_F1=1
fi
if git merge-base --is-ancestor 462fbe0 HEAD 2>/dev/null; then
  HEAD_HAS_BASE=1
fi
if [[ "$HEAD_HAS_F1" -eq 1 ]]; then
  ok "HEAD 包含 F1 修复提交 c8c3017（后续文档/脚本提交不影响判定）"
elif [[ "$HEAD_HAS_BASE" -eq 1 ]]; then
  warn "HEAD 仅包含统一修复 462fbe0，尚未确认包含 F1 修复 c8c3017"
else
  warn "HEAD 不包含 $EXPECTED_JOINED 中的修复祖先，请确认部署版本"
fi
echo "--- git status ---"
GIT_STATUS_OUTPUT=$(git status --short || true)
printf '%s\n' "$GIT_STATUS_OUTPUT"
UNTRACKED_LINES=$(printf '%s\n' "$GIT_STATUS_OUTPUT" | awk '$1 == "??" {print}')
MODIFIED_LINES=$(printf '%s\n' "$GIT_STATUS_OUTPUT" | grep -E '^(M|A|D|MM|AM|MA| M| A| D)' || true)
SELF_CHECK_LINES=$(printf '%s\n' "$MODIFIED_LINES" | grep -F 'docs/self-check.sh' || true)
SOURCE_MODIFIED_LINES=$(printf '%s\n' "$MODIFIED_LINES" | grep -vF 'docs/self-check.sh' || true)
if [[ -n "$UNTRACKED_LINES" ]]; then
  warn "存在根目录未跟踪文件/脚本，建议运行 docs/cleanup-working-tree.sh 归档"
  printf '%s\n' "$UNTRACKED_LINES" | sed 's/^/    untracked: /'
else
  ok "无未跟踪文件"
fi
if [[ -n "$SOURCE_MODIFIED_LINES" ]]; then
  warn "工作树存在源码/测试/配置未提交修改，请确认是否需要提交"
  printf '%s\n' "$SOURCE_MODIFIED_LINES" | sed 's/^/    modified: /'
elif [[ -n "$SELF_CHECK_LINES" ]]; then
  ok "仅 docs/self-check.sh 有本地更新，提交后即可清除该状态"
  printf '%s\n' "$SELF_CHECK_LINES" | sed 's/^/    pending self-check: /'
else
  ok "工作树无未提交源码修改"
fi

section "2. 关键修复代码标记"
check_marker() {
  local file="$1" marker="$2" label="$3"
  if [[ -f "$file" ]] && grep -qF -- "$marker" "$file"; then
    ok "$label"
  else
    fail "$label（未找到: $marker in $file）"
  fi
}
check_marker framework/app.py "def markets(self): return list(resolved_markets)" "日报 Catalog.markets() 已装配"
check_marker framework/app.py "max_stale_buckets=snap_max_stale" "快照守卫阈值已接线"
check_marker framework/app.py "now_bucket=0" "启动时不固定 now_bucket"
check_marker framework/app.py "交易漏斗基线" "启动输出 C1 漏斗基线"
check_marker src/snapshots/collector.py 'outcomes.index("Yes")' "Yes 侧价格按 outcomes 顺序定位"
check_marker src/core/pipeline.py '"notional_none"' "pipeline notional_none 计数器存在"
check_marker src/core/pipeline.py '"notional_below"' "pipeline notional_below 计数器存在"
check_marker features/trader/usecase.py '"dedup_hit"' "trader dedup_hit 计数器存在"
check_marker features/trader/usecase.py '"render_none"' "trader render_none 计数器存在"
check_marker features/trader/usecase.py '"dispatch_failed"' "trader dispatch_failed 计数器存在"
check_marker framework/runtime/loops.py "funnel_watch_loop" "周期漏斗日志任务存在"
check_marker framework/runtime/loops.py 'snap_skip:' "去重抑制写 snap_skip 状态"
check_marker src/sources/data_api_client.py '"taker_only": "false"' "Data API 对账覆盖 maker/非 taker 行"
check_marker src/core/parsing.py '"usdc_size"' "解析器支持 activity/snake_case 金额字段"
[[ -f tests/test_data_api_client.py ]] && ok "maker 覆盖参数测试文件存在" || fail "缺少 tests/test_data_api_client.py"

section "3. 当前进程启动证据"
LOGS=""
if [[ $HAS_SYSTEMCTL -eq 0 ]]; then
  warn "无 systemctl，跳过启动日志判定；代码存在性由第 2 节检查"
else
  if [[ -n "${PID:-}" && "$PID" =~ ^[0-9]+$ && "$PID" -gt 0 ]]; then
    LOGS=$(journalctl "_PID=$PID" --no-pager 2>/dev/null || true)
  else
    LOGS=$(journalctl -u "$SERVICE" --no-pager 2>/dev/null || true)
  fi
  [[ -n "$LOGS" ]] && ok "取得当前服务日志" || fail "没有取到服务日志"

  grep -qF "服务启动" <<<"$LOGS" && ok "存在服务启动日志" || fail "缺少服务启动日志"
  grep -qF "快照陈旧守卫阈值=" <<<"$LOGS" && ok "启动时打印陈旧守卫阈值" || fail "缺少“快照陈旧守卫阈值=”启动行"
  grep -qF "交易漏斗基线 pipeline=" <<<"$LOGS" && ok "启动时打印交易漏斗基线" || fail "缺少“交易漏斗基线”启动行"
  grep -qF "三个 feature 已接管" <<<"$LOGS" && ok "三个 feature 已接管" || fail "缺少“三个 feature 已接管”"
  grep -qF "RTDS 已连接" <<<"$LOGS" && ok "RTDS WebSocket 已连接" || warn "未见 RTDS 已连接，检查实时数据源"

  printf '\n-- 通道启动行 --\n'
  grep -E "交易员推送通道已启用|快照推送通道已启用|日报推送通道已启用" <<<"$LOGS" | tail -10 || true
  for ch in "交易员推送通道" "快照推送通道" "日报推送通道"; do
    grep -qF "$ch" <<<"$LOGS" && ok "$ch 已启用" || warn "$ch 未在当前进程日志中出现"
  done

  printf '\n-- 错误/警告计数（当前进程）--\n'
  ERR_C=$(grep -c '"level": "ERROR"' <<<"$LOGS" || true)
  WARN_C=$(grep -c '"level": "WARNING"' <<<"$LOGS" || true)
  DAILY_ERR_C=$(grep -c "日报触发异常" <<<"$LOGS" || true)
  CATALOG_ERR_C=$(grep -c "_Catalog" <<<"$LOGS" || true)
  echo "ERROR=${ERR_C:-0} WARNING=${WARN_C:-0} 日报触发异常=${DAILY_ERR_C:-0}"
  [[ "${ERR_C:-0}" -eq 0 ]] && ok "当前进程无 ERROR 日志" || fail "当前进程存在 $ERR_C 条 ERROR，见末尾摘录"
  [[ "${DAILY_ERR_C:-0}" -eq 0 ]] && ok "无日报触发异常" || fail "仍存在日报触发异常"
  [[ "${CATALOG_ERR_C:-0}" -eq 0 ]] || warn "日志仍出现 _Catalog 字样，请确认是否为旧历史日志"

  LATEST_FUNNEL=$(grep "交易漏斗" <<<"$LOGS" | tail -1 || true)
  if [[ -n "$LATEST_FUNNEL" ]]; then
    echo "latest funnel: $LATEST_FUNNEL"
    if grep -qE "dispatch_failed': [1-9]|trader_failures': [1-9]|render_none': [1-9]" <<<"$LATEST_FUNNEL"; then
      fail "最新漏斗存在失败/渲染为空计数"
    else
      ok "最新漏斗无 dispatch/render 失败"
    fi
  else
    warn "当前进程日志中暂无漏斗行，等待第一个统计周期"
  fi

  if [[ "${ERR_C:-0}" -gt 0 ]]; then
    echo "-- 最近 ERROR 摘录 --"
    grep '"level": "ERROR"' <<<"$LOGS" | tail -10 || true
  fi
fi

section "4. C1 运行时漏斗（进程内存日志；重启后清零累计）"
printf '\n-- 最新 3 条漏斗日志 --\n'
grep "交易漏斗" <<<"$LOGS" | tail -3 || true
printf '\n-- 关键分支计数（当前进程）--\n'
printf "body_hash 去重命中: %s\n" "$(grep -c "body_hash 去重命中" <<<"$LOGS" || true)"
printf "trader render None: %s\n" "$(grep -c "render 返回 None" <<<"$LOGS" || true)"
printf "notional=None 默认拒绝: %s\n" "$(grep -c "缺少 notional" <<<"$LOGS" || true)"
printf "快照未发送(empty/stale/render_none): %s\n" "$(grep -c "快照未发送 result=" <<<"$LOGS" || true)"
printf "交易分发失败: %s\n" "$(grep -c "通道推送失败" <<<"$LOGS" || true)"
printf "旧文案“整点快照已推送”: %s\n" "$(grep -c "整点快照已推送" <<<"$LOGS" || true)"

section "5. 配置快照"
CFG="$REPO/config/config.yaml"
if [[ -f "$CFG" ]]; then
  grep -nE "min_notional_usd|max_stale_buckets|event_types|aggregate_window|digest|enabled|timezone_offset" "$CFG" | head -60 || true
else
  warn "未找到 $CFG"
fi

section "6. 数据库只读对账（DB 累计值，不等同于进程内存计数）"
DB_REPORT=$(mktemp)
DB_PATH="$DB_PATH" "$PY" - <<'PY' | tee "$DB_REPORT"
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

db = os.environ.get("DB_PATH", "")
if not db or not os.path.exists(db):
    print("SELF_CHECK_WARN: 数据库不存在，跳过 DB 检查")
    sys.exit(0)

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

try:
    print("-- 24h TRADE 入库与门槛分布 --")
    rows = q(
        """
        SELECT source, wallet, tx_hash, payload_json,
               COALESCE(event_time, first_seen) AS observed_at
        FROM events
        WHERE event_type='TRADE'
          AND COALESCE(event_time, first_seen) >= datetime('now', '-24 hours')
        """
    )
    sources = {}
    none_c = below_c = pass_c = 0
    for r in rows:
        sources[r["source"]] = sources.get(r["source"], 0) + 1
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except Exception:
            payload = {}
        raw = payload.get("notional")
        if raw in (None, ""):
            none_c += 1
            continue
        try:
            notional = float(raw)
        except Exception:
            none_c += 1
            continue
        if notional < 2.0:
            below_c += 1
        else:
            pass_c += 1
    print(f"TRADE total={len(rows)} source_split={sources}")
    print(f"threshold>=2 pass={pass_c}; below={below_c}; none_or_malformed={none_c}")

    print("\n-- 最近 10 条事件 --")
    for r in q(
        """
        SELECT source, event_type, wallet, tx_hash,
               COALESCE(event_time, first_seen) AS observed_at, payload_json
        FROM events
        ORDER BY COALESCE(event_time, first_seen) DESC
        LIMIT 10
        """
    ):
        notional = ""
        try:
            notional = json.loads(r["payload_json"] or "{}").get("notional", "")
        except Exception:
            pass
        print(f"{r['observed_at']} {r['source']:>8} {r['event_type']:<10} "
              f"wallet={(r['wallet'] or '')[:10]}... notional={notional} tx={r['tx_hash'] or '-'}")

    print("\n-- 24h 通知状态 --")
    for r in q(
        """
        SELECT status, COUNT(*) AS c
        FROM notifications
        WHERE sent_at >= datetime('now', '-24 hours')
        GROUP BY status
        """
    ):
        print(f"status={r['status']:<8} count={r['c']}")

    print("\n-- 最近 8 条通知 --")
    for r in q(
        """
        SELECT sent_at, status, event_keys, substr(message_text,1,180) AS text
        FROM notifications
        ORDER BY id DESC LIMIT 8
        """
    ):
        print(f"{r['sent_at']} [{r['status']}] keys={r['event_keys'] or '-'}")
        print(f"  {r['text'] or ''}")

    print("\n-- F1 maker/非 taker 历史重点单定位（只读）--")
    f1_probes = [
        (
            "Ink BUY NO $33.84",
            "0xc255f65fd9d54c11ca1a59394668faac474add5a6f0cff9d68dea26011865250",
        ),
        (
            "Base SELL NO $20（历史缺口，不强制要求）",
            "0xc7b1e85ffbf62ceb4773c528e34473ecad2e9987adfa850ac07be9811292b538",
        ),
    ]
    for label, tx in f1_probes:
        events = q(
            """
            SELECT source, event_type, tx_hash, dedupe_key,
                   json_extract(payload_json, '$.notional') AS notional,
                   COALESCE(event_time, first_seen) AS observed_at
            FROM events
            WHERE tx_hash = ?
            """,
            (tx,),
        )
        notifications = q(
            """
            SELECT sent_at, status, event_keys, message_text
            FROM notifications
            WHERE event_keys LIKE ?
            ORDER BY id DESC LIMIT 3
            """,
            (f"{tx}:%",),
        )
        print(f"{label}: events={len(events)} success_notifications={len(notifications)}")
        is_historical_gap = label.startswith("Base")
        if len(events) == 0:
            marker = "SELF_CHECK_INFO" if is_historical_gap else "SELF_CHECK_FAIL"
            print(f"{marker}: {label} 未在 events 表定位到")
        if len(notifications) == 0:
            marker = "SELF_CHECK_INFO" if is_historical_gap else "SELF_CHECK_FAIL"
            print(f"{marker}: {label} 没有 success 通知记录")
        for r in events[:3]:
            print(f"  event: {r['observed_at']} {r['source']} notional={r['notional']} key={r['dedupe_key']}")
        for r in notifications[:3]:
            print(f"  notify: {r['sent_at']} [{r['status']}] keys={r['event_keys'] or '-'}")
            print(f"  body={(r['message_text'] or '').replace(chr(10), ' ')[:180]}")

    print("\n-- 全部通知状态汇总（重点看 success 以外计数）--")
    status_rows = q("SELECT status, COUNT(*) AS c FROM notifications GROUP BY status")
    for r in status_rows:
        print(f"status={r['status'] or '-':<8} count={r['c']}")
    non_success = sum(int(r["c"]) for r in status_rows if (r["status"] or "") != "success")
    print(f"non_success_total={non_success}")
    if non_success > 0:
        print(f"SELF_CHECK_FAIL: 存在 {non_success} 条非 success 通知，请检查分发失败或被拒绝记录")

    print("\n-- 最近 15 条成交：门槛分支与通知覆盖 --")
    print("口径：notional 是单笔原始成交；notified=YES 表示同一 tx_hash 的任一 fill 已被通知覆盖，避免 rtds/data_api 重复键误判。")
    coverage = q(
        """
        SELECT e.event_time, e.source, e.wallet, e.tx_hash, e.dedupe_key,
               (SELECT w.label FROM wallets w
                 WHERE w.address = e.wallet OR w.proxy_address = e.wallet
                 LIMIT 1) AS label,
               CAST(json_extract(e.payload_json, '$.notional') AS REAL) AS notional,
               CASE
                 WHEN json_extract(e.payload_json, '$.notional') IS NULL THEN 'none'
                 WHEN CAST(json_extract(e.payload_json, '$.notional') AS REAL) < 2.0 THEN 'below'
                 ELSE 'pass'
               END AS threshold_state,
               CASE WHEN EXISTS(
                   SELECT 1 FROM notifications n
                   JOIN events e2 ON e2.tx_hash = e.tx_hash
                   WHERE ',' || COALESCE(n.event_keys, '') || ','
                         LIKE '%,' || e2.dedupe_key || ',%'
               ) THEN 'YES' ELSE 'NO' END AS notified
        FROM events e
        WHERE e.event_type = 'TRADE'
        ORDER BY COALESCE(e.event_time, e.first_seen) DESC
        LIMIT 15
        """
    )
    for r in coverage:
        label = r["label"] or (r["wallet"] or "")[:10]
        print(f"{r['event_time']} {r['source']:>8} {label:<10} "
              f"notional={r['notional']} threshold={r['threshold_state']:<5} "
              f"notified={r['notified']} key={r['dedupe_key']}")

    print("\n-- snapshot state 键 --")
    push_c = skip_c = 0
    for r in q(
        """
        SELECT key, value, updated_at FROM sync_state
        WHERE key LIKE 'snap_push:%' OR key LIKE 'snap_skip:%'
        ORDER BY updated_at DESC LIMIT 20
        """
    ):
        if r["key"].startswith("snap_push:"):
            push_c += 1
        elif r["key"].startswith("snap_skip:"):
            skip_c += 1
        print(f"{r['updated_at']} {r['key']}={r['value']}")
    print(f"latest-20: snap_push={push_c} snap_skip={skip_c}")

    print("\n-- daily state --")
    for r in q(
        """
        SELECT key, value, updated_at FROM sync_state
        WHERE key LIKE 'daily%'
        ORDER BY updated_at DESC LIMIT 10
        """
    ):
        print(f"{r['updated_at']} {r['key']}={r['value']}")

    print("\n-- 最近 10 条市场快照桶 --")
    for r in q(
        """
        SELECT condition_id, slug_id, bucket_ts, price_close, volume_shares, collected_ts
        FROM market_snapshots
        ORDER BY bucket_ts DESC, collected_ts DESC LIMIT 10
        """
    ):
        ts = datetime.fromtimestamp(r["collected_ts"], tz=timezone.utc).isoformat() \
            if r["collected_ts"] else "-"
        print(f"bucket={r['bucket_ts']} slug={r['slug_id']:<8} price_close={r['price_close']:.4f} "
              f"volume_shares={r['volume_shares']} collected={ts}")
except sqlite3.Error as exc:
    print(f"SELF_CHECK_WARN: SQLite 查询失败: {exc}")
PY
while IFS= read -r db_line; do
  case "$db_line" in
    "SELF_CHECK_FAIL:"*) fail "${db_line#SELF_CHECK_FAIL: }" ;;
    "SELF_CHECK_WARN:"*) warn "${db_line#SELF_CHECK_WARN: }" ;;
  esac
done < "$DB_REPORT"
rm -f "$DB_REPORT"

section "7. 建议人工复核命令"
cat <<'EOF'
# 实时跟踪交易漏斗与异常
journalctl -u polymarket-tracker -f --no-pager | grep -E "交易漏斗|对账补入|去重命中|ERROR|Traceback|快照未发送|推送失败"

# 当前进程完整错误
journalctl _PID=<当前PID> --no-pager | grep -E "ERROR|Traceback|推送失败|对账轮次异常" | tail -50

# 最近窗口外部成交对账（避免旧历史缺口干扰当前判定）
.venv/bin/python docs/external-trade-reconcile.py --label MaxEnt --hours 1 --limit 50 --raw 10

# 工作树整理：根目录旧 patch/调试脚本归档到 docs/archive（docs/ 被 gitignore，不会误提交）
bash docs/cleanup-working-tree.sh

# 下一个北京时间整点快照后核对 state
sqlite3 data/monitor.db "SELECT key,value,updated_at FROM sync_state WHERE key LIKE 'snap_%' ORDER BY updated_at DESC LIMIT 20;"
EOF

printf '\n===== 结论汇总 =====\n'
if [[ "$FAIL_N" -gt 0 ]]; then
  printf '%s%sFAIL: %d 个失败项, %d 个警告项, %d 个通过项%s\n' \
    "$RED" "$BOLD" "$FAIL_N" "$WARN_N" "$PASS_N" "$RESET"
  printf '%s判定：存在阻断性失败项，不建议判定为当前服务正常。%s\n\n' "$RED" "$RESET"
  printf '%s-- 必须处理：失败项清单 --%s\n' "$BOLD" "$RESET"
  for i in "${!FAIL_ITEMS[@]}"; do
    printf '%s%2d) FAIL%s %s\n' "$RED" "$((i + 1))" "$RESET" "${FAIL_ITEMS[$i]}"
    add_hint "${FAIL_ITEMS[$i]}"
  done
  exit 1
fi

if [[ "$WARN_N" -gt 0 ]]; then
  printf '%s%sWARN: %d 个警告项, %d 个通过项%s\n' \
    "$YELLOW" "$BOLD" "$WARN_N" "$PASS_N" "$RESET"
  printf '%s判定：主流程可能可用，但下面警告项需要观察或人工复核。%s\n\n' "$YELLOW" "$RESET"
  printf '%s-- 待观察：警告项清单 --%s\n' "$BOLD" "$RESET"
  for i in "${!WARN_ITEMS[@]}"; do
    printf '%s%2d) WARN%s %s\n' "$YELLOW" "$((i + 1))" "$RESET" "${WARN_ITEMS[$i]}"
    add_hint "${WARN_ITEMS[$i]}"
  done
  printf '\n提示：黄色 WARN 不等于功能失败；红色 FAIL 才是必须先处理的阻断项。\n'
  exit 0
fi

printf '%sPASS: %d 个检查项全部通过%s\n' "$GREEN" "$PASS_N" "$RESET"
printf '%s判定：未发现阻断性失败项和警告项。%s\n' "$GREEN" "$RESET"
exit 0
