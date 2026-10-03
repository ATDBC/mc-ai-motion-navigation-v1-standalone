"""What does a late goal revision get, during the ending and after the terminal state?  (3c15744)

Section 17.11 of the coordination architecture says a revision that races an accepted ending
request is an expected lifecycle race: update_goal / replace_goal return "not accepted" and do
not raise.  This script runs one ordinary product task, cancels it at tick 12, and then offers
a newer revision on every following tick, recording what the session returns or raises.

    PYTHONPATH=. python -B late_revision_check.py
"""
import json
import pathlib

from mc2p.contracts.common import ContractViolation
from tests.sim.product_cases import product_scenario
from tests.sim.runner import run

manifest = json.loads(pathlib.Path("tests/sim/manifests/navigation-product-r28-v7.json").read_text("utf-8"))
group = next(g for g in manifest["groups"] if g["id"] == "point-normal")
scenario, _ = product_scenario({k: v for k, v in manifest.items() if k != "motion_delivery_profile"}, group, 0)
outcomes, state = [], {"revision": 1, "cancelled": False}


def control_step(context):
    from mc2p.contracts.behavior import BehaviorProfileV0
    session = context.driver.session
    state["session"], state["goal"] = session, context.goal_state
    if context.tick == 12 and not state["cancelled"]:
        session.cancel("late_revision_probe")          # an accepted ending request
        state["cancelled"] = True
    elif state["cancelled"]:
        state["revision"] += 1
        before = session.report.state.value
        try:
            accepted = session.update_goal("goal", state["revision"], context.goal_state)
            outcomes.append((context.tick, before, f"returned {accepted}"))
        except ContractViolation as error:
            outcomes.append((context.tick, before, f"raised ContractViolation: {error}"))
    if context.driver.source is not None:
        context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
    return ()


result = run(scenario, control_step=control_step)
print(f"task outcome: {result.outcome} ({result.reason})")
# The runner stops calling the control step once the driver is terminal; offer one more
# revision to the same session object afterwards, as a follow layer one tick late would.
session = state["session"]
state["revision"] += 1
try:
    accepted = session.update_goal("goal", state["revision"], state["goal"])
    outcomes.append(("after", session.report.state.value, f"returned {accepted}"))
except ContractViolation as error:
    outcomes.append(("after", session.report.state.value, f"raised ContractViolation: {error}"))
seen = set()
for tick, session_state, outcome in outcomes:
    key = (session_state, outcome.split(":")[0])
    if key not in seen:
        seen.add(key)
        print(f"  first at tick {tick!s:>5}: session {session_state:<10} -> {outcome}")
