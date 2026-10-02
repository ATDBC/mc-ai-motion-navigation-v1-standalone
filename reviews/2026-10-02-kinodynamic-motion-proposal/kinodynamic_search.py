"""Prototype: velocity-aware search over the project's 1.21 calculator (run from a 7348b64 checkout).

Not production code.  It answers three questions for the design proposal:
  1. Can a plain best-first search over short input primitives, using the existing calculator as
     the transition model, find run-up + jump sequences (gaps of 1-4 blocks, chained gaps, a step up
     at speed, a turn into a gap) without any per-action transition rules?
  2. How much does it cost in Python (expansions, calculator steps, wall time)?
  3. What does "robust to one late tick" cost, when every jump is also simulated one tick late and
     must still land safely?

State key: position (0.1 block), feet height (0.05), velocity (0.02 block/tick), on_ground, sprinting.
Primitives on the ground, for a few yaw choices: sprint 1 tick, sprint 3 ticks, sprint-jump (jump on
the first tick, then hold sprint-forward until landing), walk 2 ticks, release 2 ticks.  Walking off
an edge keeps the input until landing.  Cost is game ticks; the heuristic is remaining horizontal
distance / 0.45 block per tick, weighted 1.5 (weighted A*, so plans are near- not exactly optimal).

    PYTHONPATH=. python -B kinodynamic_search.py
"""
from __future__ import annotations

import heapq
import itertools
import math
import time
from dataclasses import dataclass

from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from tests.sim.backend import CalculatorBackend, Scene

STONE = "minecraft:stone"
FLOOR_Y = 64.0
MAX_AIR_TICKS = 40
H_SPEED = 0.45          # block/tick, optimistic horizontal speed for the heuristic
WEIGHT = 1.5
KINDS = ("sprint1", "sprint3", "jump", "walk", "release")        # walk-only variant: see WALK_KINDS
WALK_KINDS = ("walk1", "walk3", "walkjump", "release")


class Counter:
    steps = 0


def tick(state, inp, world):
    Counter.steps += 1
    result = step(state, inp, world, JAVA_1_21_RULESET)
    return result.next_state


def key(s):
    x, y, z = s.position
    vx, vy, vz = s.velocity_blocks_per_tick
    return (round(x * 10), round(y * 20), round(z * 10), round(vx * 50), round(vy * 20), round(vz * 50),
            s.on_ground, s.sprinting)


@dataclass
class Course:
    name: str
    solids: dict
    start: tuple
    goal_box: tuple          # (min_x, max_x, min_z, max_z, feet_y)
    bounds: tuple

    def scene(self):
        return Scene(dict(self.solids), self.bounds)


def lane(length, gaps=(), steps_up=(), width=3, x0=-1):
    """Floor y=63 along +z; gaps = [(z_start, length)]; steps_up = [z_from] raise floor by one from z."""
    solids = {}
    raised = 0
    for z in range(-2, length):
        if any(z >= s for s in steps_up):
            raised = sum(1 for s in steps_up if z >= s)
        if any(g <= z < g + n for g, n in gaps):
            continue
        for x in range(x0, x0 + width):
            for y in range(63, 64 + raised):
                solids[(x, y, z)] = STONE
    return solids


def courses():
    out = []
    out.append(Course("flat 20", lane(26), (0.5, 64.0, 0.5), (-1, 2, 20, 21, 64.0), ((-3, 3), (60, 70), (-3, 27))))
    for gap in (1, 2, 3, 4):
        out.append(Course(f"gap {gap}", lane(24, gaps=[(10, gap)]), (0.5, 64.0, 0.5),
                          (-1, 2, 10 + gap + 3, 10 + gap + 4, 64.0), ((-3, 3), (55, 70), (-3, 25))))
    out.append(Course("gap 3 + 4 floor + gap 3", lane(30, gaps=[(8, 3), (15, 3)]), (0.5, 64.0, 0.5),
                      (-1, 2, 22, 23, 64.0), ((-3, 3), (55, 70), (-3, 31))))
    out.append(Course("step up 1 at speed", lane(24, steps_up=[10]), (0.5, 64.0, 0.5),
                      (-1, 2, 16, 17, 65.0), ((-3, 3), (60, 70), (-3, 25))))
    solids = {(x, 63, z): STONE for x in range(-1, 2) for z in range(-2, 8)}          # run north...
    solids.update({(x, 63, z): STONE for x in range(-1, 12) for z in range(5, 8)})    # ...then east
    solids.update({(x, 63, z): STONE for x in range(15, 20) for z in range(5, 8)})    # gap 3 eastward
    out.append(Course("turn then gap 3", solids, (0.5, 64.0, 0.5), (17, 18, 5, 8, 64.0), ((-3, 21), (55, 70), (-3, 10))))
    return out


def yaw_choices(state, goal_box):
    gx = (goal_box[0] + goal_box[1]) / 2
    gz = (goal_box[2] + goal_box[3]) / 2
    x, _, z = state.position
    toward = math.atan2(-(gx - x), gz - z)            # Minecraft yaw: +z is 0, +x is -90 degrees
    snapped = round(toward / (math.pi / 4)) * (math.pi / 4)
    current = round(state.yaw_radians / (math.pi / 4)) * (math.pi / 4)
    values = {round(v, 6) for v in (toward, snapped, current, current + math.pi / 4, current - math.pi / 4)}
    return sorted(values)


