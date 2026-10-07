"""How often a goal that the old point query accepts gets no exact completion rectangle.

    python <this file> [--proto]     (from a dc27f4c checkout root, or the prototype tree)

Random 1x1x3 posts (density 10% / 20%) on a flat floor.  For each goal size the
script samples goal points on standable cells and compares
standable_point_in_region (old admission) with standable_region_in_goal (new
admission).  Seeds are fixed; no wall clock.
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
    rng = random.Random(f"clutter:{density}")
    counts = {name: [0, 0] for name in SIZES}
    for scene_index in range(12):
        solids = {(x, 63, z): STONE for x in range(-6, 7) for z in range(-6, 7)}
        posts = {(x, z) for x in range(-5, 6) for z in range(-5, 6) if rng.random() < density}
        posts.discard((0, 0))
        for x, z in posts:
            solids.update({(x, y, z): STONE for y in (64, 65, 66)})
        world = CalculatorBackend([0], Scene(solids, ((-8, 8), (60, 68), (-8, 8))), (.5, 64., .5), 0.).world._world
        free = [(x, z) for x in range(-4, 5) for z in range(-4, 5) if (x, z) not in posts]
        for _ in range(25):
            cx, cz = rng.choice(free)
            gx, gz = cx + rng.uniform(.05, .95), cz + rng.uniform(.05, .95)
            surface = query_support_surfaces(world, cx, cz, 64., 64.).surfaces[0]
            for name, h in SIZES.items():
                region = Aabb(gx - h, 63.92, gz - h, gx + h, 64.08, gz + h)
                point = standable_point_in_region(world, surface, region)
                if point.status.value != "feasible":
                    continue
                counts[name][0] += 1
                exact = standable_region_in_goal(world, surface, region, connection_from=(.5, 64., .5))
                if exact.status.value != "feasible":
                    counts[name][1] += 1
    for name, (accepted, lost) in counts.items():
        print(f"density {density:.2f} {name:15s}: old point feasible {accepted:3d}, "
              f"new region rejected {lost:3d} ({100 * lost / max(accepted, 1):.1f}%)")
