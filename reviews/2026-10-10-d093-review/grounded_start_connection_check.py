"""Component check of the observed-grounded-start connection rule.

    python <this file>          (from a checkout with proto-h02-replan.diff applied)

World (fully known): stone floor at y=60, air above it, and a platform
block layer at y=64 (top 65) with an L shape:
x 3..4 for z 8..10, plus x 5 for z 10 only.  Body footprint 0.6.
Each case asks query_standable_connection for one exact endpoint from one
observed body position, with the original rule and with the grounded-start
rule.  The relaxed rule must only accept legs whose support never drops
while it is below the 0.5 planning margin.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.support_surfaces import (
    query_standable_connection, query_support_surfaces,
)
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)

session = WorldSessionId("grounded-start-check")
world = WorldKnowledge(session)
stamp = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
solids = {(x, 64, z) for x in (3, 4) for z in (8, 9, 10)} | {(5, 64, 10)}
floor = {(x, 60, z) for x in range(0, 9) for z in range(5, 14)}
world.observe_blocks(stamp, {cell: BlockGeometry.full_cube("minecraft:stone")
                             for cell in solids | floor})
world.confirm_air(stamp, tuple((x, y, z) for x in range(0, 9) for y in range(61, 68)
                               for z in range(5, 14) if (x, y, z) not in solids))
view = world.view()
surface = query_support_surfaces(view, 3, 9, 64.5, 65.5).surfaces[0]

CASES = (
    ("inward from 20% edge support (column-top short landing)", (2.82, 65., 9.5), (3.5, 65., 9.5), "accept"),
    ("along the edge at 20%, then inward", (2.82, 65., 8.5), (3.5, 65., 9.6), "accept"),
    ("outward toward the edge", (3.5, 65., 9.5), (2.82, 65., 9.5), "reject"),
    ("from 20% edge across the inner notch (support drops)", (4.5, 65., 8.35), (5.5, 65., 10.5), "reject"),
)
ok = True
for name, start, end, expect in CASES:
    strict = query_standable_connection(view, surface, end, start).status
    relaxed = query_standable_connection(view, surface, end, start,
                                         observed_grounded_start=True).status
    got = "accept" if relaxed is QueryStatus.FEASIBLE else "reject"
    ok &= got == expect
    print(f"{name:55s} strict={strict.value:9s} grounded={relaxed.value:9s} expect={expect}")
print("ALL AS EXPECTED" if ok else "MISMATCH")
