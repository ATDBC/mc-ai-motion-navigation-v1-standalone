#!/bin/sh
# 用法：
#   PROJECT=<origin/main dfaa0b4f 检出> BASE=<3889d10 检出> sh run_all.sh
# 只调用项目函数、项目夹具和项目测试；PROJECT 和 BASE 本身不改。
# E125 的对照树是临时 worktree，只把“只有行尾变化”的文件换回 BASE 的原字节，结束时删除。
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

# E125：行尾归因。对照树 = PROJECT 的内容，但把 BASE..PROJECT 之间只有行尾变化的文件全部换回 BASE 的原字节
# （忽略行尾时与 PROJECT 完全相同）。在两棵树上重跑全量分片里新报错的 7 个模块。
B=$(git -C "$BASE" rev-parse HEAD); P=$(git -C "$PROJECT" rev-parse HEAD)
SCRATCH=$(mktemp -d)
git -C "$PROJECT" worktree add --detach "$SCRATCH/e125" HEAD > /dev/null 2>&1
git -C "$PROJECT" diff --name-only "$B" "$P" | LC_ALL=C sort > "$SCRATCH/all.txt"
git -C "$PROJECT" diff --numstat --ignore-cr-at-eol "$B" "$P" | cut -f3 | LC_ALL=C sort > "$SCRATCH/content.txt"
LC_ALL=C comm -23 "$SCRATCH/all.txt" "$SCRATCH/content.txt" > "$SCRATCH/eol_only.txt"
while IFS= read -r f; do git -C "$PROJECT" show "$B:$f" > "$SCRATCH/e125/$f"; done < "$SCRATCH/eol_only.txt"
MODS="test_action_entry_handoff_repair test_f2rec_r0_baseline test_f2rec_r1_region_geometry test_reference_baselines
      test_action_continuity_solver_limits test_d058_runtime_validation test_navigation_session"
{
  echo "对照树换回原字节的文件数：$(wc -l < "$SCRATCH/eol_only.txt")；忽略行尾后对照树与 $(head_of "$PROJECT") 的差异行数：$(git -C "$SCRATCH/e125" diff --ignore-cr-at-eol | wc -l)"
  for tree in "$PROJECT" "$SCRATCH/e125"; do
    echo
    if [ "$tree" = "$PROJECT" ]; then echo "== $(head_of "$PROJECT") 原样"; else echo "== 对照树：只有行尾换回 $(head_of "$BASE")"; fi
    for m in $MODS; do
      printf '%s  ' "$m"
      (cd "$tree" && PYTHONPATH=. taskset -c 1 python3 -m unittest "tests.motion_nav.$m" 2>&1 | grep -E "^(OK|FAILED)" || true)
    done
  done
} > "$OUT/e125_line_ending_attribution.txt"
git -C "$PROJECT" worktree remove --force "$SCRATCH/e125"
rm -rf "$SCRATCH"

# E126：行尾之外的两处回归，在 BASE 与 PROJECT 上的错误信息
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
