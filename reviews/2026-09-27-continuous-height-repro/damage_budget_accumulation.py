"""Round-16 review: is the task damage budget a per-task total or a per-drop cap?

Lane along +z on the live planner (plan_known_surface_snapshot):
    start platform top 74, drop 5 to top 69, walk one block, drop 5 to top 64.
Each 5-block drop is predicted at conservative_plain_fall_damage_points(5) = 2.
The request allows a task damage budget of 2 points.

If the budget were a task total, the route could spend at most 2 points; the
planner would have to reject the second drop (or the whole route).

Run from the repository root of a 59c45bb checkout, with height_transition_cost.py
(this directory's copy, known air up to y=79) next to this script:
    PYTHONPATH=. python -B <this script>
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import CONFIG, SESSION, build_world, initial_state, top  # noqa: E402
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SurfacePlanningRequest, plan_known_surface_snapshot,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.step_transition import load_step_profile
from mc2p.motion_nav.support_surfaces import query_support_surfaces

environment = load_frozen_environment(CONFIG / "environment-v1.json")
catalog = BlockMotionCatalog.load(CONFIG / "block-motion-traits-v1.json",
                                  CONFIG / "vanilla-block-registry-1_21.json")
ground = load_ground_motion_profile(CONFIG / "ordinary-ground-b07-v1.json", environment=environment, catalog=catalog)
step_profile = load_step_profile(CONFIG / "step-b07-v1.json", environment=environment)
jump = load_jump_up_profile(CONFIG / "jump-up-b06-v1.json", environment=environment, catalog=catalog)
air = load_air_motion_profiles(CONFIG / "air-motions-b09-v1.json", environment=environment, catalog=catalog)

columns = (list(range(63, 74)), list(range(63, 69)), list(range(63, 69)), [63], [63])
world = build_world(columns)
start = (.5, top(columns, 0), .5)
goal = (.5, top(columns, len(columns) - 1), len(columns) - .5)
frame, state = initial_state(world, start)


def node(position):
    result = query_support_surfaces(frame.world, math.floor(position[0]), math.floor(position[2]),
                                    position[1] - .1, position[1] + .1)
    return min(result.surfaces, key=lambda s: abs(s.position[1] - position[1])).node_id


snapshot = KnownMapSnapshotBuilder(
    frame.world, KnownMapBounds(-3, 3, 63, 76, -1, len(columns), True, extra_top_clearance_cells=2),
).advance(frame.world, 1_000_000).snapshot
for points in (0.0, 2.0, 4.0):
    budget = TaskDamageBudget(maximum_expected_damage_points=points, risk_policy_id=f"budget-{points:g}")
    candidate = plan_known_surface_snapshot(
        snapshot, ground, step_profile,
        SurfacePlanningRequest(1, "r", "g", 1, SESSION.value, node(start), node(goal),
                               entry_physics_state=state, damage_budget=budget),
        jump, air_profiles=air)
    drops = [
        (before.position[1], after.position[1])
        for edge, before, after in zip(candidate.segments, candidate.path, candidate.path[1:])
        if type(edge).__name__ == "SurfaceControlledDropEdge"
    ]
    total = sum(max(0, math.ceil(a - b - 3.0)) for a, b in drops)
    print(f"budget {points:g}: {candidate.status.value}; drops {[f'{a:g}->{b:g}' for a, b in drops]}; "
          f"sum of per-drop conservative damage {total}")
