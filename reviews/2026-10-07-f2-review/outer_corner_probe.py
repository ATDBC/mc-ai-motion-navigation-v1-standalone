"""Outer-corner and diagonal-pillar goals: point selection vs. exact completion region.

Run from a checkout root (old 5124bd7 or new dc27f4c):
    python <this file>

Each scene is the product player layout floor (stone at y=63) plus a 3-high
pillar whose only contact with the goal region is diagonal.  The goal region is
the product/Fabric `_goal` box (+-0.20 x/z around the point).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.runner import Scenario, run, _goal
from mc2p.motion_nav.support_surfaces import query_support_surfaces, standable_point_in_region
try:
    from mc2p.motion_nav.support_surfaces import standable_region_in_goal
except ImportError:
    standable_region_in_goal = None

STONE = "minecraft:stone"


def layout(pillars, goal):
    solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
    for (x, z) in pillars:
        solids.update({(x, y, z): STONE for y in (64, 65, 66)})
    return Scene(solids, ((-6, 6), (60, 68), (-3, 14))), (.5, 64., .5), goal


CASES = {
    # building occupies x>=2, z>=7; goal sits at its outer corner
    "outer_corner_building": ([(x, z) for x in (2, 3) for z in (7, 8)], (1.7, 64., 6.7)),
    # single post diagonal to the goal point
    "diagonal_post": ([(2, 7)], (1.7, 64., 6.7)),
    # same post, goal 0.1 farther from both faces
    "diagonal_post_0.6": ([(2, 7)], (1.6, 64., 6.6)),
    # control: inner corner (two walls), as in the frozen F2 family
    "inner_corner_control": ([(2, z) for z in range(4, 9)] + [(x, 7) for x in range(-1, 3)], (1.7, 64., 6.7)),
}

for name, (pillars, goal) in CASES.items():
    scene, start, goal = layout(pillars, goal)
    backend = CalculatorBackend([0], scene, start, 0.)
    world = backend.world._world
    surface = query_support_surfaces(world, int(goal[0]), int(goal[2]), 64., 64.).surfaces[0]
    goal_state = _goal(goal)
    point = standable_point_in_region(world, surface, goal_state.region)
    line = f"{name}: point={point.status.value}"
    if standable_region_in_goal is not None:
        region = standable_region_in_goal(world, surface, goal_state.region,
                                          connection_from=start)
        line += f" region={region.status.value}"
    result = run(Scenario(name, scene, start, goal, max_ticks=400))
    final = tuple(round(v, 3) for v in result.final_position)
    print(f"{line} sim={result.outcome}/{result.reason} ticks={result.ticks} final={final}")
