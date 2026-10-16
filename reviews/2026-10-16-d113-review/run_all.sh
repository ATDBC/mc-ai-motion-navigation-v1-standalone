#!/bin/sh
# 用法：
#   PROJECT=<origin/main 81367da 检出> D112=<54a904e 检出> sh run_all.sh
# 只调用项目函数、项目夹具和项目测试，或只读项目公开的原始证据。E69 的对照改动在 PROJECT 之外
# 新建临时 git worktree 应用，结束时删除；PROJECT 和 D112 检出本身不改。
# E71 复用 D110 审查目录的 e53_repeat_project_tests.py；E72 复用 D112 审查目录的 E62、E66。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
R10="$HERE/../2026-10-15-d110-review"
R12="$HERE/../2026-10-16-d112-review"
OUT="$HERE/output"
EVIDENCE="$PROJECT/evidence/motion_navigation/d113-control-visible-v1"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"

# E68：10-tick 超时的直接原因（只读 D113 公开原始 trace）
{
  python3 "$HERE/e68_stale_intent_deadline.py" "$EVIDENCE/formal-default/raw"
  python3 "$HERE/e68_stale_intent_deadline.py" "$EVIDENCE/targeted-5tick/raw"
} > "$OUT/e68_stale_intent_deadline.txt" 2>&1

# E69：生产驱动撤销旧移动租约这一步的作用（对照只去掉那一行）
SCRATCH=$(mktemp -d)
git -C "$PROJECT" worktree add --detach "$SCRATCH/noguard" HEAD > /dev/null 2>&1
# 目标文件为 CRLF；diff 以 LF 保存，用 --ignore-whitespace 应用。
git -C "$SCRATCH/noguard" apply --ignore-whitespace "$HERE/e69_no_lease_revoke_control.diff"
{
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e69_driver_lease_guard.py")
  (cd "$SCRATCH/noguard" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e69_driver_lease_guard.py")
} > "$OUT/e69_driver_lease_guard.txt" 2>&1
git -C "$PROJECT" worktree remove --force "$SCRATCH/noguard"
rmdir "$SCRATCH"

# E70：交付估计在同一路线内只增不减的代价（D112 与 D113 对比）
{
  (cd "$D112" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e70_sticky_delivery_estimate.py")
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e70_sticky_delivery_estimate.py")
} > "$OUT/e70_sticky_delivery_estimate.txt" 2>&1

# E71：D102 接收屏障修正（单核重复）
taskset -c 1 python3 "$R10/e53_repeat_project_tests.py" "$PROJECT" \
  tests.motion_nav.test_d102_action_handoff.ActionHandoffTests.test_old_permission_cannot_prepare_new_generation 30 \
  > "$OUT/e71_d102_barrier_repeat.txt" 2>&1

# E72：D112 审查的抖动与固定延迟边界实验在 D113 上重跑
{
  echo "== D112 审查 E66（{D,D+1} 抖动）在 PROJECT 上重跑"
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$R12/e66_delivery_jitter.py" 20)
  echo; echo "== D112 审查 E62（固定延迟边界）在 PROJECT 上重跑"
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$R12/e62_delivery_tick_boundary.py" 4 5 6 7)
} > "$OUT/e72_d112_experiments_on_d113.txt" 2>&1

# E73：“新的 SOLVE 重新建立估计，REVALIDATE 只上调”对照；E74：D102 接收周期竞态（同核争用）
SCRATCH=$(mktemp -d)
for pair in reest:e73_solve_reestablishes_control.diff barrier:e74_receiver_serialize_barrier_test_proposal.diff; do
  tree="$SCRATCH/${pair%%:*}"
  git -C "$PROJECT" worktree add --detach "$tree" HEAD > /dev/null 2>&1
  git -C "$tree" apply --ignore-whitespace "$HERE/${pair#*:}"
done
T="tests.motion_nav.test_d113_delivery_adaptation tests.motion_nav.test_d113_delivery_metrics tests.motion_nav.test_d113_targeted_probe tests.motion_nav.test_d112_worker_degradation tests.motion_nav.test_d112_delivery_baseline tests.test_b10_runtime_probe tests.motion_nav.test_action_continuity_formal tests.motion_nav.test_action_continuity_solver_limits tests.motion_nav.test_motion_start_delivery tests.motion_nav.test_route_body_advance tests.motion_nav.test_d108_receiver_tick_delivery tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_air_transition_solver tests.test_fabric_deployment_probe tests.motion_nav.test_d102_action_handoff"
{
  echo "== 对照树：PROJECT + e73_solve_reestablishes_control.diff"
  echo "-- E70 多动作路线"; (cd "$SCRATCH/reest" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e70_sticky_delivery_estimate.py")
  echo; echo "-- D112 审查 E66 抖动"; (cd "$SCRATCH/reest" && PYTHONPATH=. taskset -c 1,2 python3 "$R12/e66_delivery_jitter.py" 20)
  echo; echo "-- D112 审查 E62 固定延迟边界"; (cd "$SCRATCH/reest" && PYTHONPATH=. taskset -c 1,2 python3 "$R12/e62_delivery_tick_boundary.py" 4 5 6 7)
  echo; echo "-- D113 新增与相邻 15 个模块"
  (cd "$SCRATCH/reest" && taskset -c 1,2 python3 -m unittest $T 2>&1 | grep -E "^(FAIL|ERROR):|^Ran|^OK|^FAILED|^AssertionError" || true)
} > "$OUT/e73_solve_reestablishes_control.txt" 2>&1
H=tests.motion_nav.test_d102_action_handoff.ActionHandoffTests.test_parent_request_building_stays_on_existing_receiver
taskset -c 1 timeout 3000 python3 -c "while True: pass" > /dev/null 2>&1 &
HOG=$!
{
  echo "== 原样（同核另有忙循环）"; taskset -c 1 python3 "$R10/e53_repeat_project_tests.py" "$PROJECT" "$H" 40
  echo "== 测试侧修正（同核另有忙循环）"; taskset -c 1 python3 "$R10/e53_repeat_project_tests.py" "$SCRATCH/barrier" "$H" 40
} > "$OUT/e74_receiver_serialize_barrier.txt" 2>&1
kill "$HOG" 2> /dev/null || true
for tree in reest barrier; do git -C "$PROJECT" worktree remove --force "$SCRATCH/$tree"; done
rmdir "$SCRATCH"

# 项目自己的分片器，完整 tests/motion_nav（Linux 单核约 30 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
