"""Frozen layouts for the formal-path navigation coordination matrix.

Each scenario states what a correct navigation stack should do (`expect`) and
the monitor adds invariant violations.  Run from the repository root:
    PYTHONPATH=.:<this directory> python -B <this directory>/scenarios.py [name ...]
"""
from __future__ import annotations

import sys
import time

from tests.sim.backend import Perturbations, SLAB_ID, Scene
from tests.sim.runner import Event, Scenario, lane, late_ticks, run, _goal
from mc2p.motion_nav.motion_risk import TaskDamageBudget

STONE = "minecraft:stone"


def columns(tops):
    """Solid stone from y=63 up to (not including) each column's top."""
    return [list(range(63, top)) for top in tops]


def half_steps():
    return [[63], [63], [63, (64, SLAB_ID)], [63, 64], [63, 64], [63, (64, SLAB_ID)], [63], [63]]


def l_walkway_with_drop():
    solids = {(0, 63, z): STONE for z in range(0, 7)}
    solids.update({(x, 63, 6): STONE for x in range(0, 7)})
    solids[(7, 60, 6)] = STONE
    return Scene(solids, ((-3, 10), (52, 72), (-3, 9)))


def terrace():
    solids = {}
    for z in range(0, 3):
        solids[(0, 63, z)] = STONE
    for z in range(3, 9):
        solids[(0, 60, z)] = STONE
    for z in range(9, 11):
        solids[(0, 57, z)] = STONE
    return Scene(solids, ((-3, 3), (52, 72), (-3, 13)))


def terrace_three():
    solids = {}
    for z in range(0, 3):
        solids[(0, 63, z)] = STONE
    for z in range(3, 7):
        solids[(0, 60, z)] = STONE
    for z in range(7, 11):
        solids[(0, 57, z)] = STONE
    for z in range(11, 14):
        solids[(0, 54, z)] = STONE
    return Scene(solids, ((-3, 3), (50, 72), (-3, 16)))


def drop_ledge(height, width=3):
    """Three-wide platform z=0..2 at feet 64, then a `height`-block drop onto z=3..5."""
    solids = {}
    for x in range(-(width // 2), width - width // 2):
        for z in range(0, 3):
            solids[(x, 63, z)] = STONE
        for z in range(3, 6):
            solids[(x, 63 - height, z)] = STONE
    return Scene(solids, ((-3, 3), (52, 72), (-3, 8)))


def probe_active(context) -> bool:
    diagnostics = context.diagnostics
    return ("landing_edge_probe" in diagnostics.controller_ids
            and context.diagnostics.state.value == "needs_information")


def revise_goal_back(context) -> None:
    revised_position = (.5, 64.0, .5)
    revised_goal = _goal(revised_position, context.risk_policy_id)
    context.driver.replace_goal("goal", 2, revised_goal,
                                context.clock[0],
                                damage_budget=TaskDamageBudget(
                                    context.risk_policy_id, context.damage_points))
    context.goal_state = revised_goal
    context.goal_position = revised_position


def cancel_task(context) -> None:
    context.driver.release("harness_cancel")


def airborne_in_drop(context) -> bool:
    return (not context.backend.state.on_ground
            and "route_executor" in context.diagnostics.controller_ids)


SCENARIOS = [
    Scenario("flat_walk", lane([[63]] * 10, width=3), (.5, 64.0, .5), (.5, 64.0, 8.5)),
    Scenario("flat_walk_offset_start", lane([[63]] * 10, width=3), (.83, 64.0, .21), (.5, 64.0, 8.5)),
    Scenario("goal_on_current_support", lane([[63]] * 6, width=3), (.6, 64.0, .4), (.5, 64.0, .5)),
    Scenario("half_steps_up_down", lane(half_steps(), width=3), (.5, 64.0, .5), (.5, 64.0, 7.5)),
    Scenario("half_steps_20pct_late", lane(half_steps(), width=3), (.5, 64.0, .5), (.5, 64.0, 7.5),
             perturbations=Perturbations(late_ticks=late_ticks(.2, 7))),
    Scenario("stair_descent_4", lane(columns([68, 68, 67, 66, 65, 64, 64]), width=3),
             (.5, 68.0, .5), (.5, 64.0, 6.5)),
    Scenario("direct_drop_2", drop_ledge(2), (.5, 64.0, .5), (.5, 62.0, 4.5)),
    Scenario("direct_drop_5_budget_2", drop_ledge(5), (.5, 64.0, .5), (.5, 59.0, 4.5), damage_points=2.0),
    Scenario("direct_drop_2_goal_revised_during_probe", drop_ledge(2), (.5, 64.0, .5), (.5, 62.0, 4.5),
             events=[Event("revise_goal_back", probe_active, revise_goal_back)]),
    Scenario("direct_drop_2_cancel_in_air", drop_ledge(2), (.5, 64.0, .5), (.5, 62.0, 4.5),
             events=[Event("cancel_in_air", airborne_in_drop, cancel_task)], expect="cancelled"),
    Scenario("direct_drop_2_20pct_late", drop_ledge(2), (.5, 64.0, .5), (.5, 62.0, 4.5),
             perturbations=Perturbations(late_ticks=late_ticks(.2, 11)), expect="failed"),
    Scenario("far_landing_L_walkway", l_walkway_with_drop(), (.5, 64.0, .5), (7.5, 61.0, 6.5)),
    Scenario("far_landing_L_goal_revised_at_30", l_walkway_with_drop(), (.5, 64.0, .5), (7.5, 61.0, 6.5),
             events=[Event("revise_goal_back", lambda c: c.tick >= 30 and probe_active(c), revise_goal_back)]),
    Scenario("terrace_two_ledges", terrace(), (.5, 64.0, .5), (.5, 58.0, 10.5)),
    Scenario("terrace_three_ledges", terrace_three(), (.5, 64.0, .5), (.5, 55.0, 13.5)),
]


def main(names):
    chosen = [s for s in SCENARIOS if not names or s.name in names]
    print(f"{'scenario':<42} {'verdict':<7} {'expect':<9} {'outcome':<18} {'reason':<44} ticks  damage  violations")
    for scenario in chosen:
        started = time.perf_counter()
        try:
            result = run(scenario)
        except Exception as error:  # noqa: BLE001 - an exception is itself a finding
            print(f"{scenario.name:<42} {'FAIL':<7} {scenario.expect:<9} EXCEPTION {type(error).__name__}: {error}")
            continue
        elapsed = time.perf_counter() - started
        violations = "; ".join(f"{name}@{tick}: {detail}" for tick, name, detail in result.violations) or "-"
        print(f"{result.scenario:<42} {result.verdict:<7} {result.expect:<9} {result.outcome:<18} "
              f"{result.reason[:44]:<44} {result.ticks:>5}  {result.damage:>6g}  {violations}"
              f"{'  events ' + ','.join(result.events) if result.events else ''}  [{elapsed:.1f}s]")


if __name__ == "__main__":
    main(sys.argv[1:])
