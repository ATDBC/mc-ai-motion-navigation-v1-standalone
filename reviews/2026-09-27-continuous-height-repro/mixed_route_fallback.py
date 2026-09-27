"""Round-16 review: does a route that mixes small rises with another action keep
the continuous varying-height Walk?

Lane along +z on the live planner (plan_known_surface_snapshot) with a real
entry physics state:  flat, flat, bottom slab (+0.5), full block (+0.5), then a
one-block drop back to the floor, flat.  The same lane without the drop is the
control.

Run from the repository root of a 59c45bb checkout, with height_transition_cost.py
(round-15 review) next to this script:
    PYTHONPATH=. python -B <this script>
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import (  # noqa: E402
    CONFIG, GRASS, SESSION, SLAB, build_world, initial_state, top,
)
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SurfacePlanningRequest, plan_known_surface_snapshot,
)
from mc2p.motion_nav.step_transition import load_step_profile
from mc2p.motion_nav.support_surfaces import query_support_surfaces

environment = load_frozen_environment(CONFIG / "environment-v1.json")
catalog = BlockMotionCatalog.load(CONFIG / "block-motion-traits-v1.json",
                                  CONFIG / "vanilla-block-registry-1_21.json")
ground = load_ground_motion_profile(CONFIG / "ordinary-ground-b07-v1.json", environment=environment, catalog=catalog)
step_profile = load_step_profile(CONFIG / "step-b07-v1.json", environment=environment)
jump = load_jump_up_profile(CONFIG / "jump-up-b06-v1.json", environment=environment, catalog=catalog)
air = load_air_motion_profiles(CONFIG / "air-motions-b09-v1.json", environment=environment, catalog=catalog)

LANES = {
    "rises only (control)": ([63], [63], [63, (64, SLAB)], [63, 64], [63, 64], [63, 64]),
    "rises then a one-block drop": ([63], [63], [63, (64, SLAB)], [63, 64], [63], [63]),
}
for name, columns in LANES.items():
    world = build_world(columns)
    start = (.5, top(columns, 0), .5)
    goal = (.5, top(columns, len(columns) - 1), len(columns) - .5)
    frame, state = initial_state(world, start)

    def node(position):
        result = query_support_surfaces(frame.world, math.floor(position[0]), math.floor(position[2]),
                                        position[1] - .1, position[1] + .1)
        return min(result.surfaces, key=lambda s: abs(s.position[1] - position[1])).node_id

    snapshot = KnownMapSnapshotBuilder(frame.world, KnownMapBounds(-3, 3, 63, 66, -1, len(columns), True, extra_top_clearance_cells=2)
                                       ).advance(frame.world, 100_000).snapshot
    candidate = plan_known_surface_snapshot(
        snapshot, ground, step_profile,
        SurfacePlanningRequest(1, "r", "g", 1, SESSION.value, node(start), node(goal),
                               entry_physics_state=state),
        jump, air_profiles=air)
    kinds = [type(edge).__name__.replace("Surface", "").replace("Edge", "") for edge in candidate.segments]
    print(f"{name}: {candidate.status.value}; edges {kinds}; "
          f"ground traversal proofs {len(candidate.ground_traversal_plans)}; "
          f"reasons {candidate.reasons}")
    for edge, node_after in zip(candidate.segments, candidate.path[1:]):
        print(f"    {type(edge).__name__:<26} -> column ({node_after.node_id.column_x},{node_after.node_id.column_z}) "
              f"y={node_after.position[1]:.2f} proof_required={getattr(edge, 'requires_ground_traversal_proof', '-')}")

# What happens to the mixed-route candidate downstream?
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor  # noqa: E402
from mc2p.motion_nav.route_admission import RouteAdmitter  # noqa: E402
admission = RouteAdmitter().admit_surface(candidate, frame, expected_request_id="r", goal_id="g",
                                          goal_revision=1, changed_cells=())
print("admission of the mixed route:", admission.status.value, admission.reason)
if admission.route is not None:
    route = admission.route.action_route
    print("  actions:", [type(a).__name__ for a in route.actions])
    try:
        ActionRouteExecutor(ground, jump, step_profile, air_profiles=air).start(route, frame)
        print("  executor start: ok")
    except Exception as error:  # noqa: BLE001
        print("  executor start raised:", type(error).__name__, error)
