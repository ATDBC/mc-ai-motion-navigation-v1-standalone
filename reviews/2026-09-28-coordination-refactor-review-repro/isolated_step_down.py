"""Isolated one-block step down (the continuous-height 'down one full block' case), varying the approach
length, lane width and start offset.  The continuous-height acceptance requires this route to complete under
normal input and under a fixed one-tick delay."""
from dataclasses import replace
from tests.sim.backend import Perturbations
from tests.sim.runner import Scenario, lane, run
from tests.sim.scenarios import columns

rows = []
for approach in (2, 3, 5):
    for width in (1, 3):
        for start_x, start_z in ((.5, .5), (.3, .5), (.5, .8)):
            tops = [65] * approach + [64] * 3
            base = Scenario(f"down1_a{approach}_w{width}_s{start_x},{start_z}", lane(columns(tops), width=width),
                            (start_x, 65.0, start_z), (.5, 64.0, approach + 1.5), max_ticks=300)
            for label, perturbation in (("clean", Perturbations()),
                                        ("fixed+1", Perturbations(late_ticks=frozenset(range(2, 800))))):
                r = run(replace(base, perturbations=perturbation))
                ok = r.outcome == "success" and not r.violations
                rows.append(ok)
                print(f"{base.name:<26} {label:<8} {'OK  ' if ok else 'FAIL'} {r.outcome:<9} {r.reason:<34} "
                      f"ticks={r.ticks:<4} pos={r.final_position} violations={[(t, c) for t, c, _ in r.violations]}",
                      flush=True)
print(f"passed {sum(rows)}/{len(rows)}")
