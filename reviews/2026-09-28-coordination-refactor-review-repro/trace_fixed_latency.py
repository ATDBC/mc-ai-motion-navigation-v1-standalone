"""Trace one scenario under constant one-tick latency."""
import sys
from dataclasses import replace
from tests.sim.backend import Perturbations
from tests.sim.runner import run
from tests.sim.scenarios import SCENARIOS

name = sys.argv[1] if len(sys.argv) > 1 else "direct_drop_2"
base = next(s for s in SCENARIOS if s.name == name)
r = run(replace(base, perturbations=Perturbations(late_ticks=frozenset(range(2, 800))), max_ticks=int(sys.argv[2]) if len(sys.argv) > 2 else 200))
print(r.outcome, r.reason, r.ticks, r.final_position, r.violations)
last = None
for row in r.trace:
    key = (row["session_state"], row["session_reason"], row["controller_ids"], row["controller_phase"], row["action_kind"],
           row["driver_state"], row["on_ground"], row["sneaking"], row["source_bound"])
    if key != last:
        print(row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"], row["session_reason"],
              row["controller_ids"], row["controller_phase"], row["action_kind"], "driver", row["driver_state"], row["driver_reason"],
              "ground" if row["on_ground"] else "AIR", "sneak" if row["sneaking"] else "", "bound" if row["source_bound"] else "unbound",
              row["applied_movement"])
        last = key
