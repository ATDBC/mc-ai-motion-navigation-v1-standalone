#!/bin/sh
# E27：最慢的几个 tests/motion_nav 模块在 D103 前后的串行耗时。
# 用法：sh e27_slow_modules.sh <检出目录> <绑定的 CPU 编号>
# 只运行项目测试并计时，不改项目文件。F1 模块在 D106 上会超过 15 分钟，单独设上限。
cd "$1"
for m in test_f2_ground_completion_region test_d094_fabric_timing_metric test_navigation_closed_loop test_navigation_supervised_interruptions test_d058_runtime_validation test_f1_known_world_following; do
  s=$(date +%s.%N)
  r=$(timeout 900 taskset -c "$2" python -m unittest tests.motion_nav.$m 2>&1 | grep -E "^Ran |^OK|^FAILED" | tr '\n' ' ')
  e=$(date +%s.%N)
  [ -z "$r" ] && r="未结束（900 s 上限）"
  printf '%7.1f s  %s  %s\n' "$(echo "$e - $s" | bc)" "$m" "$r"
done
