"""Straight Walk -> JumpUp onto a wide one-block step: four directions x start yaw.

    python <this file>          (from a checkout root)

Start 3 cells before the step, goal on the landing cell.  start yaw is either
0 (the runner default, facing +z) or already facing the travel direction.
Minecraft yaw: 0 = +z (south), 90 = -x (west), 180 = -z (north), -90 = +x (east).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

import tests.sim.runner as runner
from tests.sim.backend import Scene

STONE = "minecraft:stone"
floor = {(x, 63, z): STONE for x in range(-7, 8) for z in range(-7, 8)}
VOL = ((-9, 10), (60, 69), (-9, 10))
CASES = {
    "+z (south)": ([(x, z) for x in range(-7, 8) for z in (2, 3, 4)], (.5, 64., -1.5), (.5, 65., 2.5), 0.),
    "-z (north)": ([(x, z) for x in range(-7, 8) for z in (-2, -3, -4)], (.5, 64., 2.5), (.5, 65., -1.5), 180.),
    "+x (east)": ([(x, z) for x in (2, 3, 4) for z in range(-7, 8)], (-1.5, 64., .5), (2.5, 65., .5), -90.),
    "-x (west)": ([(x, z) for x in (-2, -3, -4) for z in range(-7, 8)], (2.5, 64., .5), (-1.5, 65., .5), 90.),
}
for name, (raised, start, goal, facing) in CASES.items():
    solids = dict(floor)
    solids.update({(x, 64, z): STONE for x, z in raised})
    for label, yaw in (("yaw 0", 0.), ("yaw facing", facing)):
        r = runner.run(runner.Scenario(name, Scene(solids, VOL), start, goal, yaw, max_ticks=300))
        kinds = []
        for row in r.trace:
            if row["action_kind"] and (not kinds or kinds[-1] != row["action_kind"]):
                kinds.append(row["action_kind"])
        print(f"{name:11s} {label:10s}: {r.outcome}/{r.reason} final={tuple(round(v, 3) for v in r.final_position)} {kinds}")
