"""Scan goal points around one diagonal post; compare point selection and the exact region.

    python <this file>          (from a dc27f4c checkout root)

Post occupies x in [2,3], z in [7,8] at body height.  Goal region is the
product `_goal` box (+-0.20).  Prints where the old point query is feasible but
the new completion region is not.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.runner import _goal
from mc2p.motion_nav.support_surfaces import (
    query_support_surfaces, standable_point_in_region, standable_region_in_goal)

STONE = "minecraft:stone"
solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
solids.update({(2, y, 7): STONE for y in (64, 65, 66)})
scene = Scene(solids, ((-6, 6), (60, 68), (-3, 14)))
world = CalculatorBackend([0], scene, (.5, 64., .5), 0.).world._world
surface = query_support_surfaces(world, 1, 6, 64., 64.).surfaces[0]

lost = []
total = 0
steps = [round(1.0 + i * .05, 2) for i in range(1, 15)]   # 1.05 .. 1.70 inside cell 1 / 6
for gx in steps:
    for gz in [round(v + 5, 2) for v in steps]:
        region = _goal((gx, 64., gz)).region
        point = standable_point_in_region(world, surface, region)
        exact = standable_region_in_goal(world, surface, region, connection_from=(.5, 64., .5))
        total += 1
        if point.status.value == "feasible" and exact.status.value != "feasible":
            lost.append((gx, gz, exact.status.value))
print(f"goal points scanned in cell (1,6): {total}")
print(f"point feasible but region not: {len(lost)}")
xs = sorted({p[0] for p in lost}); zs = sorted({p[1] for p in lost})
if lost:
    print(f"  x range {xs[0]}..{xs[-1]}, z range {zs[0]}..{zs[-1]}, statuses {sorted({p[2] for p in lost})}")
center = _goal((1.5, 64., 6.5)).region
print("cell centre (1.5, 6.5):",
      standable_point_in_region(world, surface, center).status.value,
      standable_region_in_goal(world, surface, center).status.value)
