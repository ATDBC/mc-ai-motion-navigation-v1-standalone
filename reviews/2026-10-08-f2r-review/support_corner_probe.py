"""Goals near a platform's outer corner: support, not an obstacle, cuts the region.

    python <this file>          (from a checkout root: 5124bd7 or 1ee76b6)

Platform: stone at y=63 for x in [-4,4], z in [-1,10]; its outer corner is (5, 11).
A body centred near that corner is legal in vanilla (any overlap supports), and the
old point query finds a standing point with support >= 0.5 inside the goal.  The
new region keeps one rectangle and requires support >= 0.5 at every vertex.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.runner import Scenario, run, _goal
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.support_surfaces import query_support_surfaces, standable_point_in_region
try:
    from mc2p.motion_nav.support_surfaces import standable_region_in_goal
except ImportError:
    standable_region_in_goal = None

STONE = "minecraft:stone"
solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
scene = Scene(solids, ((-6, 7), (60, 68), (-3, 13)))
world = CalculatorBackend([0], scene, (.5, 64., .5), 0.).world._world


def melee(position, risk_policy_id="no_expected_damage"):
    from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
    x, y, z = position
    return GoalState(Aabb(x - .5, y - .1, z - .5, x + .5, y + .1, z + .5), GoalSupport.SOLID,
                     frozenset({MovementMode.WALK}), frozenset({"standing"}), .6)


def follow(position, risk_policy_id="no_expected_damage"):
    from mc2p.contracts.observation import Vec3V0
    from mc2p.skills.known_world_follow_driver import KnownWorldFollowDriver
    return KnownWorldFollowDriver._goal(Vec3V0(*position))


import tests.sim.runner as runner
original_goal = runner._goal
for label, factory, goal in (("product +-0.20 at (4.80,10.80)", original_goal, (4.8, 64., 10.8)),
                             ("product +-0.20 at (4.90,10.90)", original_goal, (4.9, 64., 10.9)),
                             ("melee r=0.5 at (4.70,10.70)", melee, (4.7, 64., 10.7)),
                             ("follow hold, player at (4.50,10.50)", follow, (4.5, 64., 10.5)),
                             ("control: melee r=0.5 mid-edge (0.50,10.70)", melee, (.5, 64., 10.7))):
    region = factory(goal).region
    surface = query_support_surfaces(world, int(goal[0]), int(goal[2]), 64., 64.).surfaces[0]
    line = f"{label}: point={standable_point_in_region(world, surface, region).status.value}"
    if standable_region_in_goal is not None:
        line += f" region={standable_region_in_goal(world, surface, region, connection_from=(.5, 64., .5)).status.value}"
    runner._goal = factory
    result = runner.run(runner.Scenario(label, scene, (.5, 64., .5), goal, max_ticks=400))
    print(f"{line} sim={result.outcome}/{result.reason} ticks={result.ticks} "
          f"final={tuple(round(v, 2) for v in result.final_position)}")
