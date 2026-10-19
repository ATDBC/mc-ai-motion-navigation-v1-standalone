#!/bin/sh
# 用法：
#   PROJECT=<origin/main 3889d10 检出> BASE=<dce230f 检出> sh run_all.sh
# 只调用项目函数、项目夹具、项目脚本和项目测试；本轮没有对照改动，PROJECT 和 BASE 本身不改。
# E115 复用 D117 审查目录的 e99_d117_repeat.py，E118 复用 D116 审查目录的 e93_momentum_gap_routes.py。
# 耗时与机器负载有关：每项都用 taskset 固定到单核，全量分片单独占一个核。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
R17="$HERE/../2026-10-18-d117-review"
R16="$HERE/../2026-10-18-d116-review"
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
run() { (cd "$1" && shift && PYTHONPATH=. taskset -c 0 python3 "$@"); }
head_of() { git -C "$1" rev-parse --short HEAD; }

# E112：证明字节构成；E117：D121 四格场景两条证明的正式消息
run "$PROJECT" "$HERE/e112_proof_size_breakdown.py" > "$OUT/e112_proof_size_breakdown.json"
run "$PROJECT" "$HERE/e117_momentum_chain_message_size.py" > "$OUT/e117_momentum_chain_message_size.json"

# E113：项目自己的 S2 止损探针（拒绝覆盖已有输出，所以先删除）
rm -f "$OUT/e113_s2_stop_probe_linux.json"
(cd "$PROJECT" && taskset -c 0 python3 scripts/d121_joint_worker_stop_probe.py \
  --output "$OUT/e113_s2_stop_probe_linux.json" > /dev/null)

# E114、E116：两个提交对照
for tree in "$BASE" "$PROJECT"; do
  echo "== $(head_of "$tree")"
  run "$tree" "$HERE/e114_existing_wire_size.py"
done > "$OUT/e114_existing_wire_size.txt" 2>&1
for tree in "$BASE" "$PROJECT"; do
  echo "== $(head_of "$tree")，CPU 0"
  run "$tree" "$HERE/e116_solve_cost.py" 15
done > "$OUT/e116_solve_cost.txt" 2>&1

# E115：D117 代表链与回退时序
{
  echo "== $(head_of "$PROJECT")，空闲单核 CPU 0（复用 D117 审查的 e99 脚本）"
  run "$PROJECT" "$R17/e99_d117_repeat.py" normal 10
  for mode in busy_5 prepared_always_busy always_busy; do
    echo; echo "== $(head_of "$PROJECT")，CPU 0，快照注入 $mode"
    run "$PROJECT" "$R17/e99_d117_repeat.py" $mode 3
  done
  echo
  for tree in "$BASE" "$PROJECT"; do
    echo "== $(head_of "$tree")，CPU 0，prepared_always_busy"
    run "$tree" "$R17/e99_d117_repeat.py" prepared_always_busy 8
  done
  echo
  for tree in "$BASE" "$PROJECT"; do
    for mode in prepared_always_busy always_busy; do
      echo "== $(head_of "$tree")，CPU 1，$mode"
      (cd "$tree" && PYTHONPATH=. taskset -c 1 python3 "$R17/e99_d117_repeat.py" $mode 20)
    done
  done
} > "$OUT/e115_d117_chain_regression.txt" 2>&1

# E118：现行生产链在 1-1-1、1-1-4 上
{
  echo "== $(head_of "$PROJECT") 现行生产链，第一段间隙 1、中间平台 1、第二段间隙 W（复用 D116 审查的 e93 脚本）"
  run "$PROJECT" "$R16/e93_momentum_gap_routes.py" production 1 4 2>&1
} > "$OUT/e118_production_courses.txt"

# E120：D120 四方向离线扫描
run "$PROJECT" "$HERE/e120_d120_offline_scan.py" > "$OUT/e120_d120_offline_scan.json"

# E121：D118–D121 直接相关的项目测试
T="tests.motion_nav.test_d121_momentum_handoff tests.motion_nav.test_d121_joint_worker_stop tests.motion_nav.test_d120_momentum_entry_calibration tests.motion_nav.test_d118_momentum_calibration tests.motion_nav.test_b09r_physics_step"
{
  echo "== $(head_of "$PROJECT")，CPU 2：python3 -m unittest $T"
  (cd "$PROJECT" && taskset -c 2 python3 -m unittest $T 2>&1 | grep -E "^(Ran|OK|FAILED|FAIL:|ERROR:)" || true)
} > "$OUT/e121_d118_d121_tests.txt"

# E122：设计度量测试，原样与对照树（临时 worktree，结束时删除）
SCRATCH=$(mktemp -d)
git -C "$PROJECT" worktree add --detach "$SCRATCH/e122" HEAD > /dev/null 2>&1
git -C "$SCRATCH/e122" apply "$HERE/e122_classify_fallback_enums_control.diff"
M=tests.motion_nav.test_navigation_design_metrics
{
  echo "== $(head_of "$PROJECT") 原样"
  (cd "$PROJECT" && taskset -c 2 python3 -m unittest $M 2>&1 | grep -E "^(Ran|OK|FAILED|FAIL:|ERROR:)" || true)
  echo; echo "== 对照树（e122：脚本和测试各登记 FallbackTerminalMode、FallbackYawMode 为非实现选择）"
  (cd "$SCRATCH/e122" && taskset -c 2 python3 -m unittest $M 2>&1 | grep -E "^(Ran|OK|FAILED|FAIL:|ERROR:)" || true)
} > "$OUT/e122_design_metrics.txt"
git -C "$PROJECT" worktree remove --force "$SCRATCH/e122"
rmdir "$SCRATCH"

# E119：行尾（PROJECT 仓库里需要同时有 dce230f 和 3889d10 两个提交）
{
  echo "dce230f..3889d10 代码、测试、脚本、配置的 diff 行数：原样 vs 忽略行尾（git diff --stat --ignore-cr-at-eol）"
  git -C "$PROJECT" diff --stat=200 dce230f 3889d10 -- mc2p scripts tests config > "$OUT/e119_full.tmp"
  git -C "$PROJECT" diff --stat=200 --ignore-cr-at-eol dce230f 3889d10 -- mc2p scripts tests config > "$OUT/e119_nocr.tmp"
  paste -d'|' "$OUT/e119_full.tmp" "$OUT/e119_nocr.tmp" | sed 's/  */ /g; s/[+-]\{3,\}//g; s/ *$//'
  echo; echo "整文件 CRLF→LF 的文件（CRLF 行数／总行数）"
  CR=$(printf '\r')
  for f in mc2p/motion_nav/motion_solver.py mc2p/motion_nav/physics_1_21.py \
      mc2p/motion_nav/prepared_motion_proof.py tests/motion_nav/test_b09r_physics_step.py \
      tests/motion_nav/test_d102_registry_lifecycle.py tests/motion_nav/test_motion_baseline_recovery.py \
      tests/test_fabric_deployment_probe.py; do
    old=$(git -C "$PROJECT" show dce230f:$f); new=$(git -C "$PROJECT" show 3889d10:$f)
    echo "$f dce230f $(printf '%s\n' "$old" | grep -c "$CR\$")/$(printf '%s\n' "$old" | wc -l)" \
      "-> 3889d10 $(printf '%s\n' "$new" | grep -c "$CR\$")/$(printf '%s\n' "$new" | wc -l)"
  done
} > "$OUT/e119_line_endings.txt"
rm -f "$OUT/e119_full.tmp" "$OUT/e119_nocr.tmp"

# 项目自己的分片器，完整 tests/motion_nav（Linux 单核约 1 小时，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
