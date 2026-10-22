#!/bin/bash
# ============================================================
#  polymarket-tracker 服务器一键健康体检（傻瓜版 v1.0）
#
#  用法（三种模式，任选）:
#    bash health.sh           一键体检全部项目（推荐，直接看输出）
#    bash health.sh --watch   每 5 秒实时刷新关键指标（Ctrl+C 退出）
#    bash health.sh --fault   服务器打不开/卡住时 3 秒快速定位根因
#
#  适用: Amazon Linux / CentOS / 任何 systemd 发行版
# ============================================================

APP_DIR="$HOME/polymarket-tracker"        # 项目目录，如有不同请改这里
LOG="$APP_DIR/logs/monitor.log"
DB="$APP_DIR/data/monitor.db"

# 颜色（纯输出友好，黑/白终端也兼容）
GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; CYAN='\033[36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}   $1"; }
warn() { echo -e "${YELLOW}[!!]${NC}  $1"; }
bad()  { echo -e "${RED}[XX]${NC}  $1"; }
info() { echo -e "${CYAN}[==]${NC}  $1"; }

# ------------------------------------------------------------
# 1) 服务与进程
# ------------------------------------------------------------
svc_check() {
  echo "========== 1. 服务与进程 =========="
  if systemctl is-active polymarket-tracker >/dev/null 2>&1; then
    ok "polymarket-tracker 运行中"
  else
    bad "polymarket-tracker 未运行 / 异常（原因见下方日志）"
  fi
  PIDS=$(pgrep -f "src\.main" | tr '\n' ' ')
  if [ -n "$PIDS" ]; then
    ok "进程存在 PID: $PIDS"
    ps -o pid,etime,%cpu,%mem,cmd -p $(echo $PIDS | tr ' ' ',') 2>/dev/null | tail -n +1
  else
    bad "没有 src.main 进程"
  fi
}

# ------------------------------------------------------------
# 2) 资源占用（CPU / 内存 Top5）
# ------------------------------------------------------------
resource_top() {
  echo "========== 2. CPU 占用 Top5 =========="
  ps aux --sort=-%cpu | awk 'NR==1 || (NR>=2 && NR<=6) {printf "%-8s %-6s CPU:%4s%% MEM:%4s%% %s\n", $1, $2, $3, $4, $11}'
  echo "========== 3. 内存占用 Top5 =========="
  ps aux --sort=-%mem | awk 'NR==1 || (NR>=2 && NR<=6) {printf "%-8s %-6s MEM:%4s%% %s\n", $1, $2, $4, $11}'
  echo "========== 4. 内存 / 磁盘 / 负载 =========="
  free -h | awk 'NR<=2 {print}'
  df -h / | tail -1
  uptime
}

# ------------------------------------------------------------
# 3) 僵尸进程
# ------------------------------------------------------------
zombie_check() {
  echo "========== 5. 僵尸进程 =========="
  Z=$(ps aux | awk '$8=="Z"' | grep -v grep | wc -l)
  if [ "$Z" -eq 0 ]; then ok "无僵尸进程"
  else warn "发现 $Z 个僵尸进程："; ps aux | awk '$8=="Z"' | grep -v grep
  fi
}

# ------------------------------------------------------------
# 4) 陌生/可疑进程（非系统白名单且吃资源）
# ------------------------------------------------------------
strange_check() {
  echo "========== 6. 陌生/可疑进程（高占用非白名单） =========="
  ps aux --sort=-%cpu | awk '$3>3 || $4>3' | grep -vE "ps aux|awk|grep|bash|python|polymarket|sshd|systemd|kthreadd|kworker|ksoftirqd|migration|rcu|kcompactd|kauditd|kdevtmpfs|khelper|khungtaskd|kblockd|kthrotld|writeback|jbd2|ext4|bioset|scsi|ata_|nvme|acpi|pm_|watchdog|cpuhp|md_|loop|rpciod|xfs|jfs|kjournald|logd|thermal|ipmi|tpm|edac|cryptd|kernfs|cfg80211|phy0|charger|sysmon|tls|auth|unix|sock|netns|memcg|io-uring|cgroup|oom_reaper|zswap|kvm|vfio|virtio|gpu|drm|snd|card|i2c|usb|bdi|fsnotify|dio|dax|mmc|rds|nfsd|sunrpc|lockd|rpciod|nfs|ceph|libata|scsi|usb-storage|xhci|ehci|hub|tty|ttyS|pty|console|hid|evdev|joydev|input|rtc|serio|wacom|udp|tcp|dccp|sctp|udplite|ip6|nf_|iptable|nf_tables|br_|veth|docker|containerd|runc|chronyd|agetty|dbus|NetworkManager|polkitd|unattended-upgr|snapd|irqbalance|rsyslog|auditd|crond|atd|sshd|login|getty|amazon|cloud-init|ec2|newrelic|collectd|node_exporter|prometheus" | awk 'NR>=1 {printf "  %-8s %-6s CPU:%4s%% MEM:%4s%% %s\n", $1, $2, $3, $4, $11}'
  echo "  （上面为空 = 无异常；有内容请留意陌生进程名）"
}

