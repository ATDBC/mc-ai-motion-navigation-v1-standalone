"""Outcome distribution of the frozen seed families (the formal scan only reports accepted/unexpected),
plus the same seeds at lower late probabilities."""
from collections import Counter
from dataclasses import replace
import sys
from tests.sim.runner import late_ticks, run
from tests.sim.scenarios import SCENARIOS

names = sys.argv[1].split(",") if len(sys.argv) > 1 else ["direct_drop_2", "half_steps_up_down"]
probabilities = [float(p) for p in sys.argv[2].split(",")] if len(sys.argv) > 2 else [0.2, 0.05, 0.02]
seeds = range(280001, 280101)
for name in names:
    base = next(s for s in SCENARIOS if s.name == name)
    for probability in probabilities:
        tally = Counter()
        for seed in seeds:
            r = run(replace(base, perturbations=replace(base.perturbations,
                                                         late_ticks=late_ticks(probability, seed))))
            tally[(r.outcome, r.reason, bool(r.violations))] += 1
        ok = sum(v for (o, _, bad), v in tally.items() if o == "success" and not bad)
        print(f"{name:<24} p_late={probability:<5} success={ok}/{len(seeds)}  "
              + "  ".join(f"{o}/{rs}{'/VIOL' if bad else ''}={v}" for (o, rs, bad), v in tally.most_common()),
              flush=True)
