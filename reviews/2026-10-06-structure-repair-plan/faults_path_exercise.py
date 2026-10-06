"""Path counts for the 4 supplementary fault cases and the world-change cases.  (1f0fefe)

    PYTHONPATH=.:<s0-review-dir>:. python -B faults_path_exercise.py

Reuses the S0 review's `path_exercise_probe.install` (call counting only).  The S0 review table
had no column for the supplementary faults; this fills it in.
"""
import collections
from dataclasses import replace

import path_exercise_probe as probe


def main():
    probe.install()
    from tests.sim.backend import Perturbations
    from tests.sim.migration_faults import FAULT_CASES, run_fault_case
    from tests.sim.runner import run
    from tests.sim.scenarios import SCENARIOS
    keys = [f"{c + '.' if c else ''}{f}" for _, c, f in probe.ENTRIES]
    columns = {}
    for name in FAULT_CASES:
        probe.COUNTS.clear()
        run_fault_case(name)
        columns[name] = dict(probe.COUNTS)
    base = next(s for s in SCENARIOS if s.name == "flat_walk")
    probe.COUNTS.clear()
    run(replace(base, name="world-change-block", perturbations=Perturbations(
        world_edits={10: {(0, 64, 5): "minecraft:stone"}})))
    columns["walk+block placed (tick 10)"] = dict(probe.COUNTS)
    print(f"{'entry point':<44}" + "".join(f"{n[:26]:>28}" for n in columns))
    for key in keys:
        print(f"{key:<44}" + "".join(f"{columns[n].get(key, 0):>28}" for n in columns))


if __name__ == "__main__":
    main()
