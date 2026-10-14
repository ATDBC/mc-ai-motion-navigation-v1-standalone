#!/bin/sh
# E25：完整 tests/motion_nav 的状态。
# 用法：E25_TREES="<5c1485d 检出> <07fa1d7 检出> <98a0542 检出>" sh e25_suite_regression.sh
# 在每个检出上串行运行同一组模块（它们在 98a0542 的 Linux 分片运行中失败，且与平台无关的错误类型居多），
# 打印 Ran/OK/FAILED 和每类断言的首行，便于比较回归从哪个提交开始。
# 只运行项目测试，不改项目文件。
set -u
MODULES="test_async_gap_revalidation_delivery test_f2rec_r0_baseline test_motion_baseline_recovery test_motion_start_delivery test_navigation_design_metrics test_navigation_product_delivery test_public_repository_completeness test_r27_async_admission test_r28_planning_retry_successor test_r28_reanchor_handoff test_r28_persistent_budget_formal"
for tree in $E25_TREES; do
  echo "######## $(git -C "$tree" rev-parse --short HEAD) $(git -C "$tree" log -1 --format=%s)"
  for m in $MODULES; do
    if [ ! -f "$tree/tests/motion_nav/$m.py" ]; then echo "  $m: (module absent)"; continue; fi
    out=$(cd "$tree" && timeout 900 python -m unittest "tests.motion_nav.$m" 2>&1)
    echo "  $m: $(echo "$out" | grep -E '^Ran |^OK|^FAILED' | tr '\n' ' ')"
    echo "$out" | grep -E '^(AssertionError|AttributeError|TypeError|ValueError|KeyError|RuntimeError)' \
      | cut -c1-200 | sort | uniq -c | sort -rn | head -3 | sed 's/^/      /'
  done
done
