"""Same approach sweep on the 8898cf9 checkout, through the step-0 prototype harness."""
import sys
from sim_backend import Scene
from sim_runner import Scenario, run

STONE = "minecraft:stone"
for drop in (1, 2):
    for approach in (1, 2, 3, 5):
        solids = {(0, 63, z): STONE for z in range(approach)}
        solids[(0, 63 - drop, approach)] = STONE
        scene = Scene(solids, ((-3, 3), (52, 72), (-3, approach + 3)))
        r = run(Scenario(f"drop{drop}_approach{approach}", scene, (.5, 64.0, .5), (.5, 64.0 - drop, approach + .5)))
        print(f"8898cf9 drop {drop} approach {approach} -> {r.outcome:<9} {r.reason:<34} ticks={r.ticks}", flush=True)
