#!/bin/sh
# E32：项目分片器 scripts/run_motion_nav_shards.py 的“每模块超时”在测试留下子进程时是否仍然有效。
# 在临时目录造一个测试模块：它用 multiprocessing 启动一个睡 30 s 的子进程，然后自己卡住。
# 分片器超时后只 kill 直接子进程；如果孙进程继承了 stdout/stderr 管道，communicate() 会等到孙进程退出。
# 用法：sh e32_shard_timeout_grandchild.sh <检出>   （只运行项目脚本，不改项目文件）
set -eu
TMP=$(mktemp -d)
mkdir -p "$TMP/tests/fake"
cat > "$TMP/tests/fake/test_hang.py" <<'PY'
import multiprocessing, time, unittest

class Hang(unittest.TestCase):
    def test_hang(self):
        child = multiprocessing.Process(target=time.sleep, args=(30,), daemon=True)
        child.start()
        time.sleep(600)
PY
start=$(date +%s.%N)
python3 "$1/scripts/run_motion_nav_shards.py" --root "$TMP" --tests-directory tests/fake \
  --timeout-seconds 3 --report "$TMP/report.json" > "$TMP/log.txt" 2>&1 || true
end=$(date +%s.%N)
status=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));m=d['modules'][0];print(m['status'], round(m['duration_seconds'],1))" "$TMP/report.json")
printf '模块上限 3 s；分片器记录：%s；分片器实际返回用时 %.1f s\n' "$status" "$(echo "$end - $start" | bc)"
rm -rf "$TMP"
