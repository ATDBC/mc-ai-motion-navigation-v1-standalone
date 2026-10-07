"""The largest subtraction piece is not the largest standable rectangle.

    python <this file>        (from a 9a8dfd6 checkout root)

Goal box x in [0.6, 1.0], z in [0.2, 1.0] on the stone at (0, 63, 0); a full
cube at (1, 64, 1) blocks the corner.  _subtract_footprint splits the clear
L-shape into a left strip and a bottom-middle piece; a rectangle spanning both
is larger than either piece and entirely clear.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from tests.sim.backend import CalculatorBackend, Scene
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.support_surfaces import query_support_surfaces, standable_region_in_goal

STONE = "minecraft:stone"
solids = {(x, 63, z): STONE for x in range(-2, 3) for z in range(-2, 3)}
solids.update({(1, y, 1): STONE for y in (64, 65, 66)})
world = CalculatorBackend([0], Scene(solids, ((-4, 5), (60, 68), (-4, 5))), (.5, 64., .5), 0.).world._world
surface = query_support_surfaces(world, 0, 0, 64., 64.).surfaces[0]
goal = Aabb(.6, 63.92, .2, 1.0, 64.08, 1.0)
result = standable_region_in_goal(world, surface, goal)
b = result.completion_region.bounds
print("selected piece:", (b.min_x, b.min_z, b.max_x, b.max_z), "area", round((b.max_x-b.min_x)*(b.max_z-b.min_z), 4))
spanning = (.6, .2, 1.0, .7)
ok = True
for i in range(21):
    for j in range(21):
        x = spanning[0] + (spanning[2]-spanning[0]) * i / 20
        z = spanning[1] + (spanning[3]-spanning[1]) * j / 20
        body = Aabb(x-.3, 64., z-.3, x+.3, 65.8, z+.3)
        ok &= sweep(body, (0., 0., 0.), world).status is QueryStatus.FEASIBLE
        ok &= query_support(body, world).support_fraction >= .5
print("spanning rectangle:", spanning, "area", round((spanning[2]-spanning[0])*(spanning[3]-spanning[1]), 4),
      "clear and supported at all 441 samples:", ok)
