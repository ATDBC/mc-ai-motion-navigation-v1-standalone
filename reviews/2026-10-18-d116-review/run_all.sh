#!/bin/sh
# 用法：
#   PROJECT=<origin/main 62ec855 检出> sh run_all.sh
# 只调用项目函数、项目夹具和项目测试，或只读项目公开的证据。对照改动只在 PROJECT 之外
# 新建的临时 git worktree 中应用，结束时删除；PROJECT 本身不改。
# E94 复用 D110 审查目录的 e53_repeat_project_tests.py。
# 重复运行的分布与机器负载有关：下面用“空闲单核”和“同核另有忙循环”两种明确条件。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
R10="$HERE/../2026-10-15-d110-review"
OUT="$HERE/output"
git -C "$PROJECT" rev-parse HEAD > "$OUT/project_head.txt"

# 临时对照树（diff 以 LF 保存；目标文件有 CRLF，用 --ignore-whitespace 应用）
SCRATCH=$(mktemp -d)
make_tree() {  # make_tree <名称> <diff...>
  name=$1; shift
  git -C "$PROJECT" worktree add --detach "$SCRATCH/$name" HEAD > /dev/null 2>&1
  for d in "$@"; do git -C "$SCRATCH/$name" apply --ignore-whitespace "$HERE/$d.diff"; done
}
A=e87_prepared_apply_event_control
B=e88_tracker_atomic_handoff_control
C=e89_prepared_numeric_tolerance_control
D=e91_receiver_snapshot_retry_control
make_tree ab $A $B
make_tree abc $A $B $C
make_tree all $A $B $C $D

# E86：不加诊断补丁时，正式链走了哪些动作准备入口（只计数）
(cd "$PROJECT" && PYTHONPATH=. python3 "$HERE/e86_production_prepared_entry_census.py") \
  > "$OUT/e86_production_prepared_entry_census.txt" 2>&1

