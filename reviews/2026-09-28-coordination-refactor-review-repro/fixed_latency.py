"""Every command applied one tick late (constant latency), per the continuous-height acceptance condition."""
from dataclasses import replace
from tests.sim.backend import Perturbations
from tests.sim.runner import Scenario, lane, run
from tests.sim.scenarios import SCENARIOS, columns, drop_ledge

every = frozenset(range(2, 800))
extra = [Scenario("drop_1_full_block", lane(columns([65, 65, 64, 64, 64]), width=3), (.5, 65.0, .5), (.5, 64.0, 3.5)),
         Scenario("drop_3_direct", drop_ledge(3), (.5, 64.0, .5), (.5, 61.0, 4.5))]
for base in [s for s in SCENARIOS if not s.events and not s.perturbations.late_ticks] + extra:
    r = run(replace(base, perturbations=Perturbations(late_ticks=every), max_ticks=600))
    print(f"{base.name:<28} fixed+1 -> {r.outcome:<10} {r.reason:<40} ticks={r.ticks:<4} damage={r.damage:g} "
          f"violations={sorted({v[1] for v in r.violations})}", flush=True)
