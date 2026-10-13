#!/bin/sh
# 用法：PROJECT=<origin/main d984138 的检出目录> sh run_all.sh
# 只调用项目函数并计时，不改项目文件。输出写到本目录的 output/。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$PROJECT"
git rev-parse HEAD > "$HERE/output/project_head.txt"
PYTHONPATH=. python "$HERE/e17_sound_sparse_scan.py" > "$HERE/output/e17_sound_sparse_scan.txt" 2>&1
PYTHONPATH=. python "$HERE/e18_mock_robustness.py" > "$HERE/output/e18_mock_robustness.txt" 2>&1
