#!/bin/sh
# 用法：PROJECT=<origin/main eec26af 的检出目录> sh run_all.sh
# 只调用项目函数并计时，不改项目文件。输出写到本目录的 output/。
# E19 还会导入上一轮 ../2026-10-13-d104-k1-review/e17_sound_sparse_scan.py 的 sparse_scan。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
TMP=$(mktemp -d)
cd "$PROJECT"
git rev-parse HEAD > "$HERE/output/project_head.txt"
PYTHONPATH=. taskset -c 2 python "$HERE/e19_t1_cost_split.py" > "$HERE/output/e19_t1_cost_split.txt" 2>&1
PYTHONPATH=. taskset -c 2 python "$HERE/e20_t2_control_ticks.py" "$TMP/e20" > "$HERE/output/e20_t2_control_ticks.txt" 2>&1
PYTHONPATH=. taskset -c 2 python "$HERE/e21_t2_trusted_projection.py" > "$HERE/output/e21_t2_trusted_projection.txt" 2>&1
rm -rf "$TMP"
