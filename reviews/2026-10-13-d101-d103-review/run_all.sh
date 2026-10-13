#!/bin/sh
# 用法：PROJECT=<origin/main 07fa1d7 的检出目录> sh run_all.sh
# 只调用项目函数并计时，不改项目文件。输出写到本目录的 output/。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$PROJECT"
git rev-parse HEAD > "$HERE/output/project_head.txt"
PYTHONPATH=.:tests/motion_nav python "$HERE/e15_pipeline_cost.py" > "$HERE/output/e15_pipeline_cost.txt" 2>&1
PYTHONPATH=. python "$HERE/e16_keynode_proof_time.py" > "$HERE/output/e16_keynode_proof_time.txt" 2>&1
