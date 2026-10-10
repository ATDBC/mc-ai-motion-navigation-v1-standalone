"""5A-R R2 审查对照：在项目冻结的同一组 R2 夹具上，量“转向动作元、单次全输入枚举”的成本。

运行：在 ebc4129（或 cfd36acd）仓库根目录，
PYTHONPATH=.:<本目录> python <本文件> [--no-floor] [--grounded-jump] [场景名 ...]

与项目 R2 实现相同的部分：G(0..20) → 可选 J → A(0..16) → B；G、J、A 各自独立选步态；
两支时序执行相同输入；候选先过 _potential_goal 与逐支 _goal_check，再交给同一个 scan_commitment。
与项目 R2 实现不同的部分（即本对照要量的东西）：
- 不分层耗尽：一次枚举请求的全部输入；
- 朝向不是网格维度：每个 tick 在该步态已声明的朝向里选最接近目标中心方向的一个；
- 先试全部不跳的候选，再从最晚起跳点往前试；
- B 每延长一 tick 都检查终速带，但全部分支着地且水平速度为零（stop_input 下的不动点），
  或速度已低于目标最小终速后，不再延长；
- 默认把“低于锚点与目标较低者”的状态视为失败（模板限制，不是有证明的剪枝），--no-floor 关闭；
- --grounded-jump 只在全部分支着地的 G 点尝试 J（模板定义：J 指起跳；空中按跳键不起跳）。
不设计数预算，只报告实际用量和是否在冻结预算内。这是测量工具，不是建议的实现。
"""
from dataclasses import replace
import itertools
import math
import sys
import time

from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.reference_search import (
    _boundary_evidence, _goal_check, _potential_goal,
)
from experiments.motion_navigation.trajectory_proto.scenarios import primitive_fixture
from mc2p.motion_nav.physics_types import CalculationStatus

from tier_cost_probe import NEGATIVES, POSITIVES

UNBOUNDED = SearchBudget(10_000_000, 100_000_000, 40, 2)
FLOOR = True
GROUNDED_JUMP = False


def gait(command):
    return (command.forward, command.strafe, command.jump, command.sneak, command.sprint)


def moving(command):
    return command.forward != 0. or command.strafe != 0.


def heading_error(state, command, goal):
    region = goal.region
    dx = (region.min_x + region.max_x) / 2. - state.position[0]
    dz = (region.min_z + region.max_z) / 2. - state.position[2]
    wanted = math.atan2(-dx, dz)  # physics: vx = -sin(yaw), vz = cos(yaw)
    return abs(math.remainder(command.movement_yaw_radians - wanted, math.tau))


