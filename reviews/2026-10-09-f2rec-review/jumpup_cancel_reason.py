"""Who cancels the route when the body yaw already faces the jump?

    python <this file>          (from a checkout root)

Wraps ActionRouteExecutor.cancel and prints the calling coordinator's
failure reason, the rejected preparation, and the action indices.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

import tests.sim.runner as runner
from tests.sim.backend import Scene
from mc2p.motion_nav import action_route_executor as are

original = are.ActionRouteExecutor.cancel


def spy(self):
    frame = sys._getframe(1)
    owner = frame.f_locals.get("self")
    if owner is not None and hasattr(owner, "last_failure_reason"):
        prepared = frame.f_locals.get("prepared")
        print(f"  cancel from {type(owner).__name__}.{frame.f_code.co_name} (line {frame.f_lineno}): "
              f"{owner.last_failure_reason}; prepared={getattr(prepared, 'status', None)}; "
              f"upcoming action {frame.f_locals.get('action_index')}, executor at {self.action_index}")
    return original(self)


are.ActionRouteExecutor.cancel = spy
STONE = "minecraft:stone"


def scene(raised, xr=range(-7, 8), zr=range(-7, 8)):
    solids = {(x, 63, z): STONE for x in xr for z in zr}
    solids.update({(x, 64, z): STONE for x, z in raised})
    return Scene(solids, ((-9, 10), (60, 69), (-9, 14)))


cases = (
    ("straight +z, 3-cell run-up, yaw facing", scene([(x, z) for x in range(-7, 8) for z in (2, 3, 4)]),
     (.5, 64., -1.5), (.5, 65., 2.5), 0.),
    ("v9 column_top, yaw -90, goal (3.5,65,9.5)",
     scene([(x, z) for x in (3, 4) for z in (9, 10)], range(-4, 5), range(-1, 11)),
     (.5, 64., .5), (3.5, 65., 9.5), -90.),
)
for name, sc, start, goal, yaw in cases:
    print(name)
    r = runner.run(runner.Scenario(name, sc, start, goal, yaw, max_ticks=300))
    print(f"  -> {r.outcome}/{r.reason}")
