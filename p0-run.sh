#!/usr/bin/env bash
# P0 基线脚本（对应 39 §2.2 P0-A ~ P0-F）
# 用法：
#   1) 放到 polymarket-tracker 仓库根目录（与 src/ tests/ config/ 同级）
#   2) chmod +x p0-run.sh
#   3) ./p0-run.sh
# 前置：机器有 python3.11、有网（能装 requirements-dev.txt）；git 已拉到基准提交。
# 产物全部落在 doc/ 下：p0-baseline.md、baseline-files.txt、baseline-sha256.csv、pytest-output.txt
set -euo pipefail

HEAD="ad4ec4a7391709e8263439c3e92e722db22d2cf4"
OUT=doc
mkdir -p "$OUT"

echo "== [0] 环境自检 =="
python3.11 --version || { echo "缺 python3.11：先装 3.11（生产镜像是 python:3.11-slim，不要用 3.13）"; exit 1; }
git rev-parse HEAD
echo "期望 HEAD: $HEAD"

echo "== [P0-B] 隔离 venv =="
if [ ! -d .venv ]; then
  python3.11 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python --version
python -c "import sys; assert sys.prefix.startswith('$PWD/.venv'), '必须在 venv 内'; print('venv ok:', sys.prefix)"

echo "== [P0-C] 钉版依赖 =="
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements-dev.txt
python -m pytest --version
python -c "import pytest_asyncio; print('pytest-asyncio', pytest_asyncio.__version__)"

echo "== [P0-A] 行数清单 =="
{
  echo "# baseline-files.txt（wc -l 口径；末行无换行符可能差 1 行）"
  echo "# 机器: $(hostname)  日期: $(date -Iseconds)"
  echo "# OS: $(uname -a)"
  find src tests config -type f \
    \( -name '*.py' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' -o -name '*.ini' -o -name '*.example' \) \
    -not -path '*/__pycache__/*' -not -name '*.pyc' -not -path '*/.pytest_cache/*' -not -path '*/.venv/*' \
    -print0 | xargs -0 wc -l
  wc -l requirements.txt requirements-dev.txt Dockerfile pytest.ini
} > "$OUT/baseline-files.txt"
echo "行数清单 -> $OUT/baseline-files.txt"

echo "== [P0-A] SHA-256 清单 =="
{
  echo "# baseline-sha256.csv（排除 __pycache__/*.pyc/.pytest_cache/.venv）"
  find src tests config -type f \
    -not -path '*/__pycache__/*' -not -name '*.pyc' -not -path '*/.pytest_cache/*' -not -path '*/.venv/*' \
    -print0 | sort -z | xargs -0 sha256sum
  sha256sum requirements.txt requirements-dev.txt Dockerfile pytest.ini
} > "$OUT/baseline-sha256.csv"
echo "哈希清单 -> $OUT/baseline-sha256.csv"

echo "== [P0-D] 全绿基线（目标 143 passed） =="
set +e
python -m pytest -q 2>&1 | tee "$OUT/pytest-output.txt"
PASS_RC=${PIPESTATUS[0]}
set -e
echo "pytest exit code: $PASS_RC"
grep -E "passed|failed|error" "$OUT/pytest-output.txt" | tail -3

echo "== [P0-E] golden 快照 =="
ls -l tests/golden/ 2>/dev/null || echo "（无 tests/golden/ 目录；四件快照 G-F1-事件/G-F2-采集/G-F2-推送/G-F3-日报 应在首次快照生成测试跑过后出现）"

echo "== [P0-F] B4/B5 引用清单 =="
{
  echo "# VolumeTracker:";        grep -rn "VolumeTracker" src tests || true
  echo "# BotRunner.run_sender:"; grep -rn "BotRunner.run_sender\|run_sender" src tests || true
  echo "# 模块级 run_sender:";     grep -rn "^run_sender" src tests || true
} | tee "$OUT/b4b5-refs.txt"

echo "== 写 p0-baseline.md =="
{
  echo "# P0 基线记录"
  echo
  echo "- 机器: $(hostname)（$(uname -a)）"
  echo "- HEAD: $(git rev-parse HEAD)"
  echo "- venv Python: $(python --version 2>&1)"
  echo "- pytest: $(python -m pytest --version 2>&1 | head -1)"
  echo "- pytest-asyncio: $(python -c 'import pytest_asyncio;print(pytest_asyncio.__version__)' 2>/dev/null || echo 未装)"
  echo "- pytest 结果: $(grep -E 'passed|failed' "$OUT/pytest-output.txt" | tail -1)"
  echo "- pytest exit code: $PASS_RC"
  echo "- 行数清单: baseline-files.txt"
  echo "- 哈希清单: baseline-sha256.csv"
  echo "- golden 快照: tests/golden/（四件）"
  echo "- B4/B5 引用: b4b5-refs.txt"
} > "$OUT/p0-baseline.md"

echo
echo "== 完成 =="
echo "把 doc/p0-baseline.md、baseline-files.txt、baseline-sha256.csv、pytest-output.txt 留存；"
echo "确认 exit code=0 且 pytest 显示 143 passed 后，才允许进 P1。"
#（注：内容由AI生成）
