"""Round-17 review: cost of the bounded re-search for proof edges after an air action.

Only the first ground run gets a traversal proof; proof-only Walk edges in any
later run are disabled one path at a time and the whole A* search is repeated
(at most 16 times, each call with the full expansion and time budget).

Scene: a W-wide field (fully known).  The start row drops one block (a
ControlledDrop, so everything after it is a "later" run), then R rows of
bottom-slab / full-block half steps span the whole width.  The control scene has
the same half steps but no drop (so the first run is proved directly).

Run from the repository root of a 5fa2f33 checkout, with height_transition_cost.py
next to this script:
    PYTHONPATH=. python -B <this script>
"""
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from height_transition_cost import CONFIG, GRASS, SESSION, SLAB, initial_state, top  # noqa: E402
from mc2p.motion_nav import known_map_planner as planner
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
from mc2p.motion_nav.world_model import ObservationStamp, WorldKnowledge

environment = load_frozen_environment(CONFIG / "environment-v1.json")
catalog = BlockMotionCatalog.load(CONFIG / "block-motion-traits-v1.json",
                                  CONFIG / "vanilla-block-registry-1_21.json")
ground = load_ground_motion_profile(CONFIG / "ordinary-ground-b07-v1.json", environment=environment, catalog=catalog)
step_profile = load_step_profile(CONFIG / "step-b07-v1.json", environment=environment)
jump = load_jump_up_profile(CONFIG / "jump-up-b06-v1.json", environment=environment, catalog=catalog)
air = load_air_motion_profiles(CONFIG / "air-motions-b09-v1.json", environment=environment, catalog=catalog)

calls = {"search": 0}
for name in ("_plain_search", "_resource_aware_search"):
    original = getattr(planner, name)

    def counted(*args, _original=original, **kwargs):
        calls["search"] += 1
        return _original(*args, **kwargs)

    setattr(planner, name, counted)


def build(columns, half_width):
    world = WorldKnowledge(SESSION)
    stamp = ObservationStamp(SESSION, 0, 0, "sim", 0)
    blocks, known_air = {}, set()
    for z in range(-2, len(columns) + 2):
        column = columns[min(max(z, 0), len(columns) - 1)]
        solid = {}
        for item in column:
            y, geometry = item if isinstance(item, tuple) else (item, GRASS)
            solid[y] = geometry
        for x in range(-half_width - 1, half_width + 2):
            blocks[(x, 62, z)] = GRASS
            for y in range(63, 75):
                if y in solid:
                    blocks[(x, y, z)] = solid[y]
                else:
                    known_air.add((x, y, z))
    world.observe_blocks(stamp, blocks)
    world.confirm_air(stamp, tuple(sorted(known_air)))
    return world


def lane(rows, drop):
    columns = ([[63, 64], [63, 64], [63], [63]] if drop     # walk, one-block drop, flat
               else [[63], [63], [63], [63]])
    full = 63                                          # highest full block
    for row in range(rows):
        if row % 2 == 0:
            columns.append(list(range(63, full + 1)) + [(full + 1, SLAB)])   # +0.5
        else:
            full += 1
            columns.append(list(range(63, full + 1)))                       # +0.5
    columns.append(list(columns[-1]))
    return tuple(columns)


HALF_WIDTHS = (3, 10)
ROWS = (2, 4, 6, 8)
for half_width in HALF_WIDTHS:
    for rows in ROWS:
        for drop in (False, True):
            columns = lane(rows, drop)
            world = build(columns, half_width)
            start = (.5, top(columns, 0), .5)
            goal = (.5, top(columns, len(columns) - 1), len(columns) - .5)
            frame, state = initial_state(world, start)

            def node(position):
                result = query_support_surfaces(frame.world, math.floor(position[0]), math.floor(position[2]),
                                                position[1] - .1, position[1] + .1)
                return min(result.surfaces, key=lambda s: abs(s.position[1] - position[1])).node_id

            snapshot = KnownMapSnapshotBuilder(
                frame.world,
                KnownMapBounds(-half_width, half_width, 63, 71, -1, len(columns), True,
                               extra_top_clearance_cells=2),
            ).advance(frame.world, 1_000_000).snapshot
            calls["search"] = 0
            started = time.perf_counter()
            candidate = plan_known_surface_snapshot(
                snapshot, ground, step_profile,
                SurfacePlanningRequest(1, "r", "g", 1, SESSION.value, node(start), node(goal),
                                       entry_physics_state=state),
                jump, air_profiles=air)
            elapsed = (time.perf_counter() - started) * 1000
            kinds = [type(e).__name__.replace("Surface", "").replace("Edge", "") for e in candidate.segments]
            if candidate.reasons:
                print("   reasons", candidate.reasons)
            print(f"width {2 * half_width + 1:>2}, {rows} half-step rows, {'drop first' if drop else 'no drop   '}: "
                  f"{candidate.status.value:<9} A* searches {calls['search']:>2}  {elapsed:7.0f} ms  "
                  f"steps {kinds.count('Step')}  lateral jogs "
                  f"{sum(1 for e in candidate.segments if e.start.column_x != e.end.column_x)}")
