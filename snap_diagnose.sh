#!/usr/bin/env bash
# 快照停更一键取证（在 EC2 宿主机 docker-compose.yml 所在目录运行）
# 用法：bash snap_diagnose.sh
# 自动探测 docker（含 sudo 场景）；找不到 docker 时直接读宿主机挂载的库与配置
set -u
C=polymarket-tracker

# 找一个可用的 docker 命令（Amazon Linux 通常需要 sudo）
DOCKER=""
if command -v docker >/dev/null 2>&1; then
  DOCKER="docker"
elif command -v sudo >/dev/null 2>&1 && sudo -n docker ps >/dev/null 2>&1; then
  DOCKER="sudo docker"
fi

if [ -z "$DOCKER" ]; then
  echo "================ 未找到 docker 命令，改为宿主机直连模式 ================"
  echo "（直接读 ./data/monitor.db 与 ./config/markets.toml，效果等价）"
  echo
  if [ ! -f data/monitor.db ]; then
    echo "当前目录没有 data/monitor.db，请先 cd 到实际部署目录（含 config/ data/ 的那个）。"
    echo "当前目录: $(pwd)"
    ls -la
    exit 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "宿主机没有 python3，请先安装：sudo yum install -y python3"
    exit 1
  fi
  python3 snap_diagnose.py

  echo
  echo "================ 进程状态（确认服务是怎么跑的）================"
  ps -ef | grep -E 'src\.main|src/main' | grep -v grep || echo "未发现正在运行的 src.main 进程"
  systemctl list-units --type=service 2>/dev/null | grep -iE 'poly|tracker' || echo "（无相关 systemd 服务）"

  echo
  echo "================ 关键日志计数（logs/ 目录）================"
  if ls logs/monitor.log* >/dev/null 2>&1; then
    cat logs/monitor.log* 2>/dev/null \
      | grep -oE 'snap_no_current_bucket|snap_no_price|snap_volume_fetch_failed|snap_price_fetch_failed|snap_live_price_failed|snap_book_fetch_failed|快照市场标识解析失败|prev-bucket skip|任务 snapshot_collect 异常退出' \
      | sort | uniq -c
    echo "--- 最近 10 条 snapshot collected（健康时每 2h 6 条）---"
    grep -h 'snapshot collected' logs/monitor.log* 2>/dev/null | tail -10
  else
    echo "未找到 logs/monitor.log*；若用 systemd 运行，执行："
    echo "  journalctl -u <服务名> --since '24 hours ago' | grep -E 'snap_no|snapshot collected|fetch_failed' | tail -30"
  fi
  exit 0
fi

echo "================ 容器状态 ================"
$DOCKER inspect -f 'StartedAt={{.State.StartedAt}}  RestartCount={{.RestartCount}}  Status={{.State.Status}}' "$C"

echo
echo "================ 近 24h 关键日志计数（可用此判定根因分支）================"
$DOCKER logs --since 24h "$C" 2>&1 \
  | grep -oE 'snap_no_current_bucket|snap_no_price|snap_volume_fetch_failed|snap_price_fetch_failed|snap_live_price_failed|snap_book_fetch_failed|快照市场标识解析失败|prev-bucket skip|任务 snapshot_collect 异常退出' \
  | sort | uniq -c

echo
echo "================ 最近 10 条 snapshot collected（健康时每 2h 6 条）================"
$DOCKER logs --since 24h "$C" 2>&1 | grep 'snapshot collected' | tail -10

echo
echo "================ 容器内 DB + API 诊断 ================"
$DOCKER exec -i "$C" python - < snap_diagnose.py
