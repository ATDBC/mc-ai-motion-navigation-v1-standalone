#!/bin/sh
# 用法：
#   PROJECT=<origin/main 54a904e 检出> sh run_all.sh
# 只调用项目函数、项目夹具和项目测试。对照实验（E64、E65、E67）在 PROJECT 之外新建临时
# git worktree，应用本目录的 .diff 后运行，结束时删除；PROJECT 本身不改。
# E65 复用 D110 审查目录里的 e53_repeat_project_tests.py。
# 输出写到本目录的 output/。计时无关，但 E65 的竞态需要单核（taskset -c 1）才容易出现。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
R10="$HERE/../2026-10-15-d110-review"
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
T="tests.motion_nav.test_d112_worker_degradation tests.motion_nav.test_d112_delivery_baseline tests.test_b10_runtime_probe tests.motion_nav.test_action_continuity_formal tests.motion_nav.test_action_continuity_solver_limits tests.motion_nav.test_motion_start_delivery tests.motion_nav.test_route_body_advance tests.motion_nav.test_d108_receiver_tick_delivery tests.motion_nav.test_b10_motion_candidate tests.motion_nav.test_air_transition_solver tests.test_fabric_deployment_probe"
H=tests.motion_nav.test_d102_action_handoff.ActionHandoffTests.test_old_permission_cannot_prepare_new_generation

# E62：组件“提交→可见”延迟 2..7 的终态（复现 Fabric 5-tick 失败）
(cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e62_delivery_tick_boundary.py" 2 3 4 5 6 7) \
  > "$OUT/e62_delivery_tick_boundary.txt" 2>&1
# E63：控制准备尾部与同步探针求解对账（只读公开证据）
python3 "$HERE/e63_control_prepare_tail.py" "$PROJECT" > "$OUT/e63_control_prepare_tail.txt" 2>&1
# E66：交付抖动一 tick
(cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e66_delivery_jitter.py" 20) \
  > "$OUT/e66_delivery_jitter.txt" 2>&1

SCRATCH=$(mktemp -d)
for pair in cap6:e64_prefix_cap6_control.diff samp:e67_revalidation_raises_delivery_control.diff \
            fix:e65_handoff_barrier_test_proposal.diff diag:e65_handoff_barrier_diagnostic.diff; do
  tree="$SCRATCH/${pair%%:*}"
  git -C "$PROJECT" worktree add --detach "$tree" HEAD > /dev/null 2>&1
  # 目标文件多为 CRLF；diff 以 LF 保存，用 --ignore-whitespace 应用。
  git -C "$tree" apply --ignore-whitespace "$HERE/${pair#*:}"
done
# E64：准备前缀上限 5→6 的对照
{
  echo "== 对照树：origin/main + e64_prefix_cap6_control.diff（准备前缀上限 5→6，只在临时工作树）"
  (cd "$SCRATCH/cap6" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e62_delivery_tick_boundary.py" 5 6 7)
  echo; echo "== 同一组 11 个直接与相邻测试模块（221 项）"; echo "-- 对照树"
  (cd "$SCRATCH/cap6" && taskset -c 1,2 python3 -m unittest $T 2>&1 | grep -E "^(FAIL|ERROR):|^Ran|^OK|^FAILED|^AssertionError|ContractViolation not raised" || true)
  echo "-- origin/main"
  (cd "$PROJECT" && taskset -c 1,2 python3 -m unittest $T 2>&1 | grep -E "^(FAIL|ERROR):|^Ran|^OK|^FAILED" || true)
} > "$OUT/e64_prefix_cap6_control.txt" 2>&1
# E67：revalidation 结果只允许把交付估计往上调
{
  echo "== 对照树：origin/main + e67_revalidation_raises_delivery_control.diff（只在临时工作树）"
  echo "-- E66 抖动"; (cd "$SCRATCH/samp" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e66_delivery_jitter.py" 20)
  echo; echo "-- E62 固定延迟边界"; (cd "$SCRATCH/samp" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e62_delivery_tick_boundary.py" 4 5 6)
  echo; echo "-- 同一组 11 个直接与相邻测试模块"
  (cd "$SCRATCH/samp" && taskset -c 1,2 python3 -m unittest $T 2>&1 | grep -E "^(FAIL|ERROR):|^Ran|^OK|^FAILED" || true)
} > "$OUT/e67_revalidation_raises_delivery_control.txt" 2>&1
# E65：D102 接收屏障竞态（原样、诊断、修正）
{
  echo "== 原样"; taskset -c 1 python3 "$R10/e53_repeat_project_tests.py" "$PROJECT" "$H" 30
  echo "== 诊断变体"; taskset -c 1 python3 "$R10/e53_repeat_project_tests.py" "$SCRATCH/diag" "$H" 40
  echo "== 测试侧修正"; taskset -c 1 python3 "$R10/e53_repeat_project_tests.py" "$SCRATCH/fix" "$H" 30
} > "$OUT/e65_handoff_barrier_sibling.txt" 2>&1
for tree in cap6 samp fix diag; do git -C "$PROJECT" worktree remove --force "$SCRATCH/$tree"; done
rmdir "$SCRATCH"
# 项目自己的分片器，完整 tests/motion_nav（Linux 单核约 30 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
