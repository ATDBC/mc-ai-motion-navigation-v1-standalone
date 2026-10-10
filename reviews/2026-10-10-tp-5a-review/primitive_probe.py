"""TP Task 5A 审查补充探针：动作元（分段）系统枚举能否稳定找到同一组跨隙。

运行：在应用 proto-5a-fixes.diff 的仓库根目录 PYTHONPATH=.:<本目录> python <本文件>

候选结构固定为四段，每段是一个动作元：
  助跑：保持 R 共 k1 tick（R 取自允许的地面输入，k1=0..20）；
  起跳：一条带跳的输入 J（可省略）；
  空中：保持 A 共 k2 tick（A 取自允许输入，k2=0..16）；
  刹车：一直中性直到停住，停点必须在目标内。
按固定顺序系统枚举，不用启发排序；第一个通过原型扫描器（两支时序、
停车尾迹、风险区间）和逐支目标检查的候选即返回。
这是定位问题的实验，不是建议的实现；计数包括全部尝试和验证。
"""
from dataclasses import replace
import itertools
import math
import time

from experiments.motion_navigation.trajectory_proto.commitment import (
    ScanStatus, ZERO_SPEED_EPSILON, scan_commitment,
)
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.reference_search import (
    _boundary_evidence, _distance, _goal_check,
)
from experiments.motion_navigation.trajectory_proto.scenarios import representative_fixture
from mc2p.motion_nav.physics_types import CalculationStatus

import scale_probe as sp

UNBOUNDED = SearchBudget(10_000_000, 100_000_000, 40, 2)
GROUND = {"A3": (sp.WALK,), "A5": (sp.WALK, sp.SPRINT)}
JUMPS = {"A3": (sp.WALK_JUMP,), "A5": (sp.WALK_JUMP, sp.SPRINT_JUMP)}
AIR = {"A3": (sp.WALK, sp.NEUTRAL), "A5": (sp.WALK, sp.NEUTRAL, sp.SPRINT)}


def primitive_search(request, world, label):
    request = replace(request, budget=UNBOUNDED)
    counter = CountedPhysics(UNBOUNDED)
    goal = request.goal
    floor = min(request.anchor_state.position[1], goal.region.min_y)
    tried, scans = set(), [0]

    def roll(state, command):
        result = counter.step(state, command, world)
        if (result.status is not CalculationStatus.OK or result.next_state.horizontal_collision
                or result.next_state.position[1] < floor - 1.e-7):
            return None
        return result.next_state

    def brake(state, inputs):
        while len(inputs) < 40:
            state = roll(state, sp.NEUTRAL)
            if state is None:
                return None
            inputs += (sp.NEUTRAL,)
            speed = math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
            if state.on_ground and speed <= ZERO_SPEED_EPSILON:
                return inputs if _distance(state, goal) == 0. else None
        return None

    def verify(inputs):
        if inputs is None or inputs in tried:
            return None
        tried.add(inputs)
        scans[0] += 1
        scan = scan_commitment(request, inputs, world, _boundary_evidence(request, inputs), counter=counter)
        if scan.status is not ScanStatus.VERIFIED_CANDIDATE:
            return None
        checks = [_goal_check(request, branch.timing_branch, branch.states[-1], world, counter)[0]
                  for branch in scan.proof.branches]
        return inputs if all(check is not None and check.accepted for check in checks) else None

    for run in GROUND[label]:
        state, inputs = request.anchor_state, ()
        for k1 in range(21):
            if k1:
                state = roll(state, run)
                if state is None:
                    break
                inputs += (run,)
            found = verify(brake(state, inputs))
            if found:
                return found, counter, scans[0]
            for jump in JUMPS[label]:
                lifted = roll(state, jump)
                if lifted is None:
                    continue
                for air in AIR[label]:
                    flying, flown = lifted, inputs + (jump,)
                    for k2 in range(17):
                        if k2:
                            flying = roll(flying, air)
                            if flying is None or len(flown) >= 40:
                                break
                            flown += (air,)
                        found = verify(brake(flying, flown))
                        if found:
                            return found, counter, scans[0]
    return None, counter, scans[0]


if __name__ == "__main__":
    names = {sp.WALK: "W", sp.WALK_JUMP: "WJ", sp.NEUTRAL: "N", sp.SPRINT: "S", sp.SPRINT_JUMP: "SJ"}
    for start in (1, 4):
        for width in (1, 2, 3):
            if start == 1 and width == 3:
                continue
            for label in ("A3", "A5"):
                request, world, goal = sp.gap_case(width, start=start)
                alphabet = sp.ALPHABETS["A3 walk/jump/neutral" if label == "A3" else "A5 +sprint/sprint_jump"]
                request = replace(request, goal=goal, supported_inputs=alphabet)
                started = time.perf_counter()
                found, counter, scans = primitive_search(request, world, label)
                elapsed = (time.perf_counter() - started) * 1000.
                shape = "—" if found is None else " ".join(
                    f"{names[c]}x{sum(1 for _ in g)}" for c, g in itertools.groupby(found))
                print(f"start{start} gap{width} {label}: {'FOUND' if found else 'none in grid'} "
                      f"ticks={0 if found is None else len(found)} steps={counter.physics_steps} "
                      f"scans={scans} {elapsed:.0f} ms  {shape}", flush=True)
