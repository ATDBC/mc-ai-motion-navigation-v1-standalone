#!/bin/sh
# 用法：
#   PROJECT=<origin/main dfaa0b4f 检出> BASE=<3889d10 检出> sh run_all.sh
# 只调用项目函数、项目夹具和项目测试；PROJECT 和 BASE 本身不改。
# E125 的对照树是临时 worktree，只把一个冻结清单换回 BASE 的原字节，结束时删除。
# 每项都用 taskset 固定到单核，全量分片单独占一个核。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
head_of() { git -C "$1" rev-parse --short HEAD; }

# E123：L+1 实际采样的输入决定迟到后的身体结果（D120 冻结的 972 个落地入口）
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e123_late_tick_physics.py") \
  > "$OUT/e123_late_tick_physics.json"

# E124：证据里的“路径 → sha256”记录在两个提交上各有多少还对得上
for tree in "$BASE" "$PROJECT"; do
  echo "== $(head_of "$tree")"
  taskset -c 0 python3 "$HERE/e124_evidence_hash_references.py" "$tree"
done > "$OUT/e124_evidence_hash_references.txt"

# E124b：同一条记录在行尾统一前后的变化，以及证据引用证据的逐条状态
taskset -c 0 python3 "$HERE/e124b_evidence_reference_diff.py" "$BASE" "$PROJECT" \
  > "$OUT/e124b_evidence_reference_diff.json"

# E125：冻结的 v8 清单。原样 vs 只把它换回 BASE 的原字节（CRLF）
M=tests/sim/manifests/navigation-product-r28-v8.json
T=tests.motion_nav.test_action_entry_handoff_repair
SCRATCH=$(mktemp -d)
git -C "$PROJECT" worktree add --detach "$SCRATCH/e125" HEAD > /dev/null 2>&1
git -C "$BASE" show "HEAD:$M" > "$SCRATCH/e125/$M"
{
  echo "== $(head_of "$PROJECT") 原样"
  (cd "$PROJECT" && taskset -c 1 python3 -m unittest $T 2>&1 | grep -E "^(Ran|OK|FAILED)" || true)
  echo
  echo "== 对照树：只把 $M 换回 CRLF"
  (cd "$SCRATCH/e125" && taskset -c 1 python3 -m unittest $T 2>&1 | grep -E "^(Ran|OK|FAILED)" || true)
} > "$OUT/e125_frozen_v8_manifest.txt"
git -C "$PROJECT" worktree remove --force "$SCRATCH/e125"
rmdir "$SCRATCH"

# E126：另外两个在 BASE 通过、在 PROJECT 报错的模块
for tree in "$BASE" "$PROJECT"; do
  for m in test_action_continuity_solver_limits test_d058_runtime_validation; do
    echo "== $(head_of "$tree") tests.motion_nav.$m"
    (cd "$tree" && PYTHONPATH=. taskset -c 1 python3 -m unittest "tests.motion_nav.$m" 2>&1 \
      | grep -E "^(Ran|OK|FAILED|FAIL:|ERROR:)|^AttributeError|^mc2p.contracts.common.ContractViolation" || true)
  done
done > "$OUT/e126_suite_regressions.txt"

# 项目自己的分片器，完整 tests/motion_nav（Linux 单核约 1 小时，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
