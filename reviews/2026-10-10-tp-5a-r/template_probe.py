"""5A-R 夹具可行性探针：按任务说明的动作元模板枚举，确认冻结正例确实可解，并比较顺序。

运行：在应用 reviews/2026-10-10-tp-5a-review/proto-5a-fixes.diff 的仓库根目录，
PYTHONPATH=.:reviews/2026-10-10-tp-5a-review:<本目录> python <本文件> [interleaved|simple-first|steer]

三种方案都是：地面段 → 可选一 tick 起跳 → 空中段 → 目标朝向的中性输入保持到所有分支停住，
两支时序同步推进，停点全部在目标内才交给原型扫描器和逐支目标检查。
- interleaved：每段固定一个声明输入；每个助跑长度先试完整个起跳／空中子网格。
- simple-first：每段固定一个声明输入；先试全部不跳的候选，再从最晚起跳点往前试。
- steer：每段固定一种步态，每 tick 选朝向最接近目标中心方向的声明输入；顺序同 simple-first。
段内输入按“朝向与目标方向的夹角、再按声明顺序”排序，零移动输入排最后。这些只改变顺序，不删网格点。
这是确认可行性的离线工具，不是建议的实现；不限预算，只报告实际用量。
"""
from dataclasses import replace
import itertools
import math
import time

from experiments.motion_navigation.trajectory_proto.commitment import (
    ScanStatus, ZERO_SPEED_EPSILON, scan_commitment,
)
from experiments.motion_navigation.trajectory_proto.contracts import (
    ApplicationEvidence, KnownInputApplication, SearchBudget,
)
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.reference_search import (
    _boundary_evidence, _distance, _goal_check,
)
from experiments.motion_navigation.trajectory_proto.scenarios import representative_fixture
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, CellFact, CellKnowledge, WorldView

import scale_probe as sp
from alphabet_probe import ALPHABET15

ORDER = "interleaved"
UNBOUNDED = SearchBudget(10_000_000, 100_000_000, 40, 2)
EAST, SOUTH_EAST, SOUTH = -math.pi / 2, -math.pi / 4, 0.
MODES = ((1., False, False), (1., True, False), (0., False, False), (1., False, True), (1., True, True))
TURN15 = tuple(TickInput(f, 0., j, False, s, yaw) for yaw in (EAST, SOUTH_EAST, SOUTH) for f, j, s in MODES)


def moving(command):
    return command.forward != 0. or command.strafe != 0.


def heading_error(state, command, goal):
    region = goal.region
    dx = (region.min_x + region.max_x) / 2. - state.position[0]
    dz = (region.min_z + region.max_z) / 2. - state.position[2]
    wanted = math.atan2(-dx, dz)  # physics: vx = -sin(yaw), vz = cos(yaw)
    return abs(math.remainder(command.movement_yaw_radians - wanted, math.tau))


