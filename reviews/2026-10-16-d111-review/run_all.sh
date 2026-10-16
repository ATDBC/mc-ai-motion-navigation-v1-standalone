#!/bin/sh
# 用法：
#   PROJECT=<origin/main cf101b4 检出> D110=<f663586 检出> sh run_all.sh
# 只调用项目函数、项目脚本和项目测试，或只读项目公开证据；不改任何检出。输出写到本目录的 output/。
# E61 复用 D110 审查目录里的脚本；其中 E54 会强杀残留的场景进程，应在后台或不含 "time.sleep(30)" 字样的 shell 中运行。
# 计时类实验（E59、E60）应在机器空闲时运行，不要与全量检查并行。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
R10="$HERE/../2026-10-15-d110-review"
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"
# E57：D111 改动涉及的根目录测试模块
{
  echo "== cf101b4：D111 改动的根目录测试模块（不在 tests/motion_nav 分片范围内）"
  for m in tests.test_b10_runtime_probe tests.test_fabric_deployment_probe tests.test_player_runtime_v1; do
    printf "%s: " "$m"; (cd "$PROJECT" && taskset -c 0 python3 -m unittest "$m" 2>&1 | tail -3 | tr '\n' ' '); echo
  done
} > "$OUT/e57_root_test_modules.txt" 2>&1
# E58：三次正式 B10 的运行间比较（只读公开证据）
python3 "$HERE/e58_run_to_run_shift.py" "$PROJECT" > "$OUT/e58_run_to_run_shift.txt"
# E59：项目 GC 时间线在真实跨线程 GC 下的复核；两种缓冲写法
{
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e59_project_gc_timeline.py" 200)
  (cd "$D110" && PYTHONPATH=. taskset -c 1,2 python3 "$HERE/e59_project_gc_timeline.py" 10)
} > "$OUT/e59_project_gc_timeline.txt" 2>&1
# E60：一次跨隙求解的时间构成
(cd "$PROJECT" && PYTHONPATH=. taskset -c 2 python3 "$HERE/e60_solve_profile.py" > "$OUT/e60_solve_profile.txt" 2>&1)
# E61：两处测试时序修正在单核上的重复
(cd "$(mktemp -d)" && {
  echo "== 单 CPU（taskset -c 1）：D5 矩阵逐子场景，20 次（脚本沿用 D110 审查 E54）"
  taskset -c 1 python3 "$R10/e54_subreaper_matrix_subtests.py" 20 "D111=$PROJECT"
  echo "== 单核（taskset -c 2）：动作交接测试，30 次（脚本沿用 D110 审查 E53）"
  taskset -c 2 python3 "$R10/e53_repeat_project_tests.py" "$PROJECT" \
    tests.motion_nav.test_d102_action_handoff.ActionHandoffTests.test_same_worker_solve_choice_is_frozen_before_prepare 30
} > "$OUT/e61_test_timing_fixes.txt" 2>&1)
# 项目自己的分片器，完整 tests/motion_nav（Linux 约 30 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
