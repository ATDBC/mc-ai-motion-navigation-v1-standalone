#!/bin/sh
# 用法：
#   PROJECT=<origin/main 98a0542 检出> BEFORE=<5c1485d 检出> D103=<07fa1d7 检出> sh run_all.sh
# 只调用项目函数和项目测试并计时，不改项目文件。输出写到本目录的 output/。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
git -C "$PROJECT" rev-parse HEAD > "$HERE/output/project_head.txt"
cd "$PROJECT"
PYTHONPATH=. taskset -c 2 python "$HERE/e22_r1_digest_gap.py" > "$HERE/output/e22_r1_digest_gap.txt" 2>&1
PYTHONPATH=. taskset -c 2 python "$HERE/e23_r3_prior_bounds.py" > "$HERE/output/e23_r3_prior_bounds.txt" 2>&1
PYTHONPATH=. taskset -c 2 python "$HERE/e24_r4_approach_fragility.py" > "$HERE/output/e24_r4_approach_fragility.txt" 2>&1
: > "$HERE/output/e26_f1_tick_cost.txt"
for tree in "$BEFORE" "$PROJECT"; do
  (cd "$tree" && PYTHONPATH=. taskset -c 2 python "$HERE/e26_f1_tick_cost.py" >> "$HERE/output/e26_f1_tick_cost.txt" 2>&1)
done
E25_TREES="$BEFORE $D103 $PROJECT" sh "$HERE/e25_suite_regression.sh" > "$HERE/output/e25_suite_regression.txt" 2>&1
# E27：慢模块前后对比（约 40 分钟）
{ echo "== BEFORE"; sh "$HERE/e27_slow_modules.sh" "$BEFORE" 2; echo "== PROJECT"; sh "$HERE/e27_slow_modules.sh" "$PROJECT" 2; } > "$HERE/output/e27_slow_modules.rerun.txt" 2>&1
# 分片计时（4 路并行、每模块 20 分钟上限）较慢，单独运行：
#   ls tests/motion_nav/test_*.py | xargs -P 4 -I{} sh "$HERE/shard_one.sh" "$PROJECT" {} > "$HERE/output/motion_nav_shards.txt"
