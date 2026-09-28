"""One long-lived task (same task id, same TaskRiskLedger, e.g. a follow or melee task) repeats a zero-damage
2-block drop that needs a 180-degree heading alignment.  No damage is ever expected or taken."""
from mc2p.motion_nav.motion_risk import TaskDamageBudget, TaskRiskLedger
from tests.sim.runner import Scenario, run
from tests.sim.scenarios import drop_ledge

ledger = TaskRiskLedger("goal", TaskDamageBudget("no_expected_damage", 0.0))
for leg in range(1, 16):
    r = run(Scenario(f"leg{leg}", drop_ledge(2), (.5, 64.0, 2.5), (.5, 62.0, 4.5), yaw_degrees=180, max_ticks=300),
            risk_ledger=ledger)
    print(f"leg {leg:>2}: {r.outcome:<8} {r.reason:<34} damage={r.damage:g} ledger_records={len(ledger.snapshot_actions())}",
          flush=True)
    if r.outcome != "success":
        break
