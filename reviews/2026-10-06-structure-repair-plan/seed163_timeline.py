"""Timeline of the R28-C-05 regression case (drop-late, seed 163) around the failure.  (1f0fefe)

    PYTHONPATH=. python -B seed163_timeline.py
"""
import json
from pathlib import Path

from tests.sim.product_cases import product_scenario
from tests.sim.runner import run

manifest = json.loads(Path("tests/sim/manifests/navigation-product-r28-v4.json").read_text("utf-8"))
group = next(g for g in manifest["groups"] if g["id"] == "drop-late")
scenario, params = product_scenario(manifest, group, 163)
r = run(scenario)
print(f"case {params['case']} goal {scenario.goal} authorised damage {scenario.damage_points}")
print(f"outcome {r.outcome} {r.reason} damage {r.damage} violations {r.violations} final {r.final_position}")
landed = False
for row in r.trace:
    t = row.get("movement_tick")
    if 37 <= t <= 50 or (t > 50 and row["on_ground"] and not landed):
        landed = t > 50
        applied = row.get("applied_movement") or {}
        print(f"  tick {t:>3} on_ground={str(row['on_ground']):<5} y={row['position'][1]:.2f} "
              f"forward={applied.get('forward')} session={row.get('session_state')} driver={row.get('driver_reason')}")
