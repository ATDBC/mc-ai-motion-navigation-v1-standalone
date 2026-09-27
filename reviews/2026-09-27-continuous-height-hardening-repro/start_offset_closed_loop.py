"""Round-17 review: start offsets beyond the tested 0.12 blocks, closed loop.

Lane along +z (flat, flat, bottom slab, full block, flat, flat: two half-block
rises), live planner with the body's real physics state, RouteAdmitter, then the
admitted WalkSegment (its fixed route starts at the body) executed by
FixedRouteController with its traversal proof, closed-loop with physics_1_21.
The body starts anywhere on the start block: centre, 0.12, 0.3, and near the
corners (up to 0.45, 0.45 from the centre).

Run from the repository root of a 5fa2f33 checkout, with height_transition_cost.py
next to this script:
    PYTHONPATH=. python -B <this script>
"""
from dataclasses import replace
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import (  # noqa: E402
    CONFIG, SESSION, SLAB, build_world, frame_from, initial_state, top,
)
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.fixed_route import FixedRouteController, FixedRouteState
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SurfacePlanningRequest, plan_known_surface_snapshot,
)
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.route_admission import RouteAdmitter
from mc2p.motion_nav.step_transition import load_step_profile
from mc2p.motion_nav.support_surfaces import query_support_surfaces

environment = load_frozen_environment(CONFIG / "environment-v1.json")
catalog = BlockMotionCatalog.load(CONFIG / "block-motion-traits-v1.json",
                                  CONFIG / "vanilla-block-registry-1_21.json")
ground = load_ground_motion_profile(CONFIG / "ordinary-ground-b07-v1.json", environment=environment, catalog=catalog)
step_profile = load_step_profile(CONFIG / "step-b07-v1.json", environment=environment)
jump = load_jump_up_profile(CONFIG / "jump-up-b06-v1.json", environment=environment, catalog=catalog)
air = load_air_motion_profiles(CONFIG / "air-motions-b09-v1.json", environment=environment, catalog=catalog)

columns = ([63], [63], [63, (64, SLAB)], [63, 64], [63, 64], [63, 64])
world = build_world(columns)
physics_world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)
goal = (.5, top(columns, len(columns) - 1), len(columns) - .5)
for offset in ((0.0, 0.0), (0.12, 0.0), (0.3, 0.0), (0.0, -0.3), (0.35, 0.35), (0.45, -0.45), (-0.45, 0.45)):
    start = (.5 + offset[0], top(columns, 0), .5 + offset[1])
    frame, state = initial_state(world, start)

    def node(position):
        result = query_support_surfaces(frame.world, math.floor(position[0]), math.floor(position[2]),
                                        position[1] - .1, position[1] + .1)
        return min(result.surfaces, key=lambda s: abs(s.position[1] - position[1])).node_id

    snapshot = KnownMapSnapshotBuilder(
        frame.world, KnownMapBounds(-3, 3, 63, 66, -1, len(columns), True, extra_top_clearance_cells=2),
    ).advance(frame.world, 100_000).snapshot
    candidate = plan_known_surface_snapshot(
        snapshot, ground, step_profile,
        SurfacePlanningRequest(1, "r", "g", 1, SESSION.value, node(start), node(goal),
                               entry_physics_state=state),
        jump, air_profiles=air)
    admission = RouteAdmitter().admit_surface(candidate, frame, expected_request_id="r", goal_id="g",
                                              goal_revision=1, changed_cells=())
    label = f"start offset {str(offset):<13}"
    if admission.route is None:
        print(f"{label}: admission {admission.status.value} {admission.reason}")
        continue
    walk = admission.route.action_route.actions[0]
    controller = FixedRouteController(ground)
    controller.start(walk.fixed_route, frame, traversal_plan=walk.traversal_plan)
    outcome = "timeout"
    for tick in range(1, 120):
        decision = controller.decide(frame_from(state, world, tick))
        if decision.state is FixedRouteState.SUCCEEDED:
            outcome = f"complete in {tick} ticks"
            break
        if decision.state not in {FixedRouteState.RUNNING, FixedRouteState.BRAKING}:
            outcome = f"{decision.state.value}/{decision.reason} at tick {tick}"
            break
        projected = project_movement_command(state, decision.movement)
        state = step(state, projected.tick_input, physics_world, JAVA_1_21_RULESET).next_state
    print(f"{label}: admitted, proofs {len(candidate.ground_traversal_plans)}; execution {outcome}")
