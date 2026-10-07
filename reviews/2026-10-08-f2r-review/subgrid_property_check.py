"""Dense check that a returned completion rectangle is standable everywhere.

    python <this file>          (from a checkout root with the sub-grid prototype)

Samples a 21x21 grid inside each returned rectangle and requires clearance and
support >= 0.5 at every sample, and bounds inside the original goal.  Cases: the
project's own threshold-cutting test, platform-corner goals, and random holes.
"""
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from tests.sim.backend import CalculatorBackend, Scene
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.geometry import query_support, sweep, QueryStatus
from mc2p.motion_nav.support_surfaces import query_support_surfaces, standable_region_in_goal

STONE = "minecraft:stone"


def check(world, surface, goal):
    result = standable_region_in_goal(world, surface, goal)
    if result.status is not QueryStatus.FEASIBLE:
        return result.status.value, None
    b = result.completion_region.bounds
    assert goal.min_x <= b.min_x and b.max_x <= goal.max_x and goal.min_z <= b.min_z and b.max_z <= goal.max_z
    worst = 1.
    for i in range(21):
        for j in range(21):
            x = b.min_x + (b.max_x - b.min_x) * i / 20
            z = b.min_z + (b.max_z - b.min_z) * j / 20
            body = Aabb(x - .3, result.completion_region.support_height, z - .3,
                        x + .3, result.completion_region.support_height + 1.8, z + .3)
            assert sweep(body, (0., 0., 0.), world).status is QueryStatus.FEASIBLE
            support = query_support(body, world)
            assert support.status is QueryStatus.FEASIBLE
            worst = min(worst, support.support_fraction)
    assert worst + 1e-9 >= .5, worst
    return "feasible", round(worst, 4)


# the project's test_support_threshold_cutting_rectangle_is_not_approximated
world = CalculatorBackend([0], Scene({(0, 63, 0): STONE}, ((-2, 3), (60, 68), (-2, 3))), (.5, 64., .5), 0.).world._world
surface = query_support_surfaces(world, 0, 0, 64., 64.).surfaces[0]
print("project single-block case:", check(world, surface, Aabb(.3, 63.99, .3, 1., 64.01, 1.)))

samples = feasible = 0
for density in (.1, .2):
    rng = random.Random(f"holes:{density}")
    for _ in range(12):
        holes = {(x, z) for x in range(-5, 6) for z in range(-5, 6) if rng.random() < density}
        holes.discard((0, 0))
        solids = {(x, 63, z): STONE for x in range(-6, 7) for z in range(-6, 7) if (x, z) not in holes}
        world = CalculatorBackend([0], Scene(solids, ((-8, 8), (60, 68), (-8, 8))), (.5, 64., .5), 0.).world._world
        floor = [(x, z) for x in range(-4, 5) for z in range(-4, 5) if (x, z) not in holes]
        for _ in range(25):
            cx, cz = rng.choice(floor)
            gx, gz = cx + rng.uniform(.05, .95), cz + rng.uniform(.05, .95)
            surface = query_support_surfaces(world, cx, cz, 64., 64.).surfaces[0]
            for h in (.2, .5, 2.5 / 2 ** .5):
                samples += 1
                status, _ = check(world, surface, Aabb(gx - h, 63.92, gz - h, gx + h, 64.08, gz + h))
                feasible += status == "feasible"
print(f"random holes: {samples} goals, {feasible} feasible regions, all dense-checked (clearance, support >= 0.5, inside goal)")
