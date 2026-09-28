# Run from the repository root of an eb4653c checkout: PYTHONPATH=. python -B <script>
"""Interruption x phase x one late tick on the frozen drop scenarios.  Counts runs that never reach a
terminal state (stuck at the tick cap) and invariant violations."""
from collections import Counter
from dataclasses import replace
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS

TERMINAL = {"success", "failed", "cancelled"}


def revise_to(position):
    def action(context):
        goal = _goal(position, context.risk_policy_id)
        context.driver.replace_goal("goal", 2, goal, context.clock[0],
                                    damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
        context.goal_state = goal
        context.goal_position = position
    return action


def cancel(context):
    context.driver.release("harness_cancel")


for name, other in (("direct_drop_2", (-0.5, 62.0, 4.5)), ("direct_drop_5_budget_2", (-0.5, 59.0, 4.5))):
    base = next(s for s in SCENARIOS if s.name == name)
    for label, action in (("revise", revise_to(other)), ("cancel", cancel)):
        tally = Counter()
        stuck = []
        for at in range(30, 46):
            for delay in (None, 1, 2):
                late = frozenset() if delay is None else frozenset({at + delay})
                r = run(replace(base, events=[Event(label, lambda c, at=at: c.tick >= at, action)],
                                perturbations=Perturbations(late_ticks=late), max_ticks=300))
                terminal = r.outcome in TERMINAL
                tally["terminal" if terminal else "NOT terminal"] += 1
                if r.violations:
                    tally["with violations"] += 1
                if not terminal or r.violations:
                    stuck.append(f"@{at}+late{delay}:{r.outcome}/{r.reason}/{','.join(sorted({v[1] for v in r.violations}))}")
        print(f"{name:<24} {label:<7} runs={sum(v for k, v in tally.items() if k in ('terminal', 'NOT terminal'))} "
              f"{dict(tally)}  {'; '.join(stuck)}", flush=True)
