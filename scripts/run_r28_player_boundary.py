"""Frozen player-position boundary matrix; bounded refusals are not successes."""
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
import argparse
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.navigation_coordination_metrics import digest, source_fingerprint
from tests.sim.continuous_height_matrix import matrix_scenario
from tests.sim.product_cases import PLAYER_CASES, player_layout
from tests.sim.runner import Scenario, run


def evaluate(job):
    family, direction, speed, late, directory = job
    scene, start, goal = player_layout(family)
    scenario = matrix_scenario(Scenario(family, scene, start, goal),
        direction=direction, speed_blocks_per_second=speed,
        seed=21001, late_probability=late)
    result = run(scenario)
    identifier = f"{family}-{direction}-{speed:g}-{'late' if late else 'normal'}"
    record = dict(id=identifier, family=family, direction=direction,
        speed=speed, late_probability=late, outcome=result.outcome,
        reason=result.reason, task_success=result.outcome == "success",
        bounded=result.outcome in {"success", "failed", "cancelled"},
        safety_events=result.violations, complete=result.verification_complete,
        ticks=result.ticks)
    path = Path(directory) / (identifier + ".json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(dict(record=record, trace=result.trace), stream)
    record.update(file=path.name, sha256=digest(path.read_bytes()))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    jobs = [(f, d, s, p, str(args.output)) for f in PLAYER_CASES
            for d in ("south", "east", "north", "west")
            for s in (0., 1., 3.) for p in (0., .2)]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(evaluate, jobs))
    report = dict(cases=rows, product_denominator=False,
        task_success=sum(r["task_success"] for r in rows),
        unexpected=sum(not r["bounded"] or bool(r["safety_events"])
                       or not r["complete"] for r in rows),
        production=source_fingerprint(["mc2p/**/*.py", "config/motion-navigation/*.json"]))
    (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"cases", "production"}}))
    return int(report["unexpected"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
