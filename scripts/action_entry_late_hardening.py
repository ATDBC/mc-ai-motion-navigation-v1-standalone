"""D093 deterministic paired input-late handoff evidence on the formal chain."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import gzip
import platform
import subprocess
import sys

from tests.sim.backend import Perturbations, Scene
from tests.sim.runner import Scenario, late_ticks, run
from mc2p.contracts.behavior import BehaviorProfileV0

STONE = "minecraft:stone"
FAMILIES = ("column_outer_turn", "column_outer_aligned", "turn_jumpup", "turn_drop",
            "column_landing_turn", "column_landing_aligned")
ROOT = Path(__file__).resolve().parents[1]


def input_manifest(families, seeds, key_ticks=False):
    inputs = []
    for family in families:
        scenario = scenario_for(family, None)
        inputs.append({"family": family, "solids": [[*cell, material] for cell, material
                       in sorted(scenario.scene.solids.items())], "volume": scenario.scene.volume,
                       "start": scenario.start, "goal": scenario.goal, "yaw_degrees": scenario.yaw_degrees,
                       "max_ticks": scenario.max_ticks})
    value = {"families": inputs, "seeds": list(range(1, seeds + 1)),
             "late_probability": .2, "key_ticks": key_ticks}
    return {**value, "sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()}


def scenario_for(family: str, seed: int | None, *, single_late_tick: int | None = None) -> Scenario:
    if family.startswith("column_"):
        solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
        solids.update({(x, 64, z): STONE for x in (3, 4) for z in (9, 10)})
        goal = (3.5, 65., 9.5) if "landing" in family else (4.7, 65., 10.7)
        yaw = -90. if family.endswith("aligned") else 0.
        start = (.5, 64., .5)
        volume = ((-7, 8), (60, 69), (-5, 14))
    elif family == "turn_jumpup":
        solids = {(0, 63, z): STONE for z in range(-1, 10)}
        solids.update({(x, 63, 9): STONE for x in range(9)})
        solids.update({(x, 64, 9): STONE for x in range(3, 9)})
        start, goal, yaw = (.5, 64., .5), (5.5, 65., 9.5), 0.
        volume = ((-9, 12), (60, 70), (-9, 14))
    elif family == "turn_drop":
        solids = {(x, 63, z): STONE for x in range(-4, 9) for z in range(-1, 12)}
        solids.update({(x, 64, z): STONE for x in range(-4, 3) for z in range(-1, 12)})
        start, goal, yaw = (.5, 65., .5), (5.5, 64., 9.5), 0.
        volume = ((-9, 12), (60, 70), (-9, 14))
    else:
        raise ValueError(family)
    ticks = (frozenset({single_late_tick}) if single_late_tick is not None else
             frozenset() if seed is None else late_ticks(.2, seed))
    return Scenario(f"D093/{family}/{seed}/{single_late_tick}", Scene(solids, volume),
                    start, goal, yaw, max_ticks=400,
                    perturbations=Perturbations(late_ticks=ticks))


def run_case(family, seed, *, single_late_tick=None):
    scenario = scenario_for(family, seed, single_late_tick=single_late_tick)
    reports = []

    def control_step(context):
        context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
        if context.session.report.terminal:
            reports.append(context.session.report)
        return ()

    result = run(scenario, control_step=control_step)
    failure_cause = None if not reports else getattr(reports[-1], 'failure_cause', None)
    return {"id": scenario.name, "family": family, "seed": seed,
            "single_late_tick": single_late_tick,
            "late_ticks": sorted(scenario.perturbations.late_ticks),
            "outcome": result.outcome, "reason": result.reason, "ticks": result.ticks,
            "final_position": result.final_position, "damage": result.damage,
            "violations": result.violations, "trace": result.trace,
            "task_failure_cause": None if failure_cause is None else failure_cause.value}


def _run_arguments(arguments):
    family, seed, tick = arguments
    return run_case(family, seed, single_late_tick=tick)


def collect(output: Path, seeds: int, families=FAMILIES, key_ticks=False, workers=1):
    output.mkdir(parents=True, exist_ok=False)
    (output / "inputs.json").write_text(json.dumps(input_manifest(families, seeds, key_ticks), indent=2) + "\n", encoding="utf-8")
    sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
               ('mc2p/motion_nav/fixed_route.py', 'mc2p/motion_nav/action_route_executor.py',
                'mc2p/motion_nav/navigation_session.py')}
    rows = []
    tasks = []
    for family in families:
        cases = [(seed, None) for seed in range(1, seeds + 1)]
        if key_ticks:
            normal = run_case(family, None)
            first = next((f["movement_tick"] for f in normal["trace"]
                          if f["applied_movement"]["jump"] or f["action_kind"] == "ControlledDropSegment"), None)
            if first is not None:
                departure = next((f["movement_tick"] for f in normal["trace"] if not f["on_ground"]), first)
                cases = [(None, tick) for tick in range(max(1, first - 8), max(first, departure) + 1)]
        tasks.extend((family, seed, tick) for seed, tick in cases)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        with gzip.open(output / "runs.jsonl.gz", "wt", encoding="utf-8") as stream:
            for row in pool.map(_run_arguments, tasks):
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
                rows.append({k: v for k, v in row.items() if k != "trace"})
    for family in families:
        print(family, dict(Counter(row["outcome"] for row in rows if row["family"] == family)), flush=True)
    summary = {"source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
               "source_fingerprints": sources, "formal_platform": "windows",
               "measured_platform": platform.platform(), "python": sys.version,
               "python_executable": sys.executable,
               "seeds": seeds, "key_ticks": key_ticks,
               "families": {family: {"total": len(group),
                   "outcomes": dict(Counter(row["outcome"] for row in group)),
                   "reasons": dict(Counter(row["reason"] for row in group)),
                   "damage": sum(row["damage"] for row in group),
                   "violations": sum(len(row["violations"]) for row in group)}
                   for family in families for group in [[r for r in rows if r["family"] == family]]}}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (output / "index.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--families", nargs="*", default=FAMILIES)
    parser.add_argument("--key-ticks", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1)
    args = parser.parse_args()
    collect(args.output, args.seeds, args.families, args.key_ticks, args.workers)


if __name__ == "__main__":
    main()