def template_search(request, world):
    request = replace(request, budget=UNBOUNDED)
    counter = CountedPhysics(UNBOUNDED)
    goal = request.goal
    floor = min(request.anchor_state.position[1], goal.region.min_y)
    brake = next(c for c in request.supported_inputs if not moving(c) and not c.jump
                 and c.movement_yaw_radians == goal.required_yaw_radians)
    ground = tuple(c for c in request.supported_inputs if not c.jump)
    jumps = tuple(c for c in request.supported_inputs if c.jump)
    stats = {"completions": 0, "scans": 0}

    def ordered(states, commands):
        return sorted(commands, key=lambda c: (not moving(c), heading_error(states[0], c, goal),
                                               request.supported_inputs.index(c)))

    def roll(states, command):
        nxt = []
        for state in states:
            result = counter.step(state, command, world)
            if (result.status is not CalculationStatus.OK or result.next_state.horizontal_collision
                    or result.next_state.position[1] < floor - 1.e-7):
                return None
            nxt.append(result.next_state)
        return tuple(nxt)

    def complete(states, inputs):
        stats["completions"] += 1
        while len(inputs) < 40:
            states = roll(states, brake)
            if states is None:
                return None
            inputs += (brake,)
            if all(s.on_ground and math.hypot(s.velocity_blocks_per_tick[0], s.velocity_blocks_per_tick[2])
                   <= ZERO_SPEED_EPSILON for s in states):
                if not all(_distance(s, goal) == 0. for s in states):
                    return None
                stats["scans"] += 1
                scan = scan_commitment(request, inputs, world, _boundary_evidence(request, inputs),
                                       counter=counter)
                if scan.status is not ScanStatus.VERIFIED_CANDIDATE:
                    return None
                checks = [_goal_check(request, b.timing_branch, b.states[-1], world, counter)[0]
                          for b in scan.proof.branches]
                return inputs if all(c is not None and c.accepted for c in checks) else None
        return None

    def jump_subgrid(states, inputs, j):
        lifted = roll(states, j)
        if lifted is None:
            return None
        for a in ordered(lifted, ground):
            flying, flown = lifted, inputs + (j,)
            for k2 in range(17):
                if k2:
                    flying = roll(flying, a)
                    if flying is None or len(flown) >= 40:
                        break
                    flown += (a,)
                found = complete(flying, flown)
                if found:
                    return found
        return None

    entry = request.entry_states
    if ORDER == "interleaved":
        for g in ordered(entry, ground):
            states, inputs = entry, ()
            for k1 in range(21):
                if k1:
                    states = roll(states, g)
                    if states is None or len(inputs) >= 40:
                        break
                    inputs += (g,)
                found = complete(states, inputs)
                if found:
                    return found, counter, stats
                for j in ordered(states, jumps):
                    found = jump_subgrid(states, inputs, j)
                    if found:
                        return found, counter, stats
        return None, counter, stats
    if ORDER == "steer":
        def gait(c):
            return (c.forward, c.strafe, c.jump, c.sneak, c.sprint)
        gaits = []
        for c in request.supported_inputs:
            if gait(c) not in gaits:
                gaits.append(gait(c))
        ground_gaits = sorted((g for g in gaits if not g[2]), key=lambda g: (g[0] == 0. and g[1] == 0.,
                              gaits.index(g)))
        jump_gaits = [g for g in gaits if g[2]]

        def steer(states, g):
            options = [c for c in request.supported_inputs if gait(c) == g]
            return min(options, key=lambda c: (heading_error(states[0], c, goal),
                                               request.supported_inputs.index(c)))

        def hold(states, inputs, g, ticks):
            path = [(states, inputs)]
            for _ in range(ticks):
                if len(inputs) >= 40:
                    break
                command = steer(states, g)
                states = roll(states, command)
                if states is None:
                    break
                inputs += (command,)
                path.append((states, inputs))
            return path

        runs = [hold(entry, (), g, 20) for g in ground_gaits]
        for path in runs:
            for states, inputs in path:
                found = complete(states, inputs)
                if found:
                    return found, counter, stats
        for path in runs:
            for states, inputs in reversed(path):
                for jg in jump_gaits:
                    j = steer(states, jg)
                    lifted = roll(states, j)
                    if lifted is None:
                        continue
                    for ag in ground_gaits:
                        for flying, flown in hold(lifted, inputs + (j,), ag, 16):
                            found = complete(flying, flown)
                            if found:
                                return found, counter, stats
        return None, counter, stats
    # "simple-first": every no-jump candidate first, then jumps from the latest takeoff back.
    runs = []
    for g in ordered(entry, ground):
        states, inputs, path = entry, (), [(entry, ())]
        for k1 in range(1, 21):
            states = roll(states, g)
            if states is None or len(inputs) >= 40:
                break
            inputs += (g,)
            path.append((states, inputs))
        runs.append(path)
        for states, inputs in path:
            found = complete(states, inputs)
            if found:
                return found, counter, stats
    for path in runs:
        for states, inputs in reversed(path):
            for j in ordered(states, jumps):
                found = jump_subgrid(states, inputs, j)
                if found:
                    return found, counter, stats
    return None, counter, stats


