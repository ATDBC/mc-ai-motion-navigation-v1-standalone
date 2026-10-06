"""Why R28-C-02 and the `active_route_dependency_retry` fault no longer reach their paths.

    PYTHONPATH=. python -B material_variants.py        (run at b1d43c2 and at 1f0fefe)

Both use flat_walk with the floor cell (0, 63, 5) alternating every 1 or 3 ticks.  The current
tests alternate stone and grass_block.  The probe swaps the second material and counts the paths
the tests exist to protect (planning submissions, RetryLedger.begin_recovery).  The bottom slab
changes walkability without removing support; air removes support (an unavoidable fall, I3).
"""
import collections
from dataclasses import replace

from mc2p.motion_nav.retry_ledger import RetryLedger
from tests.sim.backend import Perturbations
from tests.sim.runner import run
from tests.sim.scenarios import SCENARIOS

COUNTS = collections.Counter()
original = RetryLedger.begin_recovery


def begin_recovery(*args, **kwargs):
    COUNTS["begin_recovery"] += 1
    return original(*args, **kwargs)


RetryLedger.begin_recovery = begin_recovery
BASE = next(s for s in SCENARIOS if s.name == "flat_walk")
SECOND = (("grass_block (current)", "minecraft:grass_block"), ("air", None),
          ("bottom slab", "minecraft:stone_slab[type=bottom]"))

print("R28-C-02  every tick, ticks 3-179; the test expects outcome=failed with valid work identity")
for label, other in SECOND:
    edits = {t: {(0, 63, 5): "minecraft:stone" if t % 2 else other} for t in range(3, 180)}
    r = run(replace(BASE, name="c02", max_ticks=180, perturbations=Perturbations(world_edits=edits)))
    plans = sum(len(row.get("planning_submissions") or ()) for row in r.trace)
    print(f"  stone/{label:<22} outcome={r.outcome:<8} reason={r.reason:<34} plans={plans} I3/violations={len(r.violations)}")

print("fault active_route_dependency_retry  every 3 ticks, 3-64; the test expects success and begin_recovery > 0")
for label, other in SECOND:
    if other is None:
        continue
    COUNTS.clear()
    edits = {t: {(0, 63, 5): "minecraft:stone" if t % 2 else other} for t in range(3, 65, 3)}
    r = run(replace(BASE, perturbations=Perturbations(world_edits=edits)))
    print(f"  stone/{label:<22} outcome={r.outcome:<8} reason={r.reason:<34} begin_recovery={COUNTS['begin_recovery']}")