# ------------------------------------------------------------
# 5) 内核 OOM 记录（服务器卡死/重启的真凶往往在这）
# ------------------------------------------------------------
oom_check() {
  echo "========== 7. 最近被内核杀掉的进程（OOM） =========="
  R=$(dmesg 2>/dev/null | grep -iE "oom|killed process" | tail -5)
  if [ -z "$R" ]; then R=$(journalctl -k --no-pager 2>/dev/null | grep -iE "oom|killed process" | tail -5); fi
  if [ -n "$R" ]; then warn "发现 OOM 记录："; echo "$R"
  else ok "无 OOM 记录（近期没有被内存杀进程）"
  fi
}

# ------------------------------------------------------------
# 6) 最近日志 + 数据库体积
# ------------------------------------------------------------
log_and_db() {
  echo "========== 8. 最近日志（tail 5） =========="
  if [ -f "$LOG" ]; then tail -5 "$LOG"
  else journalctl -u polymarket-tracker -n 5 --no-pager 2>/dev/null || echo "（找不到日志）"
  fi
  echo "========== 9. 数据库体积 =========="
  if [ -f "$DB" ]; then
    ls -lh "$DB" "$DB-wal" "$DB-shm" 2>/dev/null
    sqlite3 "$DB" "SELECT 'events', COUNT(*) FROM events UNION ALL SELECT 'notifications', COUNT(*) FROM notifications UNION ALL SELECT 'market_snapshots', COUNT(*) FROM market_snapshots;" 2>/dev/null \
      | awk '{printf "  %-18s %s 行\n", $1, $2}' || echo "  （sqlite3 未安装或无库）"
  else echo "  （数据库不存在，可能还没采集过）"
  fi
}

# ------------------------------------------------------------
# 模式一：一键体检
# ------------------------------------------------------------
full_check() {
  echo -e "${CYAN}========  polymarket-tracker 体检  $(date '+%F %H:%M:%S')  ========${NC}"
  svc_check
  resource_top
  zombie_check
  strange_check
  oom_check
  log_and_db
  echo -e "${CYAN}========  体检结束：红色=有问题，黄色=注意，绿色=正常  ========${NC}"
}

# ------------------------------------------------------------
# 模式二：实时刷新（--watch）
# ------------------------------------------------------------
watch_mode() {
  echo "实时监控中（每 5 秒刷新，Ctrl+C 退出）"
  while true; do
    clear
    echo "服务: $(systemctl is-active polymarket-tracker 2>/dev/null)     $(date '+%F %H:%M:%S')"
    ps aux --sort=-%cpu | awk 'NR<=6 {printf "%-8s %-6s CPU:%4s%% MEM:%4s%% %s\n", $1, $2, $3, $4, $11}'
    free -h | awk 'NR<=2 {print}'
    uptime
    sleep 5
  done
}

# ------------------------------------------------------------
# 模式三：快速定位（--fault）—— 打不开/卡住时用
# ------------------------------------------------------------
fault_mode() {
  echo -e "${CYAN}========  快速定位（服务器打不开/卡住用）  ========${NC}"
  echo "--- 1) OOM（内存被杀，最常见） ---"
  dmesg 2>/dev/null | grep -iE "oom|killed process" | tail -5 || journalctl -k --no-pager 2>/dev/null | grep -iE "oom|killed process" | tail -5 || echo "无"
  echo "--- 2) 磁盘（使用率>90% 危险） ---"
  df -h / | tail -1
  echo "--- 3) 当前最吃 CPU 的 8 个进程 ---"
  ps aux --sort=-%cpu | head -9 | awk 'NR>=1 {printf "%-8s %-6s CPU:%4s%% MEM:%4s%% %s\n", $1, $2, $3, $4, $11}'
  echo "--- 4) 最近 20 行应用日志 ---"
  tail -20 "$LOG" 2>/dev/null || journalctl -u polymarket-tracker -n 20 --no-pager 2>/dev/null || echo "无日志"
}

# ------------------------------------------------------------
# 入口
# ------------------------------------------------------------
case "$1" in
  --watch) watch_mode ;;
  --fault) fault_mode ;;
  *)       full_check ;;
esac
#（注：内容由AI生成）
