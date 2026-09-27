"""Round-17 review: robustness of the synthesized continuous stair descent.

Lane along +z: a platform, then four one-block steps down, then flat.  The live
planner (with the body's physics state) merges the one-block drops into one
proved Walk run; RouteAdmitter turns it into a WalkSegment; FixedRouteController
executes it closed-loop with physics_1_21.step under:
  nominal; every command 1 tick late; each command 1 tick late with 20%
  probability (100 frozen seeds; a late command leaves the previous keys held);
  yaw held 30 / 60 degrees off the route; start offset (0.3, -0.3).

Run from the repository root of a 5fa2f33 checkout, with height_transition_cost.py
next to this script:
    PYTHONPATH=. python -B <this script>
"""
from dataclasses import replace
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import CONFIG, SESSION, build_world, frame_from, initial_state, top  # noqa: E402
from mc2p.contracts.action_v1 import MovementV1
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

columns = tuple(list(range(63, t)) for t in (68, 68, 67, 66, 65, 64, 64))
world = build_world(columns)
physics_world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)
goal = (.5, top(columns, len(columns) - 1), len(columns) - .5)


def plan(start):
    frame, state = initial_state(world, start)

    def node(position):
        result = query_support_surfaces(frame.world, math.floor(position[0]), math.floor(position[2]),
                                        position[1] - .1, position[1] + .1)
        return min(result.surfaces, key=lambda s: abs(s.position[1] - position[1])).node_id

    snapshot = KnownMapSnapshotBuilder(
        frame.world, KnownMapBounds(-3, 3, 63, 70, -1, len(columns), True, extra_top_clearance_cells=2),
    ).advance(frame.world, 1_000_000).snapshot
    candidate = plan_known_surface_snapshot(
        snapshot, ground, step_profile,
        SurfacePlanningRequest(1, "r", "g", 1, SESSION.value, node(start), node(goal),
                               entry_physics_state=state),
        jump, air_profiles=air)
    admission = RouteAdmitter().admit_surface(candidate, frame, expected_request_id="r", goal_id="g",
                                              goal_revision=1, changed_cells=())
    return frame, state, candidate, admission


def run(*, start=(.5, 68.0, .5), delay=0, late_probability=0.0, seed=0, yaw_offset_deg=0.0):
    frame, state, candidate, admission = plan(start)
    if admission.route is None:
        return f"admission {admission.reason}"
    actions = admission.route.action_route.actions
    if len(actions) != 1 or type(actions[0]).__name__ != "WalkSegment":
        return f"actions {[type(a).__name__ for a in actions]}"
    walk = actions[0]
    heading = state.yaw_radians + math.radians(yaw_offset_deg)
    state = replace(state, yaw_radians=heading)
    controller = FixedRouteController(ground)
    controller.start(walk.fixed_route, frame, traversal_plan=walk.traversal_plan)
    rng, arrivals, pending, held = random.Random(seed), {}, [MovementV1()] * delay, MovementV1()
    for tick in range(1, 160):
        decision = controller.decide(frame_from(state, world, tick))
        if decision.state is FixedRouteState.SUCCEEDED:
            return f"complete in {tick} ticks"
        if decision.state not in {FixedRouteState.RUNNING, FixedRouteState.BRAKING}:
            return f"{decision.state.value}/{decision.reason} at tick {tick}"
        if late_probability:
            arrivals.setdefault(tick + (1 if rng.random() < late_probability else 0), []).append(decision.movement)
            arrived = arrivals.pop(tick, [])
            applied = arrived[-1] if arrived else held
            held = applied
        else:
            pending.append(decision.movement)
            applied = pending.pop(0)
        projected = project_movement_command(state, applied)
        state = step(state, projected.tick_input, physics_world, JAVA_1_21_RULESET).next_state
        state = replace(state, yaw_radians=heading)
    return "timeout"


_, _, candidate, _ = plan((.5, 68.0, .5))
print("planned edges:", [type(e).__name__.replace("Surface", "").replace("Edge", "") for e in candidate.segments],
      "proofs", len(candidate.ground_traversal_plans))
print("nominal                   :", run())
print("every command 1 tick late :", run(delay=1))
results = [run(late_probability=.2, seed=s) for s in range(100)]
print("20% commands 1 tick late  :", f"{sum(r.startswith('complete') for r in results)}/100",
      sorted({r.split(' at ')[0] for r in results if not r.startswith('complete')}))
for offset in (30, 60):
    print(f"yaw held {offset} deg off      :", run(yaw_offset_deg=offset))
print("start offset (0.3, -0.3)  :", run(start=(.8, 68.0, .2)))
