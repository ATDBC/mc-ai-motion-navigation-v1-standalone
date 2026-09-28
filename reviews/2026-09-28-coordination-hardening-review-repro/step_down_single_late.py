# Run from the repository root of an eb4653c checkout: PYTHONPATH=. python -B <script>
"""Isolated one-block step down after a walking approach: one late tick at every position of the clean run
(covers 'first drop command late by one tick'), and every command late (constant one-tick latency)."""
from dataclasses import replace
from tests.sim.backend import Perturbations
from tests.sim.runner import Scenario, lane, run
from tests.sim.scenarios import columns

for approach in (2, 3, 5):
    base = Scenario(f"down1_a{approach}", lane(columns([65] * approach + [64] * 3), width=3),
                    (.5, 65.0, .5), (.5, 64.0, approach + 1.5), max_ticks=300)
    clean = run(base)
    fails = []
    for t in range(2, clean.ticks):
        r = run(replace(base, perturbations=Perturbations(late_ticks=frozenset({t}))))
        if r.outcome != "success" or r.violations:
            fails.append(f"t{t}:{r.outcome}/{r.reason}")
    fixed = run(replace(base, perturbations=Perturbations(late_ticks=frozenset(range(2, 800)))))
    print(f"approach {approach}: clean={clean.outcome}@{clean.ticks}; single-late failures {len(fails)}/{clean.ticks - 2} "
          f"{'; '.join(fails)}; every-command-late -> {fixed.outcome}/{fixed.reason}@{fixed.ticks} "
          f"violations={[(t, c) for t, c, _ in fixed.violations]}", flush=True)
