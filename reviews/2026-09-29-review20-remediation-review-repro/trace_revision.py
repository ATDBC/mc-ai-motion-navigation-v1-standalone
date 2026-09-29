# Run from the repository root of a 340cac6 checkout: PYTHONPATH=. python -B <script> <tick> <x,y,z>
"""Trace one goal revision of direct_drop_2 to an arbitrary goal."""
import sys
from dataclasses import replace
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS

at = int(sys.argv[1])
goal = tuple(float(v) for v in sys.argv[2].split(","))
base = next(s for s in SCENARIOS if s.name == "direct_drop_2")


def revise(context):
    context.driver.replace_goal("goal", 2, _goal(goal, context.risk_policy_id), context.clock[0],
                                damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
    context.goal_position = goal


r = run(replace(base, events=[Event("revise", lambda c: c.tick >= at, revise)], max_ticks=200))
print(r.outcome, r.reason, r.ticks, r.final_position, [(t, c, d) for t, c, d in r.violations])
last = None
for row in r.trace:
    key = (row["session_state"], row["session_reason"], row["controller_ids"], row["driver_state"],
           row["on_ground"], row["source_bound"], row["request_generation"])
    if key != last:
        print(row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"], row["session_reason"],
              row["controller_ids"], "gen", row["request_generation"], "driver", row["driver_state"],
              "ground" if row["on_ground"] else "AIR", "bound" if row["source_bound"] else "unbound")
        last = key
