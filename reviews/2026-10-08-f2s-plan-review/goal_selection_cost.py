"""Cost of Session goal-surface selection if every cell runs the region query.

    python <this file>        (from a 9a8dfd6 checkout root)

Mimics NavigationSession._surface_for_goal: for every column in the goal box,
query support surfaces, then evaluate each surface.  Compares the current point
query with standable_region_in_goal on follow / melee / product goal sizes in
the fixed 10% / 20% post clutter used by the earlier scans.  Single process,
time.perf_counter_ns, medians and maxima over 60 goals per row.
"""
import math
import random
import statistics
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from tests.sim.backend import CalculatorBackend, Scene
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.support_surfaces import (
    query_support_surfaces, standable_point_in_region, standable_region_in_goal)

STONE = "minecraft:stone"
SIZES = {"product +-0.20": .20, "melee r=0.50": .50, "follow 1.77": 2.5 / 2 ** .5}


def select(world, goal, query):
    started = time.perf_counter_ns()
    calls = 0
    for x in range(math.floor(goal.min_x), math.floor(math.nextafter(goal.max_x, -math.inf)) + 1):
        for z in range(math.floor(goal.min_z), math.floor(math.nextafter(goal.max_z, -math.inf)) + 1):
            for surface in query_support_surfaces(world, x, z, goal.min_y, goal.max_y,
                                                  collect_complete_missing=True).surfaces:
                query(world, surface, goal)
                calls += 1
    return (time.perf_counter_ns() - started) / 1e6, calls


for density in (.10, .20):
    rng = random.Random(f"clutter:{density}")
    rows = {name: ([], [], []) for name in SIZES}
    for _ in range(6):
        solids = {(x, 63, z): STONE for x in range(-6, 7) for z in range(-6, 7)}
        posts = {(x, z) for x in range(-5, 6) for z in range(-5, 6) if rng.random() < density}
        posts.discard((0, 0))
        for x, z in posts:
            solids.update({(x, y, z): STONE for y in (64, 65, 66)})
        world = CalculatorBackend([0], Scene(solids, ((-8, 8), (60, 68), (-8, 8))), (.5, 64., .5), 0.).world._world
        free = [(x, z) for x in range(-3, 4) for z in range(-3, 4) if (x, z) not in posts]
        for _ in range(10):
            cx, cz = rng.choice(free)
            gx, gz = cx + rng.uniform(.05, .95), cz + rng.uniform(.05, .95)
            for name, h in SIZES.items():
                goal = Aabb(gx - h, 63.92, gz - h, gx + h, 64.08, gz + h)
                point_ms, calls = select(world, goal, standable_point_in_region)
                region_ms, _ = select(world, goal, standable_region_in_goal)
                rows[name][0].append(point_ms); rows[name][1].append(region_ms); rows[name][2].append(calls)
    for name, (point, region, calls) in rows.items():
        print(f"posts {density:.2f} {name:15s}: surfaces/goal median {statistics.median(calls):.0f} max {max(calls)}; "
              f"point median {statistics.median(point):.2f} ms max {max(point):.2f}; "
              f"region median {statistics.median(region):.2f} ms max {max(region):.2f}")
