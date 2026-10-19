"""Run a frozen formal-path navigation matrix and preserve every result."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import time
import traceback

from tests.sim.runner import late_ticks, run
from tests.sim.scenarios import SCENARIOS


SCHEMA = "mc2p.navigation-sim-manifest.v1"
CLASSES = {"positive", "known_failure", "formal_receipt_new_failure",
           "calibrated_input_new_failure"}


def read_manifest(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    document = json.loads(raw)
    if document.get("schema_version") != SCHEMA or not isinstance(document.get("cases"), list):
        raise ValueError("unsupported navigation matrix manifest")
    seen: set[str] = set()
    scenarios = {scenario.name for scenario in SCENARIOS}
    for case in document["cases"]:
        identifier = case.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("matrix case identifiers must be unique")
        seen.add(identifier)
        if case.get("scenario") not in scenarios or case.get("classification") not in CLASSES:
            raise ValueError(f"invalid scenario or classification: {identifier}")
        if (not isinstance(case.get("late_ticks"), list)
                or any(type(value) is not int or value < 0 for value in case["late_ticks"])
                or sorted(set(case["late_ticks"])) != case["late_ticks"]):
            raise ValueError(f"late ticks are not frozen: {identifier}")
        if "late_ticks_seed" in case and case["late_ticks"] != sorted(late_ticks(
                case["late_ticks_probability"], case["late_ticks_seed"],
                case["late_ticks_horizon"])):
            raise ValueError(f"late tick seed does not reproduce list: {identifier}")
        if (not isinstance(case.get("violations"), list)
                or any(item not in {f"I{i}" for i in range(1, 14)}
                       for item in case["violations"])):
            raise ValueError(f"invalid frozen violations: {identifier}")
        event_ticks = case.get("event_ticks", {})
        scheduled = {event.name for event in next(
            scenario for scenario in SCENARIOS
            if scenario.name == case["scenario"]
        ).events}
        if (not isinstance(event_ticks, dict)
                or set(event_ticks) != scheduled
                or not set(case.get("events", [])).issubset(scheduled)
                or any(type(value) is not int or value < 1
                       for value in event_ticks.values())):
            raise ValueError(f"events have no frozen trigger ticks: {identifier}")
    return document, raw


def _git_root(root: Path) -> bool:
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                            cwd=root, text=True, capture_output=True, check=False)
    return (result.returncode == 0
            and Path(result.stdout.strip()).resolve() == root.resolve())


def _commit() -> str:
    root = Path(__file__).resolve().parents[2]
    if not _git_root(root):
        return "unavailable"
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True,
                            capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def _source_identity() -> dict:
    """Identify the executable Python snapshot, including uncommitted edits."""
    root = Path(__file__).resolve().parents[2]
    files = sorted((*root.glob("mc2p/**/*.py"),
                    *root.glob("tests/sim/**/*.py"),
                    root / "tests/navigation_session_fixtures.py"),
                   key=lambda path: path.relative_to(root).as_posix())
    digest = hashlib.sha256()
    count = 0
    for path in files:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        count += 1
    status = (subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal", "--",
         "mc2p", "tests/sim", "tests/navigation_session_fixtures.py"],
        cwd=root, text=True, capture_output=True, check=False,
    ) if _git_root(root) else None)
    return {"scope": "mc2p/**/*.py + tests/sim/**/*.py + tests/navigation_session_fixtures.py",
            "python_files": count, "sha256": digest.hexdigest(),
            "git_dirty": (bool(status.stdout.strip()) if status is not None
                          and status.returncode == 0 else None)}


def run_matrix(manifest: Path, output: Path) -> dict:
    document, raw = read_manifest(manifest)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite matrix output: {output}")
    output.mkdir(parents=True)
    cases = {scenario.name: scenario for scenario in SCENARIOS}
    summary = {
        "schema_version": "mc2p.navigation-sim-result.v2",
        "source_commit": _commit(),
        "source_identity": _source_identity(),
        "baseline_commit": document["source_commit"],
        "review_baseline_commit": document["review_baseline_commit"],
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest": str(manifest),
        "environment": {"python": sys.version, "platform": platform.platform()},
        "counts": {"positive_pass": 0, "known_failure": 0,
                   "formal_receipt_new_failure": 0,
                   "calibrated_input_new_failure": 0, "unexpected": 0},
        "outcome_counts": {
            "task_success": 0,
            "bounded_safe_failure": 0,
            "unexpected_result": 0,
        },
        "cases": [],
    }
    started = time.perf_counter()
    for case in document["cases"]:
        scenario = cases[case["scenario"]]
        frozen = replace(scenario, perturbations=replace(
            scenario.perturbations, late_ticks=frozenset(case["late_ticks"])))
        case_started = time.perf_counter()
        trace_path = output / f"{case['id']}.jsonl"
        try:
            with trace_path.open("w", encoding="utf-8") as trace_file:
                result = run(frozen, trace_sink=lambda row: trace_file.write(
                    json.dumps(row, ensure_ascii=False) + "\n"),
                    event_ticks=case.get("event_ticks", {}))
            violation_codes = sorted({item[1] for item in result.violations})
            event_names = [event.split("@")[0] for event in result.events]
            exact = (result.outcome == case["outcome"]
                     and result.verification_complete
                     and result.reason == case["reason"]
                     and violation_codes == case["violations"]
                     and event_names == case.get("events", [])
                     and result.events == [f"{name}@{at}" for name, at
                                           in case.get("event_ticks", {}).items()
                                           if name in case.get("events", [])])
            if case["classification"] == "positive":
                exact = exact and result.verdict == "PASS"
            payload = {"case": case, "result": asdict(result),
                       "outcome_class": result.outcome_class,
                       "matched_frozen_baseline": exact,
                       "elapsed_seconds": time.perf_counter() - case_started,
                       "requested_late_ticks": case["late_ticks"],
                       "partial_trace_file": trace_path.name,
                       "actual_superseded_requests": [
                           {"tick": event["tick"], "ids": event["superseded"]}
                           for event in (row["command_event"] for row in result.trace)
                           if event["superseded"]],
                       }
            classification = (
                "positive_pass" if exact and case["classification"] == "positive"
                else case["classification"] if exact else "unexpected"
            )
            outcome_class = result.outcome_class
        except Exception as error:
            classification = "unexpected"
            outcome_class = "unexpected_result"
            payload = {"case": case, "matched_frozen_baseline": False,
                       "error": f"{type(error).__name__}: {error}",
                       "traceback": traceback.format_exc(),
                       "partial_trace_file": trace_path.name,
                       "elapsed_seconds": time.perf_counter() - case_started}
        (output / f"{case['id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["counts"][classification] += 1
        summary["outcome_counts"][outcome_class] += 1
        summary["cases"].append({"id": case["id"], "classification": classification,
                                  "outcome_class": outcome_class,
                                  "matched_frozen_baseline": classification != "unexpected",
                                  "elapsed_seconds": payload["elapsed_seconds"]})
    summary["elapsed_seconds"] = time.perf_counter() - started
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = run_matrix(args.manifest, args.output)
    except (ValueError, OSError) as error:
        print(f"matrix setup failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"counts": summary["counts"],
                      "outcome_counts": summary["outcome_counts"],
                      "elapsed_seconds": summary["elapsed_seconds"],
                      "output": str(args.output)}, ensure_ascii=False))
    return 1 if summary["counts"]["unexpected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
