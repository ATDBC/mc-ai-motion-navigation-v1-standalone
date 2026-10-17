#!/bin/sh
# 用法：
#   PROJECT=<origin/main 37ea00b 检出> sh run_all.sh
# 只调用项目函数、项目夹具和项目测试，或只读项目公开的原始证据。E82 的对照改动在 PROJECT 之外
# 新建临时 git worktree 应用，结束时删除；PROJECT 本身不改。
# E77 复用 D110 审查目录的 e53_repeat_project_tests.py；E82 复用 D113 审查目录的 e69 对照 diff。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
R10="$HERE/../2026-10-15-d110-review"
R13="$HERE/../2026-10-16-d113-review"
OUT="$HERE/output"
RAW="$PROJECT/evidence/motion_navigation/d114-formal-driver-v1/formal-default"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"

# E75／E78／E79：只读 D114 公开的 gzip 原始证据
python3 "$HERE/e75_ground_flag_frames.py" "$RAW/raw" "$RAW/analysis.json" > "$OUT/e75_ground_flag_frames.txt" 2>&1
python3 "$HERE/e78_selected_intent_freshness.py" "$RAW/raw" > "$OUT/e78_selected_intent_freshness.txt" 2>&1
python3 "$HERE/e79_post_teleport_ground_blip.py" "$RAW/raw" > "$OUT/e79_post_teleport_ground_blip.txt" 2>&1

# E80：用项目脚本重算控制准备与交付（脚本需要解压后的文件，解压到临时目录）
UNZ=$(mktemp -d)
mkdir "$UNZ/raw"
for name in trace.jsonl b10-coordinator-control-frames.jsonl b10-receiver-diagnostics.jsonl b10-gap-solver-trials.jsonl; do
  gzip -dc "$RAW/raw/$name.gz" > "$UNZ/raw/$name"
done
# 项目脚本拒绝把输出写进输入目录，所以输入放在 raw/，输出放在上一级。
(cd "$PROJECT" && PYTHONPATH=. python3 scripts/analyze_d113_delivery_baseline.py "$UNZ/raw" --output "$UNZ/e80.json")
python3 - "$UNZ/e80.json" > "$OUT/e80_control_prepare_recompute.txt" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("E80：用项目脚本 scripts/analyze_d113_delivery_baseline.py 只读重算 D114 唯一 Fabric 批次（解压后的公开原始文件）")
print("source_hashes_unchanged:", d["source_hashes_unchanged"], "；performance_signable:", d["performance_signable"], "；production_thresholds_met:", d["production_thresholds_met"])
print("mapping_issues:", d["mapping_issues"])
for k in ("production_control_prepare", "synchronous_probe", "other_fixture", "all_trace_diagnostic"):
    v = d[k]
    print(f"{k}: n={v.get('source_association_count')} P50={v.get('p50')} P95={v.get('p95')} P99={v.get('p99')} 最大={v.get('maximum')}")
i = d["identity_delivery"]
print("identity_delivery:", {k: i[k] for k in ("identity_count", "incomplete_count", "invalid_order_count", "visibility_endpoint_unavailable_count")})
print("actual_delivery_ticks:", i["actual_delivery_ticks"])
PY
rm -rf "$UNZ"

# E76：帧数上限截断后的门槛分类；E83：两种“等待帧”口径
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0,2 python3 "$HERE/e76_frame_limit_gate.py" 160 36 30 24 18 12 6 3) \
  > "$OUT/e76_frame_limit_gate.txt" 2>&1
(cd "$PROJECT" && PYTHONPATH=. taskset -c 0,2 python3 "$HERE/e83_waiting_definitions.py") \
  > "$OUT/e83_waiting_definitions.txt" 2>&1

# E81：直接与相邻测试；E82：去掉生产驱动撤销旧租约后的 D114 组件测试
T="tests.motion_nav.test_d114_b10_formal_driver tests.motion_nav.test_d113_delivery_adaptation tests.motion_nav.test_d113_delivery_metrics tests.motion_nav.test_d113_targeted_probe tests.motion_nav.test_d112_worker_degradation tests.motion_nav.test_d112_delivery_baseline tests.test_b10_runtime_probe tests.motion_nav.test_action_continuity_formal tests.motion_nav.test_motion_start_delivery tests.motion_nav.test_d108_receiver_tick_delivery tests.motion_nav.test_b10_motion_candidate tests.test_fabric_deployment_probe tests.motion_nav.test_standalone_export"
SCRATCH=$(mktemp -d)
git -C "$PROJECT" worktree add --detach "$SCRATCH/noguard" HEAD > /dev/null 2>&1
# 目标文件为 LF 或 CRLF；diff 以 LF 保存，用 --ignore-whitespace 应用。
git -C "$SCRATCH/noguard" apply --ignore-whitespace "$R13/e69_no_lease_revoke_control.diff"
{
  echo "== E82：37ea00b + 上一轮 e69_no_lease_revoke_control.diff（去掉生产驱动在无移动帧撤销旧租约的那一行）"
  echo "-- tests.motion_nav.test_d114_b10_formal_driver"
  (cd "$SCRATCH/noguard" && taskset -c 0,2 python3 -m unittest tests.motion_nav.test_d114_b10_formal_driver 2>&1 \
    | grep -E "^(FAIL|ERROR):|^Ran|^OK|^FAILED" | sed 's/ (tests.motion_nav.test_d114_b10_formal_driver.D114B10FormalDriverTests.[a-z_0-9]*)//' || true)
  echo; echo "== 对照：37ea00b 原样，同一组 13 个直接与相邻模块"
  (cd "$PROJECT" && taskset -c 0,2 python3 -m unittest $T 2>&1 | grep -E "^(FAIL|ERROR):|^Ran|^OK|^FAILED" || true)
} > "$OUT/e81_e82_focused_and_lease_control.txt" 2>&1
git -C "$PROJECT" worktree remove --force "$SCRATCH/noguard"
rmdir "$SCRATCH"

# E77：D102 接收周期等待（同核另有忙循环）
C=tests.motion_nav.test_d102_action_handoff.ActionHandoffTests
taskset -c 1 timeout 3000 python3 -c "while True: pass" > /dev/null 2>&1 &
HOG=$!
{
  echo "== 37ea00b，同核另有忙循环"
  taskset -c 1 python3 "$R10/e53_repeat_project_tests.py" "$PROJECT" \
    $C.test_parent_request_building_stays_on_existing_receiver 40 \
    $C.test_parent_builder_failure_is_typed_and_keeps_receiver_and_body_uses 40 \
    $C.test_retired_selection_loses_authority_and_keeps_incumbent_uses 40 \
    $C.test_old_permission_cannot_prepare_new_generation 40
} > "$OUT/e77_d102_contention.txt" 2>&1
kill "$HOG" 2> /dev/null || true

# 项目自己的分片器，完整 tests/motion_nav（Linux 单核约 30 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 3 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
