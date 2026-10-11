#!/bin/sh
# 用法：PROJECT=<origin/main 5c1485d 的检出目录> sh run_all.sh
# 只在进程内构造对象，不改项目文件。输出写到本目录的 output/。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$PROJECT"
git rev-parse HEAD > "$HERE/output/project_head.txt"
for script in e11_packet_margin e12_double_gap e13_tail_budget e14_chain_delay; do
    PYTHONPATH=. python "$HERE/$script.py" > "$HERE/output/$script.txt" 2>&1
done
