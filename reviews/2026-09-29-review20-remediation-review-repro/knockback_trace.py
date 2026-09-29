# Run from the repository root of a 340cac6 checkout: PYTHONPATH=. python -B <script> <scenario> <tick> <vz>
"""Trace one knockback impulse during a frozen drop scenario."""
import sys
from dataclasses import replace
from tests.sim.backend import Perturbations
from tests.sim.runner import run
from tests.sim.scenarios import SCENARIOS

name, at, vz = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
base = next(s for s in SCENARIOS if s.name == name)
r = run(replace(base, perturbations=Perturbations(impulses={at: (0.0, 0.0, vz)}), max_ticks=300))
print(r.outcome, r.reason, r.ticks, r.final_position, "damage", r.damage, [(t, c, d) for t, c, d in r.violations])
last = None
for row in r.trace:
    risk = tuple((a["action_id"].split("/")[-1], a["state"], a["expected_damage"]) for a in row["risk_actions"])
    key = (row["session_state"], row["session_reason"], row["controller_ids"], row["on_ground"], row["driver_state"], risk)
    if key != last:
        print(row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"], row["session_reason"],
              row["controller_ids"], "ground" if row["on_ground"] else "AIR", "driver", row["driver_state"],
              "bound" if row["source_bound"] else "unbound", risk)
        last = key
