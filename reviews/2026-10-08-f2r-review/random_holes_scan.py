"""Random floor holes (no posts): old point query vs exact completion region.

    python <this file>          (from a checkout root)

Holes remove the stone at y=63, so the support threshold, not an obstacle,
cuts the region.  Fixed seeds; goals sampled on cells that still have floor.
"""
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from tests.sim.backend import CalculatorBackend, Scene
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.support_surfaces import (
    query_support_surfaces, standable_point_in_region, standable_region_in_goal)

STONE = "minecraft:stone"
SIZES = {"product +-0.20": .20, "melee r=0.50": .50, "follow 1.77": 2.5 / 2 ** .5}

for density in (.10, .20):
    rng = random.Random(f"holes:{density}")
    counts = {name: [0, 0] for name in SIZES}
    for _ in range(12):
        holes = {(x, z) for x in range(-5, 6) for z in range(-5, 6) if rng.random() < density}
        holes.discard((0, 0))
        solids = {(x, 63, z): STONE for x in range(-6, 7) for z in range(-6, 7) if (x, z) not in holes}
        world = CalculatorBackend([0], Scene(solids, ((-8, 8), (60, 68), (-8, 8))), (.5, 64., .5), 0.).world._world
        floor = [(x, z) for x in range(-4, 5) for z in range(-4, 5) if (x, z) not in holes]
        for _ in range(25):
            cx, cz = rng.choice(floor)
            gx, gz = cx + rng.uniform(.05, .95), cz + rng.uniform(.05, .95)
            surfaces = query_support_surfaces(world, cx, cz, 64., 64.).surfaces
            if not surfaces:
                continue
            for name, h in SIZES.items():
                region = Aabb(gx - h, 63.92, gz - h, gx + h, 64.08, gz + h)
                if standable_point_in_region(world, surfaces[0], region).status.value != "feasible":
                    continue
                counts[name][0] += 1
                exact = standable_region_in_goal(world, surfaces[0], region, connection_from=(.5, 64., .5))
                counts[name][1] += exact.status.value != "feasible"
    for name, (accepted, lost) in counts.items():
        print(f"holes {density:.2f} {name:15s}: old point feasible {accepted:3d}, "
              f"new region rejected {lost:3d} ({100 * lost / max(accepted, 1):.1f}%)")
