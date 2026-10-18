#!/bin/sh
# 用法：
#   PROJECT=<origin/main dce230f 检出> sh run_all.sh
# 只调用项目函数、项目夹具和项目测试；本轮没有对照改动，PROJECT 本身不改。
# E101 复用 D116 审查目录的 e93_momentum_gap_routes.py，E104 复用 D110 审查目录的
# e53_repeat_project_tests.py。
# 重复运行的分布与机器负载有关：下面用“空闲单核”和“同核另有忙循环”两种明确条件。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
R10="$HERE/../2026-10-15-d110-review"
R16="$HERE/../2026-10-18-d116-review"
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
run() { (cd "$PROJECT" && PYTHONPATH=. python3 "$@"); }

# E98：连续跨隙的物理可行性（助跑 8 格；目标高度 DY=-1/0/+1；第二段间隙 1–5 格）
{
  echo "E98 原始输出（修正着地判定后；助跑 8 格；DY=-1/0/+1；W2=1..5）"
  run "$HERE/e98_momentum_feasibility.py" 8 -1,0,1 1,2,3,4,5
} > "$OUT/e98_momentum_feasibility.jsonl"
{
  echo "E98 整理（修正着地判定后）"
  python3 "$HERE/e98_format.py" "$OUT/e98_momentum_feasibility.jsonl"
  echo; echo "== 静止不可过、带速度可过的地形"
  python3 "$HERE/e98_format.py" --only-momentum "$OUT/e98_momentum_feasibility.jsonl"
} > "$OUT/e98_momentum_feasibility_summary.txt"

# E99：D117 代表链与快照注入（空闲单核 CPU 0；之后在 CPU 1 上另起忙循环）
{
  echo "== dce230f，空闲单核 CPU 0"
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e99_d117_repeat.py" normal 30)
  for mode in busy_5 prepared_always_busy always_busy; do
    echo; echo "== dce230f，空闲单核 CPU 0，快照注入 $mode"
    (cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e99_d117_repeat.py" $mode 5)
  done
} > "$OUT/e99_d117_repeat.txt" 2>&1
taskset -c 1 sh -c 'while :; do :; done' &
HOG=$!
{
  echo; echo "== dce230f，CPU 1 同核另有忙循环"
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e99_d117_repeat.py" normal 30)
} >> "$OUT/e99_d117_repeat.txt" 2>&1

# E104：D116 时间线测试与 D117 监视器测试的重复（先在忙循环所在的 CPU 1，再在空闲的 CPU 0）
T1=tests.motion_nav.test_d116_handoff_timeline
T2=tests.motion_nav.test_d117_prepared_liveness_monitor
{
  echo "== dce230f，同核另有忙循环（CPU 1）"
  taskset -c 1 python3 "$R10/e53_repeat_project_tests.py" "$PROJECT" $T1 20 $T2 10
} > "$OUT/e104_hog.tmp" 2>&1
kill "$HOG" 2> /dev/null || true
{
  echo "== dce230f，空闲单核（CPU 0）"
  taskset -c 0 python3 "$R10/e53_repeat_project_tests.py" "$PROJECT" $T1 20 $T2 10
  cat "$OUT/e104_hog.tmp"
} > "$OUT/e104_d117_test_repeat.txt" 2>&1
rm -f "$OUT/e104_hog.tmp"

# E100：可接续的第一跳落点窗口、不潜行滑落、潜行停稳
{
  echo "E100（修正着地判定后；每行一个地形；落点为身体中心 z）"
  run "$HERE/e100_landing_window.py"
} > "$OUT/e100_landing_window.jsonl"

# E101：现行生产链在 1-1-W 地形上的结果
{
  echo "== dce230f 现行生产链，第一段间隙 1、中间平台 1、第二段间隙 W（复用 D116 审查的 e93 脚本）"
  run "$R16/e93_momentum_gap_routes.py" production 1 3 4 2>&1
} > "$OUT/e101_production_courses.txt"

# E102：代表链交接前后的速度与输入
run "$HERE/e102_handoff_speed.py" > "$OUT/e102_handoff_speed.txt" 2>&1

# E103、E105–E111：计算器上的时序、平台长度、单跳极限、侧移、静止起点、容差与回退
run "$HERE/e103_jump_hold_timing.py" > "$OUT/e103_jump_hold_timing.txt"
run "$HERE/e105_platform_length.py" > "$OUT/e105_platform_length.txt"
run "$HERE/e106_single_jump_limit.py" > "$OUT/e106_single_jump_limit.txt"
run "$HERE/e107_strafe45_sensitivity.py" > "$OUT/e107_strafe45_sensitivity.txt"
run "$HERE/e108_standstill_band.py" > "$OUT/e108_standstill_band.txt"
run "$HERE/e109_timing_tolerance.py" > "$OUT/e109_timing_tolerance.txt"
run "$HERE/e110_ground_jump_tolerance.py" > "$OUT/e110_ground_jump_tolerance.txt"
run "$HERE/e111_sneak_fallback_timing.py" > "$OUT/e111_sneak_fallback_timing.txt"

# 项目自己的分片器，完整 tests/motion_nav（Linux 单核约 40 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
