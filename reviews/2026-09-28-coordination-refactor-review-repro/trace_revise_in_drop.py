"""Trace one goal revision during the committed 5-block drop (budget fits that one drop)."""
import sys
from dataclasses import replace
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS

at = int(sys.argv[1]) if len(sys.argv) > 1 else 37
target = tuple(float(v) for v in sys.argv[2].split(",")) if len(sys.argv) > 2 else (1.5, 59.0, 4.5)
budget_scale = float(sys.argv[3]) if len(sys.argv) > 3 else None
base = next(s for s in SCENARIOS if s.name == "direct_drop_5_budget_2")
if budget_scale is not None:
    base = replace(base, damage_points=budget_scale)


def revise(context):
    goal = _goal(target, context.risk_policy_id)
    context.driver.replace_goal("goal", 2, goal, context.clock[0],
                                damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
    context.goal_state = goal
    context.goal_position = target


result = run(replace(base, events=[Event("revise", lambda c: c.tick >= at, revise)], max_ticks=200))
print(result.outcome, result.reason, result.ticks, result.damage, result.final_position,
      sorted({v[1] + ':' + v[2] for v in result.violations}))
last = None
for row in result.trace:
    key = (row["session_state"], row["session_reason"], row["controller_ids"], row["action_kind"], row["route_id"],
           row["on_ground"], row["request_generation"], row["risk_available_points"])
    if key != last:
        print(row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"], row["session_reason"],
              row["controller_ids"], row["action_kind"], (row["route_id"] or "")[:8], "gen", row["request_generation"],
              "ground" if row["on_ground"] else "air", "avail", row["risk_available_points"])
        last = key
