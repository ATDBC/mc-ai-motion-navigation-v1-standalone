# Run from the repository root of an eb4653c checkout: PYTHONPATH=. python -B <script>
"""Goal revision at the drop start followed by one late tick (the round-19 P1-2 sweep, 'late' variant)."""
import sys
from dataclasses import replace
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS

at = int(sys.argv[1]) if len(sys.argv) > 1 else 36
target = (1.5, 59.0, 4.5)
base = next(s for s in SCENARIOS if s.name == "direct_drop_5_budget_2")


def revise(context):
    goal = _goal(target, context.risk_policy_id)
    context.driver.replace_goal("goal", 2, goal, context.clock[0],
                                damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
    context.goal_state = goal
    context.goal_position = target


r = run(replace(base, events=[Event("revise", lambda c: c.tick >= at, revise)],
                perturbations=Perturbations(late_ticks=frozenset({at + 1})), max_ticks=300))
print(r.outcome, r.reason, r.ticks, r.final_position, [(t, c, d) for t, c, d in r.violations])
last = None
for row in r.trace:
    key = (row["session_state"], row["session_reason"], row["controller_ids"], row["controller_phase"],
           row["action_kind"], row["on_ground"], row["driver_state"], row["request_generation"])
    if key != last:
        print(row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"], row["session_reason"],
              row["controller_ids"], row["controller_phase"], row["action_kind"], "gen", row["request_generation"],
              "ground" if row["on_ground"] else "AIR", "driver", row["driver_state"], "sneak" if row["sneaking"] else "")
        last = key
