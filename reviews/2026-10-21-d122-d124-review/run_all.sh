#!/bin/sh
# 用法：
#   PROJECT=<origin/main fdf22ff9 检出> BASE=<dfaa0b4f 检出> OLDBASE=<3889d10 检出> sh run_all.sh
# 只调用项目函数、项目夹具和项目测试；PROJECT、BASE、OLDBASE 本身不改。
# E131 的对照树是临时 worktree，只应用 e131_optional_abandon_point_control.diff（5 行），结束时删除。
# E129 需要 Gson 2.10.1（与项目 Windows 夹具同一版本）事先放在
#   $PROJECT/.gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1/<任意目录>/gson-2.10.1.jar
# 每项都用 taskset 固定到单核，全量分片单独占一个核。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/output"
PREV="$HERE/../2026-10-20-d121-transport-review"
PREV2="$HERE/../2026-10-19-d121-review"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
head_of() { git -C "$1" rev-parse --short HEAD; }

# E127：剩下 3 个必要格从哪些站位有视线（必要条件；乐观上界）
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e127_lower_cell_sight_lines.py") \
  > "$OUT/e127_lower_cell_sight_lines.json"

# E128：3 个必要格里放不同原版内容，父动作、后继和行为签名是否变化
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e128_necessary_cell_contents.py") \
  > "$OUT/e128_necessary_cell_contents.json"
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e128b_z2_exit_tail.py") \
  > "$OUT/e128b_z2_exit_tail.json"

# E129：项目 D122 Java 门禁测试在 Linux 上运行（只替换 javac/java 路径和 classpath 分隔符）
{ (cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e129_java_gates_linux.py" 2>&1 \
    | grep -v JAVA_TOOL_OPTIONS) && echo "exit=0" || echo "exit=$?"
  find "$PROJECT/.gradle/caches/modules-2/files-2.1/com.google.code.gson/gson/2.10.1" -name gson-2.10.1.jar \
    -exec sha256sum {} \; | sed 's#  .*/#  #'
} > "$OUT/e129_java_gates_linux.txt"

# E130：预排默认执行途中被一条普通 neutral 请求清空时，D120 的 972 个入口落在哪里
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e130_default_clear_by_plain_request.py") \
  > "$OUT/e130_default_clear_by_plain_request.json"

# E131：(0,62,0) 未知时父动作 NEEDS_WORLD 是否只来自可选放弃点；原样树与 5 行对照树
SCRATCH=$(mktemp -d)
git -C "$PROJECT" worktree add --detach "$SCRATCH/e131" HEAD > /dev/null 2>&1
git -C "$SCRATCH/e131" apply "$HERE/e131_optional_abandon_point_control.diff"
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e131_optional_abandon_point.py") \
  > "$OUT/e131_optional_abandon_point_project.json"
(cd "$SCRATCH/e131" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e131_optional_abandon_point.py") \
  > "$OUT/e131_optional_abandon_point_control.json"
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e131b_failing_boundary.py") \
  > "$OUT/e131b_failing_boundary.json"
MODS="test_d123_dependency_probe test_d123_dependency_gate_repair test_d123_actor_calibration
      test_d124_proof_dependency_projection test_d121_momentum_handoff test_d121_successor_binding
      test_action_continuity_solver_limits test_b10_gap_solver"
for tree in "$PROJECT" "$SCRATCH/e131"; do
  if [ "$tree" = "$PROJECT" ]; then echo "== $(head_of "$PROJECT") 原样"; else echo "== 对照树：$(head_of "$PROJECT") + e131 diff"; fi
  for m in $MODS; do
    printf '%s  ' "$m"
    (cd "$tree" && PYTHONPATH=. taskset -c 1 python3 -m unittest "tests.motion_nav.$m" 2>&1 \
      | grep -E "^(OK|FAILED)|^(FAIL|ERROR): " | tr '\n' ' ' || true)
    echo
  done
done | sed 's/[[:space:]]*$//' > "$OUT/e131_adjacent_tests.txt"
git -C "$PROJECT" worktree remove --force "$SCRATCH/e131"
rm -rf "$SCRATCH"

# E132：test_continuous_descent 里被判 EXPIRED 的求解任务耗时（单核，各 3 次）与函数分布；
# 另用上一轮 E116 基准比较单格跨隙求解
for tree in "$BASE" "$PROJECT"; do
  for i in 1 2 3; do
    echo "$(head_of "$tree") time run$i $(cd "$tree" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e132_descent_job_cost.py" time)"
  done
  echo "$(head_of "$tree") profile $(cd "$tree" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e132_descent_job_cost.py" profile)"
  echo "$(head_of "$tree") e116 $(cd "$tree" && PYTHONPATH=. taskset -c 1 python3 "$PREV2/e116_solve_cost.py" 15 | tr -d '\n ')"