def turn_case(platform):
    """Entry walking east from the representative anchor; goal three blocks south."""
    base = representative_fixture("flat_walk")
    stamp = base.world.cell((0, 0, 0)).stamp
    facts = {}
    for x in range(-3, 4):
        for y in range(-5, 7):
            for z in range(-3, 13):
                solid = y == 0 or (platform and y == 1 and z >= 3)
                facts[(x, y, z)] = CellFact(
                    CellKnowledge.BLOCK if solid else CellKnowledge.AIR, stamp,
                    BlockGeometry.full_cube("minecraft:stone") if solid else None)
    world = PhysicsWorldView(WorldView.detached(base.world.session, 3, 0, facts), JAVA_1_21_RULESET)
    walk_east = TickInput(1., 0., False, False, False, EAST)
    neutral_east = TickInput(0., 0., False, False, False, EAST)
    anchor = base.request.anchor_state
    for _ in range(8):
        anchor = step(anchor, walk_east, world, JAVA_1_21_RULESET).next_state
    late = step(anchor, neutral_east, world, JAVA_1_21_RULESET).next_state
    tick = anchor.movement_tick_id
    prelude = KnownInputApplication(base.request.world_session, 99, tick + 1, neutral_east,
                                    "known-wait-99", ApplicationEvidence.PREDICTED)
    height = 2. if platform else 1.
    goal = replace(base.request.goal, region=Aabb(2.35, height, 3.3, 2.65, height + .05, 4.3))
    request = replace(base.request, request_id=f"turn-{'up' if platform else 'flat'}",
                      entry_states=(anchor, late), allowed_effect_ticks=(tick + 1, tick + 2),
                      branch_preludes=((), (prelude,)), goal=goal, supported_inputs=TURN15,
                      route_guidance=((2.5, height, 3.5),))
    return request, world


def flat_sprint_case():
    base = representative_fixture("flat_walk")
    goal = replace(base.request.goal, region=Aabb(.35, 1., 5.3, .65, 1.05, 6.3))
    return replace(base.request, request_id="flat-sprint", goal=goal,
                   supported_inputs=sp.ALPHABETS["A5 +sprint/sprint_jump"]), base.world


def cases():
    alphabets = (("A3", sp.ALPHABETS["A3 walk/jump/neutral"]),
                 ("A5", sp.ALPHABETS["A5 +sprint/sprint_jump"]), ("A15", ALPHABET15))
    for name in ("flat_walk", "jump_up_straight", "jump_gap_1"):
        fixture = representative_fixture(name)
        yield f"repr {name}", fixture.request, fixture.world
    for start in (1, 4):
        for width in (1, 2, 3):
            if start == 1 and width == 3:
                continue
            for label, alphabet in alphabets:
                request, world, goal = sp.gap_case(width, start=start)
                yield (f"gap start{start} w{width} {label}",
                       replace(request, goal=goal, supported_inputs=alphabet), world)
    yield ("flat_sprint", *flat_sprint_case())
    yield ("turn_90", *turn_case(False))
    yield ("jump_up_after_turn", *turn_case(True))


if __name__ == "__main__":
    import sys
    ORDER = sys.argv[1] if len(sys.argv) > 1 else "interleaved"
    print("order:", ORDER)
    for name, request, world in cases():
        started = time.perf_counter()
        found, counter, stats = template_search(request, world)
        elapsed = (time.perf_counter() - started) * 1000.
        shape = "—" if found is None else " ".join(
            f"{'J' if c.jump else ('S' if c.sprint else ('W' if c.forward else 'N'))}"
            f"{round(math.degrees(c.movement_yaw_radians))}x{len(tuple(g))}"
            for c, g in itertools.groupby(found))
        print(f"{name:28s} {'FOUND' if found else 'none in grid':12s} "
              f"ticks={0 if found is None else len(found):2d} completions={stats['completions']} "
              f"scans={stats['scans']} steps={counter.physics_steps} {elapsed:.0f} ms  {shape}", flush=True)
