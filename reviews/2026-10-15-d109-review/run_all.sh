#!/bin/sh
# 用法：
#   PROJECT=<origin/main c2cc1ad 检出> D108=<a9d51a2 检出> \
#   CONTROL=<另一份干净的 c2cc1ad 检出，只用于对照改动> sh run_all.sh
# 只调用项目函数、项目脚本和项目测试并计时，不改 PROJECT／D108 检出。
# CONTROL 会被临时打上本目录的对照 diff，结束时恢复。输出写到本目录的 output/。
# 部分实验复用 D108 审查目录里的脚本（E36、E39、E39b、E40）。
# E48 必须在后台运行或在不含 "time.sleep(30)" 字样的 shell 中运行：它会强杀残留的场景进程。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
R8="$HERE/../2026-10-15-d108-review"
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
# E36：快照差分（D108 与 D109 的全部可观察结果应逐字节一致）
WORK=$(mktemp -d)
(cd "$WORK" && {
  PYTHONPATH="$D108" taskset -c 2 python3 "$R8/e36_snapshot_differential_d108.py"
  PYTHONPATH="$PROJECT" taskset -c 2 python3 "$R8/e36_snapshot_differential_d108.py"
} > "$OUT/e36_snapshot_differential.txt" 2>&1)
rm -rf "$WORK"
# E39：POSIX Queue 生命周期矩阵，5 场景 × 20 次 × 3 轮
: > "$OUT/e39_linux_worker_lifecycle.txt"
for round in 1 2 3; do
  echo "（第 $round 轮）" >> "$OUT/e39_linux_worker_lifecycle.txt"
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 2 python3 "$R8/e39_linux_worker_lifecycle.py" >> "$OUT/e39_linux_worker_lifecycle.txt" 2>&1)
done
# E39b：关闭自动 GC 后的 fd 增量（D7）
: > "$OUT/e39b_worker_reference_cycle.txt"
for tree in "$D108" "$PROJECT"; do
  (cd "$tree" && PYTHONPATH=. taskset -c 2 python3 "$R8/e39b_worker_reference_cycle.py" >> "$OUT/e39b_worker_reference_cycle.txt" 2>/dev/null)
done
# E40：D108 审查的分片器对抗场景
taskset -c 1 python3 "$R8/e40_shard_runner_adversarial.py" "$PROJECT" > "$OUT/e40_shard_runner_adversarial.txt" 2>&1
# E45：close_release 诊断行的竞态（D109 两种 CPU 绑定；D108 复现 D1 的另一种表现）
{
  echo "== c2cc1ad，taskset -c 1,2（两个 CPU）"; (cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e45_close_diagnostics_race.py" 50)
  echo "== c2cc1ad，taskset -c 1（单 CPU）"; (cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e45_close_diagnostics_race.py" 50)
  echo "== a9d51a2（D1 所用生产代码），taskset -c 1,2"; (cd "$D108" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e45_close_diagnostics_race.py" 20)
} > "$OUT/e45_close_diagnostics_race.txt" 2>&1
# E46：求解耗时与同进程后台负载（墙钟与线程 CPU 时间）
(cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e46_solver_tail_inprocess_load.py" 150 > "$OUT/e46_solver_tail_inprocess_load.txt" 2>&1)
# E47：D3 zombie 测试 30 次、D5 Linux 矩阵 10 次
taskset -c 1,2 python3 "$HERE/e47_repeat_project_tests.py" "$PROJECT" \
  test_timeout_kills_descendant_holding_output_pipe_with_bounded_cleanup 30 \
  test_linux_subreaper_adversarial_matrix 10 > "$OUT/e47_repeat_project_tests.txt" 2>&1
# E48／E48b：Linux 矩阵逐子场景；fork_during_cleanup 直接交给分片器
git -C "$CONTROL" diff --quiet
git -C "$CONTROL" apply "$HERE/e48_settle_sigkill_proposal.diff"
(cd "$(mktemp -d)" && taskset -c 1 python3 "$HERE/e48_subreaper_matrix_subtests.py" "$PROJECT" "$CONTROL" 10 > "$OUT/e48_subreaper_matrix_subtests.txt" 2>&1)
taskset -c 1 python3 "$HERE/e48b_fork_during_cleanup.py" "$PROJECT" "$CONTROL" 5 > "$OUT/e48b_fork_during_cleanup.txt" 2>&1
git -C "$CONTROL" checkout -- scripts/run_motion_nav_shards.py
# E49：异步 trace 的有界关闭与退出
taskset -c 2 python3 "$HERE/e49_trace_writer_exit.py" "$PROJECT" 30 > "$OUT/e49_trace_writer_exit.txt" 2>&1
# E50：D7 的 POSIX 门禁测试本身多数了一个 fd（对照只改测试的测量时点）
git -C "$CONTROL" apply "$HERE/e50_lifecycle_test_fd_proposal.diff"
{
  for tree in "$PROJECT" "$CONTROL"; do
    echo "== $(git -C "$tree" rev-parse --short HEAD)$(git -C "$tree" diff --quiet || echo '（对照：恢复 stderr 后再计数）')"
    for i in 1 2 3; do
      (cd "$tree" && taskset -c 2 python3 -m unittest tests.motion_nav.test_d102_registry_lifecycle.RegistryTests.test_real_worker_lifecycle_matrix_releases_without_cyclic_gc 2>&1 | grep -E "AssertionError|^OK|^FAILED")
    done
  done
} > "$OUT/e50_lifecycle_test_fd.txt" 2>&1
git -C "$CONTROL" checkout -- tests/motion_nav/test_d102_registry_lifecycle.py
# 项目自己的分片器，完整 tests/motion_nav（Linux 约 30 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
