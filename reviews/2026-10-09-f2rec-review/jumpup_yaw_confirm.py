"""Confirm that body yaw, not route geometry, decides the v9 column_top handoff.

    python <this file>          (from a checkout root)

The v9 column_top scene and start (route: +z, then a 90 degree turn to +x, then
JumpUp toward +x).  The JumpUp entry window requires yaw within the calibrated
error of the jump direction; FixedRoute never outputs a look.  Only the start
yaw differs between runs.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

import tests.sim.runner as runner
from tests.sim.backend import Scene

STONE = "minecraft:stone"
solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
solids.update({(x, 64, z): STONE for x in (3, 4) for z in (9, 10)})
scene = Scene(solids, ((-7, 8), (60, 69), (-5, 14)))
for goal in ((3.5, 65., 9.5), (4.7, 65., 10.7)):
    for label, yaw in (("start yaw 0 (faces first leg +z)", 0.), ("start yaw -90 (faces jump +x)", -90.)):
        r = runner.run(runner.Scenario(label, scene, (.5, 64., .5), goal, yaw, max_ticks=300))
        kinds = []
        for row in r.trace:
            if row["action_kind"] and (not kinds or kinds[-1] != row["action_kind"]):
                kinds.append(row["action_kind"])
        print(f"goal {goal} {label:34s}: {r.outcome}/{r.reason} "
              f"final={tuple(round(v, 3) for v in r.final_position)} {kinds}")
