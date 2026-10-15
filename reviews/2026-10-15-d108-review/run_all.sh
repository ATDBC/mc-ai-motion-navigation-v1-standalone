#!/bin/sh
# 用法：
#   PROJECT=<origin/main a9d51a2 检出> D107=<8167d92 检出> D106=<98a0542 检出> BEFORE=<5c1485d 检出> \
#   CONTROL=<另一份干净的 a9d51a2 检出，只用于对照改动> sh run_all.sh
# 只调用项目函数、项目脚本和项目测试并计时，不改 PROJECT 等项目检出。
# CONTROL 会被临时打上本目录的两个对照 diff，结束时恢复。输出写到本目录的 output/。
# 计时实验绑定单核；共享主机抖动较大，文中给出多次运行的范围。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
# E36：按范围物化与 D106／D107 逐字节差分，外加三种变异
WORK=$(mktemp -d)
(cd "$WORK" && {
  PYTHONPATH="$D106" python3 "$HERE/e36_snapshot_differential_d108.py"
  PYTHONPATH="$D107" python3 "$HERE/e36_snapshot_differential_d108.py"
  PYTHONPATH="$PROJECT" python3 "$HERE/e36_snapshot_differential_d108.py"
  for m in --mutate-skip-retire --mutate-skip-materialize --mutate-forget-dirty-cell; do
    PYTHONPATH="$PROJECT" python3 "$HERE/e36_snapshot_differential_d108.py" "$m"
  done
} > "$OUT/e36_snapshot_differential_d108.txt" 2>&1)
rm -rf "$WORK"
# E37：语义变化的观察线程成本（D103 前、D107、D108）
: > "$OUT/e37_semantic_change_cost.txt"
for tree in "$BEFORE" "$D107" "$PROJECT"; do
  (cd "$tree" && PYTHONPATH=. taskset -c 2 python3 "$HERE/e37_semantic_change_cost.py" >> "$OUT/e37_semantic_change_cost.txt" 2>&1)
done
# E38：无关脏 section 是否还挡住控制线程（D107、D108）
: > "$OUT/e38_borrow_lock_stall.txt"
for tree in "$D107" "$PROJECT"; do
  (cd "$tree" && PYTHONPATH=. taskset -c 2 python3 "$HERE/e38_borrow_lock_stall.py" >> "$OUT/e38_borrow_lock_stall.txt" 2>&1)
done
# E39：Linux 动态生命周期矩阵（D107、D108；重定向标准错误两次，不重定向一次）
: > "$OUT/e39_linux_worker_lifecycle.txt"
for round in 1 2; do
  [ "$round" = 2 ] && echo "（重复运行）" >> "$OUT/e39_linux_worker_lifecycle.txt"
  for tree in "$D107" "$PROJECT"; do
    (cd "$tree" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e39_linux_worker_lifecycle.py" >> "$OUT/e39_linux_worker_lifecycle.txt" 2>&1)
  done
done
: > "$OUT/e39_linux_worker_lifecycle_nocapture.txt"
for tree in "$D107" "$PROJECT"; do
  (cd "$tree" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e39_linux_worker_lifecycle.py" --no-capture >> "$OUT/e39_linux_worker_lifecycle_nocapture.txt" 2>/dev/null)
done
# E39b：feeder 回调造成的引用环与延迟释放的 fd
: > "$OUT/e39b_worker_reference_cycle.txt"
for tree in "$D107" "$PROJECT"; do
  (cd "$tree" && PYTHONPATH=. taskset -c 2 python3 "$HERE/e39b_worker_reference_cycle.py" >> "$OUT/e39b_worker_reference_cycle.txt" 2>&1)
done
# E40／E43：分片器对抗场景与僵尸进程；CONTROL 依次打上两个对照 diff
git -C "$CONTROL" diff --quiet
{
  echo "（一）项目检出"; taskset -c 1 python3 "$HERE/e40_shard_runner_adversarial.py" "$PROJECT"
  git -C "$CONTROL" apply "$HERE/e43_zombie_cleanup_proposal.diff"
  echo "（二）对照：只加 e43_zombie_cleanup_proposal.diff"; taskset -c 1 python3 "$HERE/e40_shard_runner_adversarial.py" "$CONTROL"
} > "$OUT/e40_shard_runner_adversarial.txt" 2>&1
taskset -c 1 python3 "$HERE/e43_shard_cleanup_zombie.py" "$PROJECT" "$CONTROL" 12 > "$OUT/e43_shard_cleanup_zombie.txt" 2>&1
git -C "$CONTROL" apply "$HERE/e40_subreaper_proposal.diff"
{ echo "（三）对照：再加 e40_subreaper_proposal.diff（Linux 子收割者）"
  taskset -c 1 python3 "$HERE/e40_shard_runner_adversarial.py" "$CONTROL"; } >> "$OUT/e40_shard_runner_adversarial.txt" 2>&1
git -C "$CONTROL" checkout -- scripts/run_motion_nav_shards.py scripts/process_tree.py
# E41：BoundedAsyncTraceWriter 收尾无期限与非守护线程
taskset -c 1 python3 "$HERE/e41_trace_writer_close.py" "$PROJECT" > "$OUT/e41_trace_writer_close.txt" 2>&1
# E42：B10 探针期限账（只读公开证据和源码）
python3 "$HERE/e42_b10_budget_accounting.py" "$PROJECT" > "$OUT/e42_b10_budget_accounting.txt" 2>&1
# E44：快照物化绝对时间门槛在 Linux 上的稳定性
(cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e44_snapshot_gate_flakiness.py" 20 > "$OUT/e44_snapshot_gate_flakiness.txt" 2>&1)
# 项目自己的分片器，完整 tests/motion_nav（Linux 约 30 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
