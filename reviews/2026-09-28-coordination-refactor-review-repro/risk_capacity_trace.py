"""Trace the leg at which the shared task ledger reaches its 64-record capacity."""
from mc2p.motion_nav.motion_risk import TaskDamageBudget, TaskRiskLedger
from tests.sim.runner import Scenario, run
from tests.sim.scenarios import drop_ledge

ledger = TaskRiskLedger("goal", TaskDamageBudget("no_expected_damage", 0.0))
scenario = Scenario("leg", drop_ledge(2), (.5, 64.0, 2.5), (.5, 62.0, 4.5), yaw_degrees=180, max_ticks=300)
for _ in range(10):
    run(scenario, risk_ledger=ledger)
r = run(scenario, risk_ledger=ledger)
print(r.outcome, r.reason, r.ticks, r.final_position, [(t, c, d) for t, c, d in r.violations])
last = None
for row in r.trace:
    key = (row["session_state"], row["session_reason"], row["controller_ids"], row["driver_state"], row["sneaking"])
    if key != last:
        print(row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"], row["session_reason"],
              row["controller_ids"], "driver", row["driver_state"], row["driver_reason"], "sneak" if row["sneaking"] else "",
              "records", len(row["risk_actions"]))
        last = key
