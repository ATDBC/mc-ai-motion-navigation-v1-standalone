"""Follow-sized and melee-sized goals beside a building's outer corner.

    python <this file>          (from a checkout root)

Uses the production goal constructors (KnownWorldFollowDriver._goal and
combat_standoff_goal_state geometry) through the ordinary simulation runner.
The house is a 3x3 solid block at x 2..4, z 7..9, y 64..66.
"""
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

import tests.sim.runner as runner
from tests.sim.backend import Scene
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.known_world_follow_driver import KnownWorldFollowDriver
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.world_model import Aabb
from mc2p.skills.fixed_melee import COMBAT_GOAL_RADIUS_BLOCKS

STONE = "minecraft:stone"
solids = {(x, 63, z): STONE for x in range(-4, 9) for z in range(-1, 14)}
solids.update({(x, y, z): STONE for x in (2, 3, 4) for z in (7, 8, 9) for y in (64, 65, 66)})
scene = Scene(solids, ((-6, 10), (60, 68), (-3, 16)))


def follow_goal(position, risk_policy_id="no_expected_damage"):
    return KnownWorldFollowDriver._goal(Vec3V0(*position))


def melee_goal(position, risk_policy_id="no_expected_damage"):
    x, y, z = position
    r = COMBAT_GOAL_RADIUS_BLOCKS
    return GoalState(Aabb(x - r, y - .1, z - r, x + r, y + .1, z + r), GoalSupport.SOLID,
                     frozenset({MovementMode.WALK}), frozenset({"standing"}), .6)


for label, factory, goal in (("melee r=0.5 at corner", melee_goal, (1.5, 64., 6.5)),
                             ("melee r=0.5 near corner", melee_goal, (1.6, 64., 6.6)),
                             ("follow hold at corner", follow_goal, (1.7, 64., 6.7))):
    for start in ((.5, 64., .5), (6.5, 64., 5.5), (.5, 64., 12.5)):
        runner._goal = factory
        result = runner.run(runner.Scenario(label, scene, start, goal, max_ticks=500))
        print(f"{label:24s} start={start}: {result.outcome}/{result.reason} ticks={result.ticks} "
              f"final={tuple(round(v, 2) for v in result.final_position)}")
