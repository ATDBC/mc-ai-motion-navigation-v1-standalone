"""Point goals at off-centre positions on a flat, fully known stone platform (c9f7d41).

The sim's point goal is the same as the formal Fabric probe: a +/-0.20 box around the target
position.  NavigationSession._surface_for_goal only accepts a support surface whose
representative point lies inside that box, so a target whose box contains no block-centre
coordinate fails with goal_surface_unavailable even on open flat ground.

Run from the repository root of a c9f7d41 checkout:  PYTHONPATH=. python -B <this file>
"""
import collections
import random

from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run

STONE = "minecraft:stone"
scene = Scene({(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 14)},
              ((-6, 6), (60, 68), (-3, 16)))

rng = random.Random(20261002)
counts = collections.Counter()
rows = []
for i in range(40):
    gx = rng.randrange(-2, 3) + rng.random()
    gz = rng.randrange(6, 11) + rng.random()
    centred = all(0.3 <= (v % 1.0) <= 0.7 for v in (gx, gz))
    result = run(Scenario(f"off_centre_{i}", scene, (0.5, 64.0, 0.5), (gx, 64.0, gz), max_ticks=200))
    counts[(centred, result.outcome, result.reason)] += 1
    rows.append((round(gx, 3), round(gz, 3), centred, result.outcome, result.reason))

for row in rows:
    print(*row)
print("\n(box contains a block-centre coordinate on both axes, outcome, reason) -> count")
for key, value in sorted(counts.items(), key=str):
    print(f"  {key}: {value}")
print("\nExpected share of uniformly random targets whose +/-0.20 box contains a centre on both axes: "
      f"{0.4 * 0.4:.2f}")
