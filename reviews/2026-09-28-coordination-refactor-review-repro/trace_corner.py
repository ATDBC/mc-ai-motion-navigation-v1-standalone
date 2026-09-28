"""Trace the corner-column landing (goal x=1.5 on the 3-wide ledge)."""
import sys
from tests.sim.runner import Scenario, run
from tests.sim.scenarios import drop_ledge

height = int(sys.argv[1]) if len(sys.argv) > 1 else 2
r = run(Scenario("corner", drop_ledge(height), (.5, 64.0, .5), (1.5, 64.0 - height, 4.5),
                 damage_points=2.0 if height > 3 else 0.0, max_ticks=300))
print(r.outcome, r.reason, r.ticks, r.final_position, sorted({v[1] + ':' + v[2] for v in r.violations}), r.coverage_gaps)
last = None
for row in r.trace:
    key = (row["session_state"], row["session_reason"], row["controller_ids"], row["controller_phase"], row["action_kind"],
           row["route_id"], row["driver_state"], row["driver_reason"], row["request_generation"])
    if key != last:
        print(row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"], row["session_reason"],
              row["controller_ids"], row["controller_phase"], row["action_kind"], (row["route_id"] or "")[:8],
              "gen", row["request_generation"], "driver", row["driver_state"], row["driver_reason"],
              "sneak" if row["sneaking"] else "")
        last = key
