#!/bin/sh
# 审查方实验：在项目 01c45d5 的 checkout 根目录运行，PROJECT=<checkout> sh run_all.sh
# 全部是进程内 monkeypatch，不修改项目文件。Linux 耗时只用于比例。
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/output
mkdir -p "$OUT"
cd "$PROJECT"
export PYTHONPATH=.
git log --oneline -1 > "$OUT/project_head.txt"
python3 -m unittest tests.motion_nav.test_incremental_motion_controller tests.motion_nav.test_incremental_motion_table \
  tests.motion_nav.test_incremental_motion_proof tests.motion_nav.test_incremental_motion_performance \
  tests.motion_nav.test_incremental_motion_closed_loop tests.motion_nav.test_incremental_motion_closed_loop_measurement \
  > "$OUT/unittest_p1_p3.txt" 2>&1
python3 scripts/measure_incremental_motion_p3.py --rounds 1 --output "$OUT/p3_linux_1round.json"
python3 "$HERE/compare_with_windows.py" "$OUT/p3_linux_1round.json" \
  evidence/motion_navigation/incremental-motion-r98-v1/p3/closed-loop-matrix.json > "$OUT/compare_with_windows.txt"
python3 "$HERE/case_info.py" > "$OUT/case_info.txt"
python3 "$HERE/e1_e2.py" > "$OUT/e1_e2.txt"
for v in baseline E3 E4 E3+E4 G+E3+E4+SAFEGOAL; do python3 "$HERE/e3_e4.py" "$v"; done > "$OUT/e3_e4_jump_gap_1_matrix.txt"
python3 "$HERE/e7_exact_d.py" > "$OUT/e7_exact_d_project_gate.txt"
python3 "$HERE/e7_exact_d.py" support_only > "$OUT/e7_exact_d_support_only.txt"
for v in NONE E34 E34+CTRL G G+E34 G+E34+BOTH G+E34+BOTH+SAFEGOAL; do python3 "$HERE/e6.py" "$v"; done > "$OUT/e6_d_template_actions.txt"
python3 "$HERE/e8_air_search.py" > "$OUT/e8_air_search.txt"
python3 "$HERE/e9_branch_cost.py" > "$OUT/e9_branch_cost.txt"
python3 "$HERE/e10_after_unresolved.py" > "$OUT/e10_after_unresolved.txt"
