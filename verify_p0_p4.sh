#!/bin/bash
# P0-P4 验收脚本
set -e
cd /home/ec2-user/polymarket-tracker
source .venv/bin/activate

echo "=== 1. git 状态 ==="
git log --oneline -5
echo

echo "=== 2. pytest 全量 ==="
python -m pytest -q 2>&1 | tail -5
echo

echo "=== 3. 启动冒烟（5秒后自动停） ==="
timeout 5 env P1_SWITCH=1 python -m src.main --config config/config.yaml 2>&1 | grep -E "P1|bootstrap|接管|ERROR|Traceback" || true
echo

echo "=== 4. 分层检查：sources 不 import store ==="
python -c "
import ast, pathlib
bad = []
for f in pathlib.Path('framework/sources').glob('*.py'):
    t = ast.parse(f.read_text())
    for n in ast.walk(t):
        if isinstance(n, ast.ImportFrom) and n.module and 'store' in n.module:
            bad.append(f'{f.name}: from {n.module}')
print('PASS' if not bad else f'FAIL: {bad}')
"
echo

echo "=== 5. 文件行数 ==="
wc -l src/main.py framework/bootstrap.py
echo
echo "=== 验收完成 ==="
