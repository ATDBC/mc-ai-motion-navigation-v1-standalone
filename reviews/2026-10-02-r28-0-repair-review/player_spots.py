"""'Come to me' where a player typically stands: against a wall, in a 1-wide corridor, at a ledge.

The goal is the player's feet position with the usual +/-0.20 box; the player body (0.6 wide) is
touching the wall / corridor walls or overhanging the ledge by the stated amount.  Each case is run
head-on and, for the wall, also when approached parallel to the wall.

    PYTHONPATH=. python -B player_spots.py
"""
from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run

STONE = "minecraft:stone"


def scene(extra):
    solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
    solids.update(extra)
    return Scene(solids, ((-6, 6), (60, 68), (-3, 14)))


wall = {(2, y, z): STONE for y in (64, 65, 66) for z in range(4, 9)}
corridor = {(x, y, z): STONE for y in (64, 65, 66) for z in range(4, 11) for x in (-1, 1)}
cases = [
    ("against wall, head-on", scene(wall), (0.5, 64.0, 0.5), (1.70, 64.0, 6.5)),
    ("0.1 from wall, head-on", scene(wall), (0.5, 64.0, 0.5), (1.60, 64.0, 6.5)),
    ("against wall, parallel approach", scene(wall), (1.5, 64.0, 0.5), (1.70, 64.0, 6.5)),
    ("0.1 from wall, parallel approach", scene(wall), (1.5, 64.0, 0.5), (1.60, 64.0, 6.5)),
    ("1-wide corridor, middle", scene(corridor), (0.5, 64.0, 0.5), (0.5, 64.0, 8.5)),
    ("1-wide corridor, dead end", scene({**corridor, **{(0, y, 10): STONE for y in (64, 65, 66)}}),
     (0.5, 64.0, 0.5), (0.5, 64.0, 9.7)),
    ("ledge, overhang 0.00", scene({}), (0.5, 64.0, 0.5), (0.5, 64.0, 10.70)),
    ("ledge, overhang 0.10", scene({}), (0.5, 64.0, 0.5), (0.5, 64.0, 10.80)),
    ("ledge, overhang 0.20", scene({}), (0.5, 64.0, 0.5), (0.5, 64.0, 10.90)),
]
for name, world, start, goal in cases:
    r = run(Scenario(name, world, start, goal, max_ticks=300))
    print(f"{name:<36} goal={goal}  {r.outcome:<8}{r.reason:<40} final=({r.final_position[0]:.2f}, {r.final_position[2]:.2f})")
