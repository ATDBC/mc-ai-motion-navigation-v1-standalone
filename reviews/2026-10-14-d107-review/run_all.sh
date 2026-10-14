#!/bin/sh
# 用法：
#   PROJECT=<origin/main 8167d92 检出> D106=<98a0542 检出> BEFORE=<5c1485d 检出> sh run_all.sh
# 只调用项目函数、项目脚本和项目测试并计时，不改项目文件。输出写到本目录的 output/。
# 计时实验绑定单核；共享主机抖动较大，文中给出多次运行的范围。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
# E28：F1 缩短场景每 tick 成本与线程分布（D103 前与 D107）
: > "$OUT/e28_f1_tick_cost_threads.txt"
for tree in "$BEFORE" "$PROJECT"; do
  (cd "$tree" && PYTHONPATH=. taskset -c 2 python3 "$HERE/e28_f1_tick_cost_threads.py" >> "$OUT/e28_f1_tick_cost_threads.txt" 2>&1)
done
# E29：惰性提交与即时提交的逐字节差分（D106 与 D107；外加变异与修法探针）
WORK=$(mktemp -d)
(cd "$WORK" && {
  PYTHONPATH="$D106" python3 "$HERE/e29_lazy_snapshot_differential.py" 8
  PYTHONPATH="$PROJECT" python3 "$HERE/e29_lazy_snapshot_differential.py" 8
  PYTHONPATH="$PROJECT" python3 "$HERE/e29_lazy_snapshot_differential.py" 8 --mutate-skip-borrow-commit
  PYTHONPATH="$PROJECT" python3 "$HERE/e29_lazy_snapshot_differential.py" 8 --defer-semantic-commit
} > "$OUT/e29_lazy_snapshot_differential.txt" 2>&1)
rm -rf "$WORK"
# E30：测试里的 receiver 屏障替代了什么（每模块每模式 3 次）
sh "$HERE/e30_run.sh" "$PROJECT" 3 2 > "$OUT/e30_receiver_barrier.txt" 2>&1
# E30b：去掉屏障后，分别撤销两处生产修复，看 async_gap 模块靠什么恢复
(cd "$PROJECT" && echo "检出 $(git rev-parse --short HEAD)，receiver 屏障已去掉；分别撤销 D107 的两处生产修复后运行 test_async_gap_revalidation_delivery：" &&
 for v in none no-wake no-retire neither; do
   PYTHONPATH=. taskset -c 2 python3 "$HERE/e30b_production_fix_ablation.py" "$v" 2>&1 | tail -1; done) > "$OUT/e30b_production_fix_ablation.txt"
# E31：Linux 上 MotionSolverWorker.close() 的 EBADF
: > "$OUT/e31_linux_worker_close.txt"
for tree in "$BEFORE" "$D106" "$PROJECT"; do
  (cd "$tree" && PYTHONPATH=. taskset -c 2 python3 "$HERE/e31_linux_worker_close.py" >> "$OUT/e31_linux_worker_close.txt" 2>&1)
done
# E32：分片器超时与孙进程
sh "$HERE/e32_shard_timeout_grandchild.sh" "$PROJECT" > "$OUT/e32_shard_timeout_grandchild.txt" 2>&1
# E33：语义变化的整段重建成本（D103 前、D106、D107）
: > "$OUT/e33_semantic_change_cost.txt"
for tree in "$BEFORE" "$D106" "$PROJECT"; do
  (cd "$tree" && PYTHONPATH=. taskset -c 2 python3 "$HERE/e33_semantic_change_cost.py" >> "$OUT/e33_semantic_change_cost.txt" 2>&1)
done
# E34：借用时补提交挡住控制线程
(cd "$PROJECT" && PYTHONPATH=. taskset -c 2 python3 "$HERE/e34_borrow_lock_stall.py" > "$OUT/e34_borrow_lock_stall.txt" 2>&1)
# E35：慢模块前后对比（约 20 分钟）
{ echo "== BEFORE"; sh "$HERE/e35_slow_modules.sh" "$BEFORE" 2; echo "== PROJECT"; sh "$HERE/e35_slow_modules.sh" "$PROJECT" 2; } > "$OUT/e35_slow_modules.txt" 2>&1
# 项目自己的分片器，完整 tests/motion_nav（Linux 约 40 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json" --timeout-seconds 600)
