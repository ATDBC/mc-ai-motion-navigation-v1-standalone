"""Paired formal-path regression cases and a function-entry migration gate.

The existing scenario runners own injection and assertions. This tool only
records their observations and which migration functions each case enters.
It does not contribute cases to product success-rate denominators.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, is_dataclass, replace
from functools import lru_cache
import gzip
import json
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.navigation_coordination_metrics import coordination_inventory, digest, source_fingerprint
from tests.sim.runner import Event, run
from tests.sim.scenarios import SCENARIOS


@lru_cache(maxsize=1)
def _functions():
    rows = coordination_inventory()["functions"]
    return {(str(ROOT / row["file"]), row["id"].split(".", 1)[1]): row["id"] for row in rows}


def jobs():
    from tests.sim.run_navigation_interrupt_matrix import expand_cases
    from tests.sim.event_sequences import generate_sequence
    interrupt = json.loads((ROOT / "tests/sim/manifests/navigation-coordination-interrupt-late.json").read_text())
    for case in expand_cases(interrupt):
        yield "interrupt/" + case.identifier, "interrupt", asdict(case)
    config = json.loads((ROOT / "tests/sim/manifests/navigation-coordination-event-sequences.json").read_text())
    for scene in config["scenarios"]:
        for seed in config["seeds"]:
            yield f"events/{scene}-{seed}", "events", asdict(generate_sequence(
                seed, event_count=config["event_count"], scenario=scene, max_ticks=config["maximum_ticks"]))
    config = json.loads((ROOT / "tests/sim/manifests/navigation-async-work-r27-hardening.json").read_text())
    for family in config["families"]:
        for seed in range(config["seed_start"], config["seed_end"] + 1):
            yield f"async/{family['name']}-{seed}", "async", (family, seed)


def _execute(kind, data):
    if kind == "faults":
        from tests.sim.migration_faults import run_fault_case
        result = run_fault_case(data)
        return {"passed": True, "task_outcome": result.outcome,
                "reason": result.reason, "violations": result.violations,
                "verification": asdict(result.verification), "trace": result.trace}
    if kind == "async":
        from tests.sim.async_work_sequences import run_gap_sequence, run_information_sequence, run_placement_sequence
        family, seed = data
        if family["name"] == "information_exit":
            result = run_information_sequence(seed)
        elif family["name"] == "gap_successor":
            result = run_gap_sequence(seed)
        else:
            result = run_placement_sequence(seed, mode="failed" if family["name"] == "placement_revocation" else None,
                                            partial=family["name"] == "placement_revocation")
        result["passed"] &= set(family["events"]).issubset(result["events"]) and result["task_outcome"] == family["outcome"]
        return result
    if kind == "events":
        from tests.sim.event_sequences import EventKind, GeneratedEvent, GeneratedSequence, run_sequence
        sequence = GeneratedSequence(data["seed"], data["scenario"], data["max_ticks"], tuple(
            GeneratedEvent(EventKind(item["kind"]), item["tick"]) for item in data["events"]))
        outcome = run_sequence(sequence)
        if outcome.result is None:
            raise RuntimeError(outcome.exception)
        result = outcome.result
        passed = not outcome.failed_gate
    else:
        from tests.sim.backend import Perturbations
        from tests.sim.run_navigation_interrupt_matrix import _cancel, _revise_to
        base = next(item for item in SCENARIOS if item.name == data["scenario"])
        tick = data["interrupt_tick"]
        late = data["late_offset"]
        result = run(replace(base, name=data["scenario"], max_ticks=data["maximum_ticks"],
            events=[Event(data["interruption"], lambda c: c.tick >= tick,
                          _revise_to(tuple(data["revised_goal"])) if data["interruption"] == "revise" else _cancel)],
            perturbations=Perturbations(late_ticks=frozenset() if late is None else frozenset({tick + late}))))
        passed = result.verification_complete and not result.violations and result.outcome in {"success", "failed", "cancelled"}
    return {"passed": passed, "task_outcome": result.outcome, "reason": result.reason,
            "violations": result.violations, "verification": asdict(result.verification), "trace": result.trace}


def observe(job):
    identifier, kind, data = job
    hits, functions = set(), _functions()
    def profile(frame, event, arg):
        if event == "call":
            match = functions.get((frame.f_code.co_filename, frame.f_code.co_name))
            if match is not None:
                hits.add(match)
    old_profile = sys.getprofile()
    error = None
    try:
        sys.setprofile(profile)
        result = _execute(kind, data)
    except Exception:
        error = traceback.format_exc()
        result = {"passed": False, "trace": []}
    finally:
        sys.setprofile(old_profile)
    # Use common observations, not random work IDs or reason vocabulary.
    fields = ("movement_tick", "tick", "position", "velocity", "on_ground", "pose",
              "applied_movement", "input_window", "source_bound", "source_owned",
              "controller_ids", "controller_phase", "action_kind", "actual_applied")
    signature = {"outcome": result.get("task_outcome"), "passed": result["passed"],
                 "verification_complete": result.get("verification", {}).get("complete"),
                 "events": result.get("events"), "violations": result.get("violations", result.get("body_violations")),
                 "trace": [{key: row[key] for key in fields if key in row} for row in result["trace"]]}
    return {"id": identifier, "kind": kind, "input": data, "functions_entered": sorted(hits),
            "signature": signature, "passed": result["passed"], "exception": error}


def _encode(value):
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"unsupported evidence type: {type(value).__name__}")


def collect(output, workers=4, limit=None, resume_from=None, faults_only=False):
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    if faults_only:
        from tests.sim.migration_faults import FAULT_CASES
        tasks = [("faults/" + name, "faults", name) for name in FAULT_CASES]
    else:
        tasks = list(jobs())
    if limit is not None:
        tasks = tasks[:limit]
    rows, hits = [], {}
    reused = {}
    if resume_from is not None:
        for index, task in enumerate(tasks, 1):
            path = resume_from / f"case-{index:04d}.json.gz"
            if not path.exists():
                continue
            try:
                with gzip.open(path, "rt", encoding="utf-8") as stream:
                    row = json.load(stream)
            except (OSError, EOFError, ValueError):
                continue
            if row["id"] != task[0] or json.dumps(row["input"], sort_keys=True) != json.dumps(task[2], default=_encode, sort_keys=True):
                raise ValueError("resume input differs from the frozen case")
            reused[index] = row
        print(f"reusing {len(reused)} intact case records; original files remain unchanged", flush=True)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        pending = iter(pool.map(observe, [job for i, job in enumerate(tasks, 1) if i not in reused]))
        for index in range(1, len(tasks) + 1):
            row = reused[index] if index in reused else next(pending)
            path = output / f"case-{index:04d}.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                json.dump(row, stream, default=_encode)
            rows.append({"id": row["id"], "file": path.name, "sha256": digest(path.read_bytes()),
                         "passed": row["passed"], "exception": row["exception"]})
            for function in row["functions_entered"]:
                hits.setdefault(function, []).append(row["id"])
            if index % 100 == 0:
                print(f"migration cases {index}/{len(tasks)}", flush=True)
    inventory = coordination_inventory()
    report = {"cases": rows, "product_denominator": False, "complete_suite": limit is None,
              "production": source_fingerprint(["mc2p/**/*.py", "config/motion-navigation/*.json"]),
              "harness": source_fingerprint(["tests/sim/**/*.py", "scripts/navigation*evidence.py"]),
              "functions": {row["id"]: {"entered_by": hits.get(row["id"], []),
                            "migration_allowed": bool(hits.get(row["id"]))} for row in inventory["functions"]},
              "reused_records": len(reused), "reused_from": None if resume_from is None else str(resume_from),
              "limits": "Function entry is necessary, not sufficient: critical branches require behaviour assertions before migration."}
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return {"cases": len(rows), "failed": sum(not row["passed"] for row in rows),
            "uncovered_functions": [name for name, value in report["functions"].items() if not value["migration_allowed"]]}


def paired(old, new):
    reports = [json.loads((path / "summary.json").read_text("utf-8")) for path in (old, new)]
    # The collector's CLI and coverage reporting cannot affect simulated input.
    # Compare the actual scenario/physics-backend sources; keep collector hashes
    # in both reports for independent inspection of the common signature format.
    simulation_sources = [{name: value for name, value in report["harness"]["files"].items()
                           if name.startswith("tests/sim/")} for report in reports]
    if simulation_sources[0] != simulation_sources[1]:
        raise ValueError("paired regression harness differs")
    if [r["id"] for r in reports[0]["cases"]] != [r["id"] for r in reports[1]["cases"]]:
        raise ValueError("paired regression denominator differs")
    different = []
    for a, b in zip(*[report["cases"] for report in reports]):
        values = []
        for root, row in ((old, a), (new, b)):
            path = root / row["file"]
            if digest(path.read_bytes()) != row["sha256"]:
                raise ValueError("regression evidence checksum mismatch")
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                values.append(json.load(stream))
        if values[0]["input"] != values[1]["input"]:
            raise ValueError("paired regression inputs differ")
        if any(value["exception"] for value in values) or values[0]["signature"] != values[1]["signature"]:
            different.append(a["id"])
    return {"pairs": len(reports[0]["cases"]), "differences": different}


def check_coverage(root, required):
    report = json.loads((root / "summary.json").read_text("utf-8"))
    return {"migration_allowed": all(report["functions"].get(name, {}).get("migration_allowed", False)
                                      for name in required),
            "uncovered": [name for name in required if not report["functions"].get(name, {}).get("migration_allowed", False)]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--faults-only", action="store_true")
    parser.add_argument("--compare", type=Path, nargs=2)
    parser.add_argument("--check-coverage", type=Path)
    parser.add_argument("--require-function", nargs="+", action="extend", default=[])
    args = parser.parse_args()
    report = (check_coverage(args.check_coverage, args.require_function) if args.check_coverage else
              paired(*args.compare) if args.compare else collect(args.output, args.workers, args.limit, args.resume_from, args.faults_only))
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(int(report.get("failed", 0) > 0 or bool(report.get("differences"))
                         or report.get("migration_allowed") is False))
