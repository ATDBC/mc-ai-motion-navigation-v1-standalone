#!/bin/sh
# 分片计时：$1 = 项目检出，$2 = 测试文件；输出 秒数、模块、unittest 摘要（每模块 20 分钟上限）
cd "$1"
m=$(echo "$2" | sed 's#/#.#g; s#\.py$##')
start=$(date +%s.%N)
out=$(timeout 1200 python -m unittest "$m" 2>&1 | tail -4 | tr '\n' ' ')
rc=$?
end=$(date +%s.%N)
sum=$(echo "$out" | grep -oE 'Ran [0-9]+ tests? in [0-9.]+s|OK[^ ]*( \([^)]*\))?|FAILED \([^)]*\)' | tr '\n' ' ')
[ -z "$sum" ] && sum="NO-SUMMARY(timeout?)"
printf '%8.1f s  %s  %s\n' "$(echo "$end - $start" | bc)" "$m" "$sum"
