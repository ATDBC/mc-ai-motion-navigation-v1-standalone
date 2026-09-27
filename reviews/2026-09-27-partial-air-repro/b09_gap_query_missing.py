"""Round-15 review: which cells keep the B09 gap query in NEEDS_INFORMATION.

Knowledge = what the formal partial-visibility sensor can ever give from the gap
start stance (any yaw/pitch), produced by b09_stance_visibility.cpp: visible
blocks become BLOCK, confirmable cells become AIR, the rest stay UNKNOWN.
Then the repository's own query_air_motion is run for the four directions.

The second half replays the fixture's own views (scripts/air_motion_runtime.py):
the preparation looks from the gap origin, requesting only the gap volume, then
the trial pose (facing the jump, pitch 0) re-requesting the missing cells.

Run from the repository root of a 4b9e73e checkout, after building
b09_stance_visibility.cpp as /tmp/b09_stance:
    /tmp/b09_stance > /tmp/stance_visibility.json
    PYTHONPATH=. python -B <this script> /tmp/stance_visibility.json /tmp/b09_stance
"""
import json
import math
import sys
from pathlib import Path

from mc2p.motion_nav.air_motion import load_air_motion_profiles, query_air_motion
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import (
    BlockGeometry, CellKnowledge, ObservationStamp, WorldKnowledge, WorldSessionId,
)

CONFIG = Path("config/motion-navigation")
data = json.loads(Path(sys.argv[1]).read_text())
session = WorldSessionId("b09-stance")
stamp = ObservationStamp(session, 1, 1, "sim", 0)


BODY_CONTACT_AIR = ((0, 64, 0), (0, 65, 0))   # the player's own body cells come from body contact


def knowledge(extra_air=()):
    extra_air = tuple(extra_air) + BODY_CONTACT_AIR
    world = WorldKnowledge(session)
    world.observe_blocks(stamp, {tuple(p): BlockGeometry.full_cube("minecraft:grass_block")
                                 for p in data["visible_blocks"]})
    world.confirm_air(stamp, tuple(sorted({tuple(p) for p in data["confirmable"]} | set(extra_air))))
    return world


environment = load_frozen_environment(CONFIG / "environment-v1.json")
catalog = BlockMotionCatalog.load(CONFIG / "block-motion-traits-v1.json",
                                  CONFIG / "vanilla-block-registry-1_21.json")
profiles = {p.mode: p for p in load_air_motion_profiles(
    CONFIG / "air-motions-b09-v1.json", environment=environment, catalog=catalog)}
gap = profiles[MovementMode.JUMP_GAP]


def surface(world, x, y, z):
    result = query_support_surfaces(world.view(), math.floor(x), math.floor(z), y - .1, y + .1)
    return min(result.surfaces, key=lambda s: abs(s.position[1] - y))


world = knowledge()
print("occluded-only cells from the stance:", data["occluded_only"])
for dx, dz in ((0, 1), (1, 0), (0, -1), (-1, 0)):
    start = surface(world, .5, 64.0, .5)
    end = surface(world, dx * 2 + .5, 64.0, dz * 2 + .5)
    result = query_air_motion(world.view(), start, end, gap)
    print(f"direction ({dx:+d},{dz:+d}): {result.status.value:<18} missing {list(result.missing_cells)}")
# Control: the same query once the one hidden cell is also known as air.
world = knowledge(extra_air=[tuple(p) for p in data["occluded_only"]])
start = surface(world, .5, 64.0, .5)
end = surface(world, .5, 64.0, 2.5)
print("control, hidden cell also known:", query_air_motion(world.view(), start, end, gap).status.value)


# --- Replay of the fixture's own views --------------------------------------
# Preparation (scripts/air_motion_runtime.py): from the gap origin, look_at_cell
# the start support and each target, requesting only the 5x5x5 gap volume
# (y 62..66).  Trial: teleport facing the jump direction with pitch 0, then up
# to 8 frames requesting query.missing_cells without changing the view.
import subprocess


def look_at(cell):
    dx, dy, dz = cell[0] + .5 - .5, cell[1] + .5 - 65.62, cell[2] + .5 - .5
    mc_yaw = math.degrees(math.atan2(-dx, dz))
    mc_pitch = -math.degrees(math.atan2(dy, max(1e-6, math.hypot(dx, dz))))
    return -mc_yaw, mc_pitch                 # SurfaceSensor passes -yaw to the native core


def confirmable_at(poses):
    text = "".join(f"{yaw} {pitch}\n" for yaw, pitch in poses)
    out = subprocess.run([sys.argv[2], "poses"], input=text, capture_output=True, text=True, check=True)
    return [{tuple(p) for p in json.loads(line)} for line in out.stdout.splitlines()]


in_gap_volume = lambda p: -2 <= p[0] <= 2 and 62 <= p[1] <= 66 and -2 <= p[2] <= 2
prep_cells = [(0, 63, 0), (0, 63, 2), (2, 63, 0), (0, 63, -2), (-2, 63, 0)]
prepared = set()
for seen in confirmable_at([look_at(c) for c in prep_cells]):
    prepared |= {p for p in seen if in_gap_volume(p)}
print(f"\nfixture replay: preparation confirms {len(prepared)} cells inside the gap volume")
for (dx, dz), mc_yaw in (((0, 1), 0.0), ((1, 0), -90.0), ((0, -1), 180.0), ((-1, 0), 90.0)):
    trial_view = confirmable_at([(-mc_yaw, 0.0)])[0]
    world = WorldKnowledge(session)
    world.observe_blocks(stamp, {tuple(p): BlockGeometry.full_cube("minecraft:grass_block")
                                 for p in data["visible_blocks"]})
    world.confirm_air(stamp, tuple(sorted(prepared | set(BODY_CONTACT_AIR))))
    start = surface(world, .5, 64.0, .5)
    end = surface(world, dx * 2 + .5, 64.0, dz * 2 + .5)
    query = query_air_motion(world.view(), start, end, gap)
    missing = set(query.missing_cells)
    newly = missing & trial_view            # what the 8 re-request frames can add
    if newly:
        world.confirm_air(stamp, tuple(sorted(newly)))
        query = query_air_motion(world.view(), start, end, gap)
    left = sorted(query.missing_cells)
    heights = sorted({p[1] for p in left})
    print(f"direction ({dx:+d},{dz:+d}): {query.status.value:<18} {len(left)} cells still missing, "
          f"heights {heights}; outside gap volume: {sum(not in_gap_volume(p) for p in left)}")
    print("   e.g.", left[:6])
