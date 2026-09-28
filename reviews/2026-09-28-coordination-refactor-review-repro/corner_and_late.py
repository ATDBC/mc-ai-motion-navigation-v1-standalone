"""(1) landing columns across the 3-wide ledge, no goal revision;
(2) a single late tick at each point of the frozen drop scenarios."""
from dataclasses import replace
from tests.sim.backend import Perturbations
from tests.sim.runner import Scenario, run
from tests.sim.scenarios import SCENARIOS, drop_ledge

print("== landing column sweep (drop_ledge, no revision) ==")
for height, budget in ((2, 0.0), (5, 2.0)):
    for x in (-0.5, 0.5, 1.5):
        scenario = Scenario(f"drop{height}_x{x}", drop_ledge(height), (.5, 64.0, .5),
                            (x, 64.0 - height, 4.5), damage_points=budget, max_ticks=300)
        r = run(scenario)
        print(f"drop{height} goal_x={x:<5} {r.outcome:<10} {r.reason:<36} ticks={r.ticks:<4} "
              f"pos={r.final_position} damage={r.damage:g} violations={sorted({v[1] for v in r.violations})}")

print("== single late tick sweep ==")
for name in ("direct_drop_2", "direct_drop_5_budget_2", "far_landing_L_walkway", "stair_descent_4", "half_steps_up_down"):
    base = next(s for s in SCENARIOS if s.name == name)
    clean = run(base)
    failures = []
    for t in range(2, clean.ticks):
        r = run(replace(base, perturbations=Perturbations(late_ticks=frozenset({t}))))
        if r.outcome != "success" or r.violations:
            failures.append((t, r.outcome, r.reason, sorted({v[1] for v in r.violations})))
    print(f"{name:<28} clean={clean.outcome}@{clean.ticks:<4} single-late failures {len(failures)}/{clean.ticks - 2}: "
          + "; ".join(f"t{t}:{o}/{rs}{('/' + ','.join(v)) if v else ''}" for t, o, rs, v in failures))
