#!/usr/bin/env bash
# 在已跑过一次 P0 的机器上，拉取上游新提交后重跑 P0（对齐新基准 HEAD）
# 用法：放到仓库根目录，./p0-rerun.sh
set -euo pipefail

cd "$(dirname "$0")"

echo "== [0] 拉取最新 main =="
git pull --ff-only origin main
echo "当前 HEAD: $(git rev-parse HEAD)"

# 复用已建好的 .venv
. .venv/bin/activate
python --version

echo "== [1] 重新确认依赖钉版 =="
python -m pip install -r requirements-dev.txt --quiet
python -m pytest --version

echo "== [2] 重跑 143 passed（目标） =="
set +e
python -m pytest -q 2>&1 | tee doc/pytest-output.txt
PASS_RC=${PIPESTATUS[0]}
set -e
grep -E "passed|failed|error" doc/pytest-output.txt | tail -3

echo "== [3] 重新生成基线清单 =="
{
  echo "# baseline-files.txt（wc -l 口径；末行无换行符可能差 1 行）"
  echo "# 机器: $(hostname)  日期: $(date -Iseconds)"
  echo "# HEAD: $(git rev-parse HEAD)"
  find src tests config -type f \
    \( -name '*.py' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' -o -name '*.ini' -o -name '*.example' \) \
    -not -path '*/__pycache__/*' -not -name '*.pyc' -not -path '*/.pytest_cache/*' -not -path '*/.venv/*' \
    -print0 | xargs -0 wc -l
  wc -l requirements.txt requirements-dev.txt Dockerfile pytest.ini
} > doc/baseline-files.txt

{
  echo "# baseline-sha256.csv（排除 __pycache__/*.pyc/.pytest_cache/.venv）"
  echo "# HEAD: $(git rev-parse HEAD)"
  find src tests config -type f \
    -not -path '*/__pycache__/*' -not -name '*.pyc' -not -path '*/.pytest_cache/*' -not -path '*/.venv/*' \
    -print0 | sort -z | xargs -0 sha256sum
  sha256sum requirements.txt requirements-dev.txt Dockerfile pytest.ini
} > doc/baseline-sha256.csv

echo "== [4] 更新 p0-baseline.md =="
{
  echo "# P0 基线记录"
  echo
  echo "- 机器: $(hostname)（$(uname -a)）"
  echo "- HEAD: $(git rev-parse HEAD)"
  echo "- venv Python: $(python --version 2>&1)"
  echo "- pytest: $(python -m pytest --version 2>&1 | head -1)"
  echo "- pytest-asyncio: $(python -c 'import pytest_asyncio;print(pytest_asyncio.__version__)' 2>/dev/null || echo 未装)"
  echo "- pytest 结果: $(grep -E 'passed|failed' doc/pytest-output.txt | tail -1)"
  echo "- pytest exit code: $PASS_RC"
  echo "- 行数清单: baseline-files.txt；哈希清单: baseline-sha256.csv"
} > doc/p0-baseline.md

echo
echo "== 完成 =="
echo "退出码: $PASS_RC（必须 0，且尾部显示 143 passed）"
git add doc/baseline-files.txt doc/baseline-sha256.csv doc/pytest-output.txt doc/p0-baseline.md
echo "确认无误后: git commit -m 'p0: realign baseline to HEAD $(git rev-parse --short HEAD)' && git push"
#（注：内容由AI生成）
