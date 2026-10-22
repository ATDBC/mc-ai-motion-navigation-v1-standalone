#!/bin/sh
# 用法：
#   PROJECT=<origin/main 7f1a95a2 检出> sh run_all.sh
# 只调用项目函数、项目夹具、项目测试和项目 Java 类；PROJECT 本身不改。
# E136 只把项目 ClientTimingSchedule.java 与本目录的探针一起编译到临时目录，不改项目源码。
# 每项都用 taskset 固定到单核，全量分片单独占一个核。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"

# E135：D123-2 的 windowed 观察交换在安全默认执行期内到达（计算器客户端 + D120 972 个入口）
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e135_windowed_exchange_during_default.py") \
  > "$OUT/e135_windowed_exchange_during_default.json"

# E136：同一场景下项目 Java ClientTimingSchedule 的事件顺序
BUILD=$(mktemp -d)
javac -d "$BUILD" "$PROJECT/mc2p/backends/runtime_overlays/mc121_actions/ClientTimingSchedule.java" \
  "$HERE/e136/TimingOrderProbe.java" 2>&1 | grep -v JAVA_TOOL_OPTIONS || true
java -cp "$BUILD" com.mc2p.actions.TimingOrderProbe 2>&1 | grep -v JAVA_TOOL_OPTIONS \
  > "$OUT/e136_java_timing_order.txt"
rm -rf "$BUILD"

# E137：正式 pre-trial 组装从 live anchor 到父请求出站的宿主耗时（组件测试原样运行）
(cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e137_anchor_to_dispatch_budget.py" 2>/dev/null) \
  > "$OUT/e137_anchor_to_dispatch_budget.json"

# E138：预约起跳——在 anchor 后第 K+1 拍证明父动作并绑定后继，与 K=0 是否等价、成本是否随 K 增长
(cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e138_scheduled_start.py") \
  > "$OUT/e138_scheduled_start.json"

# E139：后继 PreparedMotionJob 在 worker 中的耗时
(cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e139_successor_job_budget.py" 2>/dev/null) \
  > "$OUT/e139_successor_job_budget.json"

# E140：本轮新增阶段的测试模块单独运行，以及公开快照中 manifest 绑定但未发布的文件
{
  for m in test_d122_fabric_timing test_d123_fabric_transport test_d125_three_fact_observation \
           test_d126_known_map_motion test_d127_transport_lifecycle test_d128_repository_jdk_launch \
           test_d129_invalid_harness; do
    printf '%s  ' "$m"
    (cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 -m unittest "tests.motion_nav.$m" 2>&1 \
      | grep -E "^(OK|FAILED)|^Ran" | tr '\n' ' ' || true)
    echo
    (cd "$PROJECT" && PYTHONPATH=. taskset -c 1 python3 -m unittest "tests.motion_nav.$m" 2>&1 \
      | grep -E "^FAIL: |^[A-Za-z.]*(Error|Violation|Exception): " | sed "s#$PROJECT#<PROJECT>#g" | sort | uniq -c \
      | sed 's/^/    /' || true)
  done
  echo
  echo "manifest 绑定、但公开快照中不存在的文件："
  (cd "$PROJECT" && python3 - <<'EOF'
import json
import pathlib
missing = []
for manifest in sorted(pathlib.Path("evidence/motion_navigation/d123-proof-and-transport-v1").rglob("manifest.json")):
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    for item in data.get("files", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and "path" in item and not (manifest.parent / item["path"]).exists():
            missing.append(str(manifest.parent / item["path"]))
print(len(missing))
print("\n".join(missing))
EOF
  )
} > "$OUT/e140_new_modules_and_unpublished_logs.txt"

# 项目自己的分片器，完整 tests/motion_nav（Linux 单核约 70 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
