"""E93：连续接续在什么路线上才有玩家可见的价值。

在 D116 代表场景的基础上只改地形：第一段 1 格间隙，中间平台 1 格，第二段间隙
宽 W 格（W = 1, 2, 3），目标在第二段间隙之后的平台上。同一地形分别跑：
  production：不加补丁，现行生产链；
  prepared：进程内恢复 prepare_upcoming_action()（与 D116 时间线脚本相同的条件）。
在哪棵树里运行，就用哪棵树的生产代码。报告终态、原因、完成 tick、起跳 tick、着地 tick、
规划出的动作序列和伤害。用法：python e93_momentum_gap_routes.py <mode> [W ...]
"""
import json
import subprocess
import sys

CHILD = r'''
import json, sys
from dataclasses import replace
from unittest.mock import patch
from scripts import d116_handoff_timeline as t
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from tests.sim.backend import Scene
from tests.sim.runner import InlineMotionWorker, Scenario, run
mode, width = sys.argv[1], int(sys.argv[2])
gap2 = set(range(4, 4 + width))
end = 4 + width + 3
solids = {(x, 63, z): "minecraft:grass_block" for x in range(-1, 2) for z in range(end + 1)
          if z != 2 and z not in gap2}
case = Scenario(f"momentum-gap-1-then-{width}", Scene(solids, ((-2, 2), (60, 70), (-2, end + 2))),
                (.5, 64.0, .5), (.5, 64.0, 4 + width + 1.5), max_ticks=120)
planned = []
original = MotionRouteCoordinator.decide
def decide(self, frame, anchor, ledger, world, **kwargs):
    if not planned:
        planned.extend(type(action).__name__ for action in self.route.action_route.actions)
    decision = original(self, frame, anchor, ledger, world, **kwargs)
    if mode == "prepared":
        next_index = self.executor.action_index + 1
        if (self.executor.current_verified_action_started()
                and self.executor.active_verified_exit_state() is not None
                and next_index < len(self.route.action_route.actions)
                and self.generation_for(next_index) is None
                and self._work_identity is None):
            self.prepare_upcoming_action(anchor, world, ledger_identity=ledger.ledger_identity)
    return decision
trace = []
with patch.object(MotionRouteCoordinator, "decide", decide):
    result = run(case, motion_factory=InlineMotionWorker, trace_sink=trace.append)
rows = [(row["movement_tick"], row["applied_movement"], row["on_ground"]) for row in trace]
jumps = [tick for tick, moved, _g in rows if moved.get("jump")]
landings = [tick for (tick, _m, g), (_t, _m0, before) in zip(rows[1:], rows[:-1]) if g and not before]
print(json.dumps({"width": width, "outcome": result.outcome, "reason": str(result.reason), "ticks": result.ticks,
                  "jumps": jumps, "landings": landings, "planned": planned,
                  "final_z": round(result.final_position[2], 2), "damage": result.damage,
                  "violations": sorted({code for _t, code, _d in result.violations})}, sort_keys=True))
'''


def main() -> int:
    mode = sys.argv[1]
    widths = [int(value) for value in sys.argv[2:]] or [1, 2, 3]
    print(f"mode={mode}")
    for width in widths:
        done = subprocess.run([sys.executable, "-c", CHILD, mode, str(width)], capture_output=True, text=True,
                              timeout=900)
        line = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else json.dumps(
            {"width": width, "error": done.stderr[-400:]})
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
