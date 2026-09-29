# Run from the repository root of a 340cac6 checkout: PYTHONPATH=. python -B <script> <period>
"""Goal revised every <period> ticks between two resolvable goals on the lower floor of direct_drop_2
(a moving follow or melee target produces this pattern)."""
import sys
from dataclasses import replace
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS

period = int(sys.argv[1]) if len(sys.argv) > 1 else 13
goals = ((-0.5, 62.0, 4.5), (0.5, 62.0, 4.5))
base = next(s for s in SCENARIOS if s.name == "direct_drop_2")
log = []


def make(index, goal):
    def action(context):
        before = (context.diagnostics.state.value, context.diagnostics.reason)
        try:
            context.driver.replace_goal("goal", index + 2, _goal(goal, context.risk_policy_id), context.clock[0],
                                        damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
            context.goal_position = goal
            log.append((context.tick, goal, before, "accepted"))
        except Exception as error:  # noqa: BLE001
            log.append((context.tick, goal, before, f"RAISED {error}"))
            raise
    return action


events = [Event(f"flap{i}", lambda c, at=at: c.tick >= at, make(i, goals[i % 2]))
          for i, at in enumerate(range(12, 160, period))]
try:
    r = run(replace(base, events=events, max_ticks=400))
    print(r.outcome, r.reason, r.ticks, r.final_position, [(t, c, d) for t, c, d in r.violations])
    last = None
    for row in r.trace:
        key = (row["session_state"], row["session_reason"], row["controller_ids"], row["driver_state"], row["on_ground"])
        if key != last:
            print(" ", row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"],
                  row["session_reason"], row["controller_ids"], "goal_rev", row["goal_revision"],
                  "ground" if row["on_ground"] else "AIR")
            last = key
except Exception as error:  # noqa: BLE001
    print("run aborted:", type(error).__name__, error)
for item in log:
    print("revision", item)
