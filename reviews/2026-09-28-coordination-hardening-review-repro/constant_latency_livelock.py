# Run from the repository root of an eb4653c checkout: PYTHONPATH=. python -B <script>
"""Isolated one-block step down after a three-block approach with every command one tick late:
the grounded entry recovery oscillates on a six-tick cycle and never ends; I4 stays silent because the body moves."""
from dataclasses import replace
from tests.sim.backend import Perturbations
from tests.sim.runner import Scenario, lane, run
from tests.sim.scenarios import columns

base = Scenario("down1_a3", lane(columns([65] * 3 + [64] * 3), width=3), (.5, 65.0, .5), (.5, 64.0, 4.5), max_ticks=300)
r = run(replace(base, perturbations=Perturbations(late_ticks=frozenset(range(2, 800)))))
print(r.outcome, r.reason, "violations", r.violations)
for row in r.trace[-13:]:
    print(row["tick"], tuple(round(v, 3) for v in row["position"]), row["session_reason"],
          "retries", row["retry_total_failures"], "fwd", row["applied_movement"]["forward"],
          "sneak", row["applied_movement"]["sneak"])
