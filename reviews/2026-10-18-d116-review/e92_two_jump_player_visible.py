"""E92：同一条两跳路线，现行生产链与连续接续各用多少 tick、两跳之间站多久。

场景是 D116 代表路线（scripts.d116_handoff_timeline._representative，Walk → JumpGap →
JumpGap），max_ticks 放到 80。
  mode=production：不加任何补丁，现行生产链（第二跳在落地后普通 SOLVE）；
  mode=prepared：用 run_representative_timeline() 同样的进程内补丁恢复
                 prepare_upcoming_action()（在哪棵树里运行就用哪棵树的生产代码）。
用 trace 统计：起跳 tick、每次着地 tick、两跳之间“着地且无移动输入”的帧数、
终态与完成 tick。用法：python e92_two_jump_player_visible.py <mode> [次数]
"""
import collections
import json
import subprocess
import sys

CHILD = r'''
import json, sys
from dataclasses import replace
from unittest.mock import patch
from scripts import d116_handoff_timeline as t
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from tests.sim.runner import InlineMotionWorker, run
mode = sys.argv[1]
case = replace(t._representative(), max_ticks=80)
original = MotionRouteCoordinator.decide
def decide(self, frame, anchor, ledger, world, **kwargs):
    decision = original(self, frame, anchor, ledger, world, **kwargs)
    next_index = self.executor.action_index + 1
    if (self.executor.current_verified_action_started()
            and self.executor.active_verified_exit_state() is not None
            and next_index < len(self.route.action_route.actions)
            and self.generation_for(next_index) is None
            and self._work_identity is None):
        self.prepare_upcoming_action(anchor, world, ledger_identity=ledger.ledger_identity)
    return decision
trace = []
if mode == "prepared":
    with patch.object(MotionRouteCoordinator, "decide", decide):
        result = run(case, motion_factory=InlineMotionWorker, trace_sink=trace.append)
else:
    result = run(case, motion_factory=InlineMotionWorker, trace_sink=trace.append)
rows = [(row["movement_tick"], row["applied_movement"], row["on_ground"]) for row in trace]
jumps = [tick for tick, moved, _ground in rows if moved.get("jump")]
landings = [tick for (tick, _m, ground), (_t0, _m0, before) in zip(rows[1:], rows[:-1]) if ground and not before]
between = 0
if len(jumps) >= 2 and landings:
    first_landing = landings[0]
    between = sum(1 for tick, moved, ground in rows
                  if first_landing <= tick < jumps[1] and ground
                  and not any(moved.get(key) for key in ("forward", "strafe", "jump", "sprint")))
print(json.dumps({"outcome": result.outcome, "reason": result.reason, "ticks": result.ticks,
                  "jumps": jumps, "landings": landings[:2], "neutral_grounded_between": between,
                  "violations": sorted({code for _t, code, _d in result.violations}),
                  "damage": result.damage}, sort_keys=True))
'''


def main() -> int:
    mode = sys.argv[1]
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    counter = collections.Counter()
    for _ in range(repeats):
        done = subprocess.run([sys.executable, "-c", CHILD, mode], capture_output=True, text=True, timeout=900)
        line = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else json.dumps({"error": done.stderr[-300:]})
        counter[line] += 1
    print(f"mode={mode}，{repeats} 次")
    for line, count in counter.most_common():
        print(f"{count} 次：{line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
