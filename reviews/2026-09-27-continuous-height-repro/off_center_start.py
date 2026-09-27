"""Round-16 review: does a varying-height Walk survive a start that is not the node centre?

Same control lane as mixed_route_fallback.py (flat, flat, bottom slab, full block,
flat, flat -> two half-block rises), same live planner and RouteAdmitter.  Only the
body start changes: exactly at the start node centre, then offset by a few
centimetres (well inside the 0.25 endpoint tolerance a previous route may stop at).

Run from the repository root of a 59c45bb checkout, with height_transition_cost.py
next to this script:
    PYTHONPATH=. python -B <this script>
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import CONFIG, SESSION, SLAB, build_world, initial_state, top  # noqa: E402
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SurfacePlanningRequest, plan_known_surface_snapshot,
)
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
goal = (.5, top(columns, len(columns) - 1), len(columns) - .5)
for offset in ((0.0, 0.0), (0.05, 0.0), (0.12, -0.09)):
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
    print(f"start offset {offset}: plan {candidate.status.value}, proofs {len(candidate.ground_traversal_plans)}; "
          f"admission {admission.status.value} {admission.reason}")
