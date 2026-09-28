"""Count session state writes that leave a terminal state (FAILED/CANCELLED/COMPLETE), or abandon STOPPING,
for a non-terminal state.
A lifecycle table with absorbing terminal states would reject every one of these writes."""
from collections import Counter
from dataclasses import replace
import traceback
from mc2p.motion_nav import navigation_session as ns
from mc2p.motion_nav.motion_risk import TaskDamageBudget, TaskRiskLedger
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, Scenario, _goal, late_ticks, run
from tests.sim.scenarios import SCENARIOS, drop_ledge

S = ns.NavigationSessionState
TERMINAL = {S.FAILED, S.CANCELLED, S.COMPLETE}
prop = ns.NavigationSession._state
writes = Counter()
sites = Counter()
closing = Counter()


def setter(self, value):
    old = self._lifecycle.state
    leaves_terminal = old in TERMINAL and value not in TERMINAL and value is not S.CLOSED
    abandons_stop = old is S.STOPPING and value not in TERMINAL and value not in {S.STOPPING, S.CLOSED}
    if leaves_terminal or abandons_stop:
        stack = traceback.extract_stack(limit=8)[:-1]
        if any(f.name == "close" for f in stack):
            closing[(old.value, value.value)] += 1
        else:
            writes[(old.value, value.value)] += 1
            sites[" <- ".join(f"{f.name}:{f.lineno}" for f in reversed(stack[-3:]))] += 1
    prop.fset(self, value)


ns.NavigationSession._state = property(prop.fget, setter)


def revise_to(position):
    def action(context):
        goal = _goal(position, context.risk_policy_id)
        context.driver.replace_goal("goal", 2, goal, context.clock[0],
                                    damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
        context.goal_state = goal
        context.goal_position = position
    return action


runs = []
for scenario in SCENARIOS:
    runs.append((scenario.name, lambda s=scenario: run(s)))
drop5 = next(s for s in SCENARIOS if s.name == "direct_drop_5_budget_2")
runs.append(("drop5_revise_at_37", lambda: run(replace(drop5, events=[Event("r", lambda c: c.tick >= 37, revise_to((1.5, 59.0, 4.5)))], max_ticks=200))))
runs.append(("corner_landing_x1.5", lambda: run(Scenario("c", drop_ledge(2), (.5, 64.0, .5), (1.5, 62.0, 4.5), max_ticks=200))))
drop2 = next(s for s in SCENARIOS if s.name == "direct_drop_2")
for seed in range(280001, 280021):
    runs.append((f"drop2_late20_{seed}", lambda seed=seed: run(replace(drop2, perturbations=Perturbations(late_ticks=late_ticks(.2, seed))))))


def capacity_leg():
    ledger = TaskRiskLedger("goal", TaskDamageBudget("no_expected_damage", 0.0))
    scenario = Scenario("leg", drop_ledge(2), (.5, 64.0, 2.5), (.5, 62.0, 4.5), yaw_degrees=180, max_ticks=200)
    for _ in range(10):
        run(scenario, risk_ledger=ledger)
    return run(scenario, risk_ledger=ledger)


runs.append(("risk_capacity_leg11", capacity_leg))
affected = []
for name, job in runs:
    before = sum(writes.values())
    r = job()
    if sum(writes.values()) > before:
        affected.append((name, r.outcome, r.reason, sum(writes.values()) - before))
print(f"runs={len(runs)} runs_with_terminal_left={len(affected)} total_writes={sum(writes.values())}")
for item in affected:
    print("  ", item)
print("transitions:", dict(writes))
print("write sites:", dict(sites))
print("writes made by close() on an already terminal session (not counted above):", dict(closing))