def steer_search(request, world):
    if request.goal.minimum_resources.values:
        return "needs_information/unproven_resources", None, None, {}
    limit = request.budget.max_trajectory_ticks
    request = replace(request, budget=UNBOUNDED)
    counter = CountedPhysics(UNBOUNDED)
    goal, stop, alphabet = request.goal, request.stop_input, request.supported_inputs
    floor = min(request.anchor_state.position[1], goal.region.min_y)
    minimum = request.minimum_terminal_speed_blocks_per_second
    gaits = list(dict.fromkeys(gait(c) for c in alphabet))
    walk_gaits = [g for g in gaits if not g[2] and (g[0] != 0. or g[1] != 0.)]
    jump_gaits = [g for g in gaits if g[2]]
    stats = {"completions": 0, "scans": 0, "unknown": False}
    tried = set()

    def steer(states, g):
        options = [c for c in alphabet if gait(c) == g]
        return min(options, key=lambda c: (heading_error(states[0], c, goal), alphabet.index(c)))

    def roll(states, command):
        nxt = []
        for state in states:
            result = counter.step(state, command, world)
            if result.status is CalculationStatus.NEEDS_WORLD:
                stats["unknown"] = True
                return None
            if (result.status is not CalculationStatus.OK or result.next_state.horizontal_collision
                    or (FLOOR and result.next_state.position[1] < floor - 1.e-7)):
                return None
            nxt.append(result.next_state)
        return tuple(nxt)

    def verify(inputs, states):
        checks = []
        for branch, state in zip(request.timing_branches, states):
            check, missing = _goal_check(request, branch, state, world, counter)
            if missing:
                stats["unknown"] = True
                return None
            checks.append(check)
        if not all(check.accepted for check in checks):
            return None
        stats["scans"] += 1
        scan = scan_commitment(request, inputs, world, _boundary_evidence(request, inputs), counter=counter)
        if scan.status is ScanStatus.NEEDS_INFORMATION:
            stats["unknown"] = True
        return inputs if scan.status is ScanStatus.VERIFIED_CANDIDATE else None

    def complete(states, inputs):
        if inputs in tried:
            return None
        tried.add(inputs)
        while True:
            stats["completions"] += 1
            if _potential_goal(request, states):
                found = verify(inputs, states)
                if found:
                    return found
            speeds = [20. * math.hypot(s.velocity_blocks_per_tick[0], s.velocity_blocks_per_tick[2])
                      for s in states]
            if (len(inputs) >= limit or (all(s.on_ground for s in states) and max(speeds) == 0.)
                    or max(speeds) + 1.e-12 < minimum):
                return None
            states = roll(states, stop)
            if states is None:
                return None
            inputs += (stop,)

    def hold(states, inputs, g, ticks):
        path = [(states, inputs)]
        for _ in range(ticks):
            if len(inputs) >= limit:
                break
            command = steer(states, g)
            states = roll(states, command)
            if states is None:
                break
            inputs += (command,)
            path.append((states, inputs))
        return path

    entry = request.entry_states
    for command in request.input_prefix:
        entry = roll(entry, command)
        if entry is None:
            return "search_exhausted", None, counter, stats
    runs = [hold(entry, request.input_prefix, g, 20) for g in walk_gaits]
    for path in runs:
        for states, inputs in path:
            found = complete(states, inputs)
            if found:
                return "found", found, counter, stats
    for path in runs:
        for states, inputs in reversed(path):
            if len(inputs) >= limit or (GROUNDED_JUMP and not all(s.on_ground for s in states)):
                continue
            for jg in jump_gaits:
                jump = steer(states, jg)
                lifted = roll(states, jump)
                if lifted is None:
                    continue
                for ag in walk_gaits:
                    for flying, flown in hold(lifted, inputs + (jump,), ag, 16):
                        found = complete(flying, flown)
                        if found:
                            return "found", found, counter, stats
    return ("needs_information/unknown_world" if stats["unknown"] else "search_exhausted"), None, counter, stats


def shape(found):
    def letter(c):
        return "SJ" if c.jump and c.sprint else "J" if c.jump else "S" if c.sprint else "W" if moving(c) else "N"
    return " ".join(f"{letter(c)}{round(math.degrees(c.movement_yaw_radians))}x{len(tuple(g))}"
                    for c, g in itertools.groupby(found))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--no-floor" in args:
        FLOOR = False
        args.remove("--no-floor")
    if "--grounded-jump" in args:
        GROUNDED_JUMP = True
        args.remove("--grounded-jump")
    wanted = set(args)
    print("floor prune:", FLOOR, "grounded jump:", GROUNDED_JUMP)
    for name, tier in POSITIVES + NEGATIVES:
        if wanted and name not in wanted:
            continue
        fixture = primitive_fixture(name, tier) if tier else primitive_fixture(name)
        started = time.perf_counter()
        status, found, counter, stats = steer_search(fixture.request, fixture.world)
        elapsed = (time.perf_counter() - started) * 1000.
        steps = 0 if counter is None else counter.physics_steps
        within = "" if counter is None else (
            " within" if steps <= fixture.request.budget.max_physics_steps else " OVER")
        print(f"{name:30s} {str(tier):4s} -> {status:32s} steps={steps:6d}{within} "
              f"completions={stats.get('completions', 0)} scans={stats.get('scans', 0)} "
              f"ticks={0 if found is None else len(found):2d} {elapsed:.0f} ms  "
              f"{'' if found is None else shape(found)}", flush=True)
