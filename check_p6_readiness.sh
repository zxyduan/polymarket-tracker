#!/bin/bash
# P6 就绪检查
cd /home/ec2-user/polymarket-tracker
source .venv/bin/activate

echo "=== P6 就绪检查 ==="
echo

echo "1. pytest 全量"
python -m pytest -q 2>&1 | tail -1
echo

echo "2. main.py 行数（P5 目标 ≤200）"
wc -l src/main.py
echo

echo "3. VolumeTracker 是否还在使用（B4）"
grep -l "VolumeTracker" src/snapshots/*.py
echo

echo "4. 旧 trigger 函数是否还在 main.py"
grep -c "trigger_snapshot_push\|trigger_daily_report" src/main.py
echo

echo "5. config.example 是否有 daily_report 节"
grep -c "daily_report" config/config.yaml.example 2>/dev/null || echo "0"
echo

echo "6. FEATURE_RULES.md 是否存在"
ls doc/FEATURE_RULES.md 2>/dev/null || echo "不存在"
echo

echo "=== 结论 ==="
echo "P5 差：main 瘦身 + 删 B4"
echo "P6 待做：架构脚本 + FEATURE_RULES.md + config.example 补全"