done > "$OUT/e132_descent_job_cost.txt"

# E133：分片里失败的另一条墙钟测试，单独运行（单核，各 3 次）
T=tests.motion_nav.test_b10_motion_candidate.VerifiedMotionRouteIntegrationTests.test_coordinator_aligns_a_safe_gap_entry_before_background_solving
for tree in "$BASE" "$PROJECT"; do
  for i in 1 2 3; do
    echo "$(head_of "$tree") run$i $(cd "$tree" && PYTHONPATH=. taskset -c 1 python3 -m unittest "$T" 2>&1 \
      | grep -E '^(OK|FAILED)|^Ran' | tr '\n' ' ')"
  done
done | sed 's/[[:space:]]*$//' > "$OUT/e133_b10_candidate_wall_clock.txt"

# E134：相对 OLDBASE（D121 修复声称的字节基线 3889d10）仍只有行尾变化的文件
O=$(git -C "$OLDBASE" rev-parse HEAD)
SCRATCH=$(mktemp -d)
{
  for tree in "$BASE" "$PROJECT"; do
    P=$(git -C "$tree" rev-parse HEAD)
    git -C "$PROJECT" diff --name-only "$O" "$P" | LC_ALL=C sort > "$SCRATCH/all.txt"
    git -C "$PROJECT" diff --numstat --ignore-cr-at-eol "$O" "$P" | cut -f3 | LC_ALL=C sort > "$SCRATCH/content.txt"
    LC_ALL=C comm -23 "$SCRATCH/all.txt" "$SCRATCH/content.txt" > "$SCRATCH/eol-$(head_of "$tree").txt"
    echo "$(head_of "$tree") 相对 $(head_of "$OLDBASE")：变化文件 $(wc -l < "$SCRATCH/all.txt")，其中只有行尾变化 $(wc -l < "$SCRATCH/eol-$(head_of "$tree").txt")"
  done
  B=$(head_of "$BASE"); P=$(head_of "$PROJECT")
  echo "两轮都只有行尾变化：$(LC_ALL=C comm -12 "$SCRATCH/eol-$B.txt" "$SCRATCH/eol-$P.txt" | wc -l)"
  echo "$B 有、$P 不再是纯行尾变化：$(LC_ALL=C comm -23 "$SCRATCH/eol-$B.txt" "$SCRATCH/eol-$P.txt" | wc -l)"
  n=0
  for f in $(LC_ALL=C comm -23 "$SCRATCH/eol-$B.txt" "$SCRATCH/eol-$P.txt"); do
    if [ "$(git -C "$PROJECT" show "$O:$f" | sha256sum)" = "$(git -C "$PROJECT" show "$P:$f" | sha256sum)" ]; then
      echo "  恢复为 $(head_of "$OLDBASE") 原字节：$f"; n=$((n + 1))
    fi
  done
  echo "  其余 $(( $(LC_ALL=C comm -23 "$SCRATCH/eol-$B.txt" "$SCRATCH/eol-$P.txt" | wc -l) - n )) 个本轮有真实内容修改"
  echo "仍为纯行尾变化的文件中，$P 与 $B 字节不同的：$(git -C "$PROJECT" diff --name-only "$B" "$P" | LC_ALL=C sort | LC_ALL=C comm -12 - "$SCRATCH/eol-$P.txt" | wc -l)"
  echo "按顶层目录："
  cut -d/ -f1 "$SCRATCH/eol-$P.txt" | sort | uniq -c | sort -rn
  echo "$P EXPORT-METADATA.json："
  git -C "$PROJECT" show "$P:EXPORT-METADATA.json"
} > "$OUT/e134_byte_base_residue.txt"
rm -rf "$SCRATCH"

# E124／E124b（上一轮脚本）：OLDBASE → PROJECT，证据里的“路径 → sha256”记录
for tree in "$OLDBASE" "$PROJECT"; do
  echo "== $(head_of "$tree")"
  taskset -c 0 python3 "$PREV/e124_evidence_hash_references.py" "$tree"
done > "$OUT/e124_evidence_hash_references.txt"
taskset -c 0 python3 "$PREV/e124b_evidence_reference_diff.py" "$OLDBASE" "$PROJECT" \
  > "$OUT/e124b_evidence_reference_diff.json"

# 项目自己的分片器，完整 tests/motion_nav（Linux 单核约 70 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