# E84：D116 代表链跑完整条路线（80 tick），原样与四处对照
{
  echo "== 62ec855 原样（D116 诊断补丁恢复自动准备）"
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e84_two_jump_full_run.py" 80 2>&1)
  echo; echo "== 四处对照修复树（e87+e88+e89+e91）"
  (cd "$SCRATCH/all" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e84_two_jump_full_run.py" 80 2>&1)
} > "$OUT/e84_two_jump_full_run.txt"

# E97：tick 21 的激活后路线复核与 prepared 状态比较（原样、只修 a+b、公开 Windows 证据）
{
  echo "== 62ec855 原样（D116 诊断补丁恢复自动准备）"
  (cd "$PROJECT" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e97_tick21_fields.py" 2>&1)
  echo; echo "== 62ec855 + e87 + e88（只修 a、b）"
  (cd "$SCRATCH/ab" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e97_tick21_fields.py" 2>&1)
  echo; echo "== 对照：D116 公开的 Windows 证据 evidence/motion_navigation/d116-handoff-timeline-v1/d116-1-semantic-result-timeline.json，tick 21"
  (cd "$PROJECT" && python3 -c "
import json
d = json.load(open('evidence/motion_navigation/d116-handoff-timeline-v1/d116-1-semantic-result-timeline.json', encoding='utf-8'))
row = {r['movement_tick']: r for r in d['timeline']}[21]
for k in ('incumbent_validation', 'incumbent_validation_reason', 'route_cancel_after_validation', 'prepared_expected_state_matches', 'prepared_controller_reason', 'prepared_controller_state_after', 'prepared_expected_velocity', 'prepared_actual_velocity'): print(f'  {k}: {row.get(k)}')
")
} > "$OUT/e97_tick21_fields.txt"

# E96：快照借用持续失败时有没有回退（原样与四处对照）
{
  echo "== 62ec855 原样（D116 诊断补丁恢复自动准备）"
  (cd "$PROJECT" && PYTHONPATH=. python3 "$HERE/e96_persistent_snapshot_failure.py" 2>&1)
  echo; echo "== 四处对照修复树（e87+e88+e89+e91）"
  (cd "$SCRATCH/all" && PYTHONPATH=. python3 "$HERE/e96_persistent_snapshot_failure.py" 2>&1)
} > "$OUT/e96_persistent_snapshot_failure.txt"

# E85／E88／E91：逐步加对照修复后的 30 次分布（当时另有测试进程占用其它核，负载未控制）
{
  echo "== 62ec855 原样（D116 诊断补丁恢复自动准备），max_ticks=25"
  (cd "$PROJECT" && PYTHONPATH=. python3 "$HERE/e85_timeline_repeat.py" 30 2>&1)
  echo; echo "== 62ec855 原样，max_ticks=80"
  (cd "$PROJECT" && PYTHONPATH=. python3 "$HERE/e85_timeline_repeat.py" 30 80 2>&1)
  echo; echo "== 对照树：+ e87 apply 事件 + e88 tracker 同一提交 + e89 prepared 数值容差，max_ticks=80"
  (cd "$SCRATCH/abc" && PYTHONPATH=. python3 "$HERE/e85_timeline_repeat.py" 30 80 2>&1)
} > "$OUT/e85_timeline_repeat.txt"
{
  echo "== 对照树：+ e87 apply 事件 + e88 tracker 同一提交（只修用户提出的两项），max_ticks=80"
  (cd "$SCRATCH/ab" && PYTHONPATH=. python3 "$HERE/e85_timeline_repeat.py" 30 80 2>&1)
} > "$OUT/e88_two_fix_repeat.txt"
{
  echo "== 对照树：+ e87 + e88 + e89 + e91 receiver 有界重试借快照，max_ticks=80"
  (cd "$SCRATCH/all" && PYTHONPATH=. python3 "$HERE/e85_timeline_repeat.py" 30 80 2>&1)
} > "$OUT/e91_four_fix_repeat.txt"

# E95：同样的 30 次重复，负载条件明确
for t in PROJECT all; do
  if [ $t = PROJECT ]; then tree=$PROJECT; label="62ec855 原样"; else tree=$SCRATCH/all; label="62ec855 + e87+e88+e89+e91"; fi
  echo "== $label，空闲单核 CPU 0"
  (cd "$tree" && PYTHONPATH=. taskset -c 0 python3 "$HERE/e85_timeline_repeat.py" 30 80 2>&1)
  echo
done > "$OUT/e95_controlled_load_repeat.txt"
taskset -c 1 timeout 3000 python3 -c "while True: pass" > /dev/null 2>&1 &
HOG=$!
for t in PROJECT all; do
  if [ $t = PROJECT ]; then tree=$PROJECT; label="62ec855 原样"; else tree=$SCRATCH/all; label="62ec855 + e87+e88+e89+e91"; fi
  echo "== $label，CPU 1 同核另有忙循环"
  (cd "$tree" && PYTHONPATH=. taskset -c 1 python3 "$HERE/e85_timeline_repeat.py" 30 80 2>&1)
  echo
done >> "$OUT/e95_controlled_load_repeat.txt"

# E94：D116 时间线测试本身的通过率（同一忙循环仍在 CPU 1 上）
M=tests.motion_nav.test_d116_handoff_timeline
{
  echo "== 62ec855，CPU 1 同核另有忙循环"
  taskset -c 1 python3 "$R10/e53_repeat_project_tests.py" "$PROJECT" $M 20
  echo "== 62ec855，空闲单核（CPU 0）"
  taskset -c 0 python3 "$R10/e53_repeat_project_tests.py" "$PROJECT" $M 20
} > "$OUT/e94_d116_timeline_test_repeat.txt" 2>&1
kill "$HOG" 2> /dev/null || true

# E92／E93：玩家可见差异（现行生产链 vs 四处对照后的 prepared 接续）
{
  echo "== 62ec855 现行生产链"
  (cd "$PROJECT" && PYTHONPATH=. python3 "$HERE/e92_two_jump_player_visible.py" production 5 2>&1)
  echo; echo "== 四处对照修复树（e87+e88+e89+e91），prepared 接续"
  (cd "$SCRATCH/all" && PYTHONPATH=. python3 "$HERE/e92_two_jump_player_visible.py" prepared 5 2>&1)
} > "$OUT/e92_two_jump_player_visible.txt"
{
  echo "== 62ec855 现行生产链"
  (cd "$PROJECT" && PYTHONPATH=. python3 "$HERE/e93_momentum_gap_routes.py" production 1 2 3 2>&1)
  echo; echo "== 四处对照修复树，prepared 接续"
  (cd "$SCRATCH/all" && PYTHONPATH=. python3 "$HERE/e93_momentum_gap_routes.py" prepared 1 2 3 2>&1)
} > "$OUT/e93_momentum_gap_routes.txt"

# E90：prepared 交接直接与相邻的 15 个测试模块，原样与对照树
T="tests.motion_nav.test_d116_handoff_timeline tests.motion_nav.test_d116_semantic_dependency_handoff tests.motion_nav.test_d102_action_handoff tests.motion_nav.test_d102_runtime_activation tests.motion_nav.test_d102_registry_lifecycle tests.motion_nav.test_d102_receiver tests.motion_nav.test_d102_prepared_proof tests.motion_nav.test_d102_contracts tests.motion_nav.test_d102_contract_red tests.motion_nav.test_d102_world_snapshot tests.motion_nav.test_d114_b10_formal_driver tests.motion_nav.test_d113_delivery_adaptation tests.motion_nav.test_action_continuity_formal tests.motion_nav.test_r27_async_admission tests.test_b10_runtime_probe"
{
  for t in PROJECT abc all; do
    case $t in
      PROJECT) tree=$PROJECT; echo "== 62ec855 原样";;
      abc) tree=$SCRATCH/abc; echo; echo "== 对照树（e87+e88+e89）";;
      all) tree=$SCRATCH/all; echo; echo "== 对照树（e87+e88+e89+e91）";;
    esac
    (cd "$tree" && taskset -c 3 python3 -m unittest $T 2>&1 | grep -E "^(FAIL|ERROR):|^AssertionError|^Ran|^OK|^FAILED" || true)
  done
} > "$OUT/e90_prepared_handoff_modules.txt"

for t in ab abc all; do git -C "$PROJECT" worktree remove --force "$SCRATCH/$t"; done
rmdir "$SCRATCH"

# 项目自己的分片器，完整 tests/motion_nav（Linux 单核约 30 分钟，单独运行）：
#   (cd "$PROJECT" && taskset -c 2 python3 scripts/run_motion_nav_shards.py --report "$OUT/linux-forward.json")
# output/linux-forward-summary.txt 由该报告逐模块整理而来。
