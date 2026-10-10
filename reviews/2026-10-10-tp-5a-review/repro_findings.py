"""TP Task 5A 审查探针：复现独立审查的三项发现。

运行：在仓库根目录 PYTHONPATH=. python <本文件>
只读运行，不修改原型源码。
"""
from dataclasses import replace

from experiments.motion_navigation.trajectory_proto.commitment import TailStatus
from experiments.motion_navigation.trajectory_proto.reference_search import reference_search
from experiments.motion_navigation.trajectory_proto.scenarios import representative_fixture
from mc2p.motion_nav.geometry import query_support, required_cells_for_sweep
from mc2p.motion_nav.movement_transition import ResourceState
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.world_model import Aabb, CellFact, CellKnowledge, WorldView


def support_fraction(state, world):
    cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
    facts = {p: world.cell(p) for p in cells}
    view = WorldView.detached(world.session, world.geometry_revision, 0, facts)
    return query_support(state.body_box, view).support_fraction


def p1_unclosed_risk():
    print("== P1 未关闭风险区间被丢弃")
    fixture = representative_fixture("jump_gap_1")
    original = reference_search(fixture.request, fixture.world)
    facts = {}
    for x in range(-3, 4):
        for y in range(-5, 7):
            for z in range(-3, 13):
                fact = fixture.world.cell((x, y, z))
                if y == 0 and z >= 2 and x >= 1:
                    fact = CellFact(CellKnowledge.AIR, fact.stamp)
                facts[(x, y, z)] = fact
    world = PhysicsWorldView(
        WorldView.detached(fixture.world.session, 3, 0, facts), JAVA_1_21_RULESET)
    anchor = replace(fixture.request.anchor_state, position=(1.25, 1., .5))
    late = step(anchor, fixture.request.branch_preludes[1][0].tick_input,
                world, JAVA_1_21_RULESET).next_state
    goal = replace(fixture.request.goal, region=Aabb(1.1, 1., 2.3, 1.4, 1.05, 3.3))
    request = replace(
        fixture.request, entry_states=(anchor, late), goal=goal,
        input_prefix=original.inputs,
        budget=replace(fixture.request.budget, max_trajectory_ticks=len(original.inputs)))
    result = reference_search(request, world)
    print("status", result.status.value, result.reason.value, "counts", result.counts)
    if result.proof is None:
        return
    for branch in result.proof.branches:
        unsafe = [t.boundary for t in branch.tails if t.status is TailStatus.UNSAFE]
        print(f"  {branch.timing_branch.value:14s} final={branch.states[-1].position} "
              f"support={support_fraction(branch.states[-1], world):.4f} "
              f"unsafe_boundaries={unsafe} risk_intervals={branch.risk_intervals}")


def p2_conditional_resources():
    print("== P2 条件资源被当成已证明")
    fixture = representative_fixture("jump_up_straight")
    anchor = replace(fixture.request.anchor_state, saturation_points=0.)
    late = step(anchor, fixture.request.branch_preludes[1][0].tick_input,
                fixture.world, JAVA_1_21_RULESET).next_state
    goal = replace(fixture.request.goal,
                   minimum_resources=ResourceState((("food_points", 20.),)))
    request = replace(fixture.request, entry_states=(anchor, late), goal=goal)
    result = reference_search(request, fixture.world)
    print("status", result.status.value, result.reason.value)
    current = anchor
    statuses = set()
    exhaustion = 0.
    for command in result.inputs:
        calc = step(current, command, fixture.world, JAVA_1_21_RULESET)
        update = calc.resource_update
        statuses.add((update.status.value, update.incomplete_reasons))
        exhaustion += update.exhaustion_delta
        current = calc.next_state
    print("  final food", current.food_points, "saturation", current.saturation_points,
          "summed exhaustion_delta", round(exhaustion, 6))
    print("  goal accepted per branch", [c.accepted for c in result.goal_checks])
    print("  per-step resource status", sorted(map(str, statuses)))


def dedup_nominal_only():
    print("== 去重只看 nominal 状态")
    fixture = representative_fixture("flat_walk")
    walk = TickInput(1., 0., False, False, False, 0.)
    jump = TickInput(1., 0., True, False, False, 0.)
    neutral = TickInput(0., 0., False, False, False, 0.)
    anchor = replace(fixture.request.anchor_state, jumping_cooldown_ticks=2)
    late = step(anchor, neutral, fixture.world, JAVA_1_21_RULESET).next_state

    def run(entry, prefix):
        state = entry
        for command in prefix:
            state = step(state, command, fixture.world, JAVA_1_21_RULESET).next_state
        return state

    a_nom, b_nom = run(anchor, (jump, neutral)), run(anchor, (walk, neutral))
    a_late, b_late = run(late, (jump, neutral)), run(late, (walk, neutral))
    print("  nominal equal:", a_nom == b_nom)
    print("  late equal:", a_late == b_late, "late y:", a_late.position[1], b_late.position[1])


if __name__ == "__main__":
    p1_unclosed_risk()
    p2_conditional_resources()
    dedup_nominal_only()
