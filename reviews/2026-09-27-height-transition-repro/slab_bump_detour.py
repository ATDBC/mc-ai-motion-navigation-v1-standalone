"""Does a single half-slab in the way make the planner walk around it?

Flat grass area; one bottom slab at (0, 64, 3) on the straight line from
(0.5, 64, 0.5) to (0.5, 64, 6.5).  Uses the repository planner unchanged.

Run from the repository root of a 4b9e73e checkout:
    PYTHONPATH=. python -B <this script>
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import (  # noqa: E402
    CONFIG, GRASS, SESSION, SLAB, initial_state,
)
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SurfacePlanningRequest,
)
from mc2p.motion_nav.planning_reference import astar_surface_plan, build_surface_graph
from mc2p.motion_nav.step_transition import load_step_profile
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import ObservationStamp, WorldKnowledge

environment = load_frozen_environment(CONFIG / "environment-v1.json")
catalog = BlockMotionCatalog.load(CONFIG / "block-motion-traits-v1.json",
                                  CONFIG / "vanilla-block-registry-1_21.json")
ground = load_ground_motion_profile(CONFIG / "ordinary-ground-b07-v1.json", environment=environment, catalog=catalog)
step_profile = load_step_profile(CONFIG / "step-b07-v1.json", environment=environment)
jump = load_jump_up_profile(CONFIG / "jump-up-b06-v1.json", environment=environment, catalog=catalog)
air = load_air_motion_profiles(CONFIG / "air-motions-b09-v1.json", environment=environment, catalog=catalog)

world = WorldKnowledge(SESSION)
stamp = ObservationStamp(SESSION, 0, 0, "sim", 0)
blocks = {(x, 63, z): GRASS for x in range(-4, 5) for z in range(-2, 10)}
blocks[(0, 64, 3)] = SLAB
air_cells = tuple((x, y, z) for x in range(-4, 5) for z in range(-2, 10) for y in range(64, 68)
                  if (x, y, z) not in blocks)
world.observe_blocks(stamp, blocks)
world.confirm_air(stamp, air_cells)
frame, _ = initial_state(world, (.5, 64.0, .5))


def node(x, y, z):
    result = query_support_surfaces(frame.world, math.floor(x), math.floor(z), y - .1, y + .1)
    return min(result.surfaces, key=lambda s: abs(s.position[1] - y)).node_id


snapshot = KnownMapSnapshotBuilder(frame.world, KnownMapBounds(-3, 3, 63, 66, -1, 8, True)).advance(frame.world, 100_000)
graph = build_surface_graph(snapshot.snapshot.world, snapshot.snapshot.bounds, ground, step_profile, jump,
                            air_profiles=air)
plan = astar_surface_plan(graph, SurfacePlanningRequest(
    1, "r", "g", 1, SESSION.value, node(.5, 64, .5), node(.5, 64, 6.5)))
path = [(n.node_id.column_x, round(n.position[1], 2), n.node_id.column_z) for n in plan.path]
print("status:", plan.status.value, "cost s:", round(plan.total_cost_seconds, 2))
print("path:", path)
print("edge kinds:", [type(e).__name__ for e in plan.segments])
print("goes over the slab:", any(c[0] == 0 and c[2] == 3 for c in path))
