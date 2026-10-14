#!/bin/sh
# E30 驱动：在 <检出> 上对每个模块、每种屏障模式各运行 <次数> 次（each run is a fresh process）。
# 用法：sh e30_run.sh <检出> <次数> <CPU>   （可用环境变量 E30_MODULES 只跑部分模块）
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$1"
MODULES=${E30_MODULES:-"test_r27_async_admission test_motion_start_delivery test_motion_baseline_recovery \
  test_async_gap_revalidation_delivery test_runtime_backend_io test_navigation_product_delivery \
  test_action_continuity_formal test_runtime_navigation_verified_handoff test_r28_reanchor_handoff"}
for m in $MODULES; do
  for mode in barrier none sleep50; do
    i=0
    while [ "$i" -lt "$2" ]; do
      PYTHONPATH=. timeout 900 taskset -c "$3" python3 "$HERE/e30_receiver_barrier.py" "$mode" "$m" 2>/dev/null \
        || echo "$mode $m 未完成（900 s 上限或进程异常）"
      i=$((i + 1))
    done
  done
done
