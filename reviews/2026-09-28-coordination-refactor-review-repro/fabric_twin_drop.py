"""Simulation twin of the Fabric continuous-height 'direct-drop-N' trials (start on the only upper block,
goal on the single lower block), next to the same drops with a walking approach of k upper blocks."""
from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run

STONE = "minecraft:stone"


def twin(drop, approach_blocks):
    # upper blocks z = 0..approach_blocks-1 at y=63 (feet 64); one lower block at z=approach_blocks.
    solids = {(0, 63, z): STONE for z in range(approach_blocks)}
    solids[(0, 63 - drop, approach_blocks)] = STONE
    return Scene(solids, ((-3, 3), (52, 72), (-3, approach_blocks + 3)))


for drop in (1, 2, 3, 5):
    for approach in (1, 2, 3, 5):
        budget = float(max(0, drop - 3))
        r = run(Scenario(f"drop{drop}_approach{approach}", twin(drop, approach),
                         (.5, 64.0, .5), (.5, 64.0 - drop, approach + .5), damage_points=budget, max_ticks=300))
        tag = "Fabric twin" if approach == 1 else ""
        print(f"drop {drop} approach {approach} {tag:<11} -> {r.outcome:<9} {r.reason:<34} ticks={r.ticks:<4} "
              f"damage={r.damage:g} violations={[(t, c) for t, c, _ in r.violations]}", flush=True)