def run_primitive(state, world, kind, yaw, *, late_jump=False):
    """Returns (final_state, inputs, alive)."""
    sprint = TickInput(1.0, 0.0, False, False, True, yaw)
    inputs = []
    s = state
    if kind == "release":
        seq = [TickInput(0.0, 0.0, False, False, False, yaw)] * 2
    elif kind == "walk":
        seq = [TickInput(1.0, 0.0, False, False, False, yaw)] * 2
    elif kind == "sprint1":
        seq = [sprint]
    elif kind == "sprint3":
        seq = [sprint] * 3
    elif kind == "walk1":
        seq = [TickInput(1.0, 0.0, False, False, False, yaw)]
    elif kind == "walk3":
        seq = [TickInput(1.0, 0.0, False, False, False, yaw)] * 3
    elif kind == "jump":
        jump = TickInput(1.0, 0.0, True, False, True, yaw)
        seq = [sprint, jump] if late_jump else [jump]
    elif kind == "walkjump":
        walk = TickInput(1.0, 0.0, False, False, False, yaw)
        jump = TickInput(1.0, 0.0, True, False, False, yaw)
        seq = [walk, jump] if late_jump else [jump]
        sprint = walk
    for inp in seq:
        s = tick(s, inp, world)
        if s is None:
            return None, inputs, False
        inputs.append(inp)
    hold = seq[-1] if kind not in ("jump", "walkjump") else sprint
    air = 0
    while not s.on_ground and air < MAX_AIR_TICKS:            # finish any flight with the same keys
        s = tick(s, hold, world)
        if s is None:
            return None, inputs, False
        inputs.append(hold)
        air += 1
    alive = s.on_ground and s.position[1] >= FLOOR_Y - 0.01 and s.fall_distance_blocks <= 3.0
    return s, inputs, alive


def in_goal(s, box):
    x, y, z = s.position
    return s.on_ground and box[0] <= x <= box[1] and box[2] <= z <= box[3] and abs(y - box[4]) < 0.01


def heuristic(s, box):
    x, _, z = s.position
    dx = max(box[0] - x, 0.0, x - box[1])
    dz = max(box[2] - z, 0.0, z - box[3])
    return math.hypot(dx, dz) / H_SPEED


def search(course, *, robust=False, budget_s=30.0):
    backend = CalculatorBackend([0], course.scene(), course.start)
    world, start = backend.world, backend.state
    Counter.steps = 0
    t0 = time.perf_counter()
    tie = itertools.count()
    frontier = [(WEIGHT * heuristic(start, course.goal_box), next(tie), 0, start, ())]
    seen = {key(start): 0}
    expanded = 0
    rejected_late = 0
    while frontier:
        if time.perf_counter() - t0 > budget_s:
            return dict(found=False, reason="budget", expanded=expanded, steps=Counter.steps,
                        ms=(time.perf_counter() - t0) * 1000, rejected_late=rejected_late)
        _, _, g, s, plan = heapq.heappop(frontier)
        if in_goal(s, course.goal_box):
            return dict(found=True, ticks=g, plan=plan, expanded=expanded, steps=Counter.steps,
                        ms=(time.perf_counter() - t0) * 1000, rejected_late=rejected_late, world=world, start=start)
        expanded += 1
        for yaw in yaw_choices(s, course.goal_box):
            for kind in KINDS:
                if kind in ("jump", "walkjump") and not s.on_ground:
                    continue
                n, inputs, alive = run_primitive(s, world, kind, yaw)
                if not alive:
                    continue
                if robust and kind in ("jump", "walkjump"):
                    _, _, late_alive = run_primitive(s, world, kind, yaw, late_jump=True)
                    if not late_alive:
                        rejected_late += 1
                        continue
                k = key(n)
                cost = g + len(inputs)
                if seen.get(k, math.inf) <= cost:
                    continue
                seen[k] = cost
                heapq.heappush(frontier, (cost + WEIGHT * heuristic(n, course.goal_box), next(tie), cost, n,
                                          plan + ((kind, round(math.degrees(yaw)), len(inputs)),)))
    return dict(found=False, reason="exhausted", expanded=expanded, steps=Counter.steps,
                ms=(time.perf_counter() - t0) * 1000, rejected_late=rejected_late)


def replay(world, start, plan, course, *, delay_jump_index=None):
    s = start
    for index, (kind, yaw_deg, _) in enumerate(plan):
        s, _, alive = run_primitive(s, world, kind, math.radians(yaw_deg),
                                    late_jump=(index == delay_jump_index))
        if not alive:
            return False
    return in_goal(s, course.goal_box)


def summarize(plan):
    out = []
    for kind, yaw, n in plan:
        if out and out[-1][0] == kind and out[-1][1] == yaw:
            out[-1] = (kind, yaw, out[-1][2] + n)
        else:
            out.append((kind, yaw, n))
    return " ".join(f"{k}@{y}x{n}" for k, y, n in out)


if __name__ == "__main__":
    print(f"calculator step cost reference: see 'steps' and 'ms' columns (weighted A*, w={WEIGHT})\n")
    for course in courses():
        for robust in (False, True):
            r = search(course, robust=robust)
            label = f"{course.name:<26}{'robust' if robust else 'nominal':<9}"
            if not r["found"]:
                print(f"{label} NOT FOUND ({r['reason']}) expanded={r['expanded']} steps={r['steps']} "
                      f"ms={r['ms']:.0f} late-rejected jumps={r['rejected_late']}")
                continue
            plan = r["plan"]
            jumps = [i for i, p in enumerate(plan) if p[0] in ("jump", "walkjump")]
            late_ok = sum(replay(r["world"], r["start"], plan, course, delay_jump_index=i) for i in jumps)
            print(f"{label} ticks={r['ticks']:<4} expanded={r['expanded']:<6} steps={r['steps']:<7} "
                  f"ms={r['ms']:<7.0f} replay={'ok' if replay(r['world'], r['start'], plan, course) else 'FAIL'} "
                  f"jumps={len(jumps)} survive-1-tick-late={late_ok}/{len(jumps)}")
            print(f"{'':<35}{summarize(plan)}")
