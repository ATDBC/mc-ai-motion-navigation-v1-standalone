"""Run and preserve the frozen random-event navigation gate."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

from tests.sim.event_sequences import (
    _document,
    generate_sequence,
    run_sequence,
    shrink_sequence,
)


SCHEMA = "mc2p.navigation-event-sequence-manifest.v1"


def _increment(table: dict, key: str, field: str, amount: int = 1) -> None:
    row = table.setdefault(key, {})
    row[field] = row.get(field, 0) + amount


def _result_class(outcome) -> str:
    if outcome.exception is not None:
        return "public_exception"
    if outcome.result is None:
        return "missing_result"
    if outcome.result.violations:
        return "invariant_violation"
    if outcome.result.outcome == "success":
        return "task_success"
    if outcome.result.reason == "navigation_internal_contract_failure":
        return "internal_contract_failure"
    if outcome.result.outcome in {"failed", "cancelled"}:
        return f"bounded_{outcome.result.outcome}"
    return "non_terminal_result"


def _result_key(outcome) -> tuple[str, str]:
    if outcome.exception is not None:
        return "exception", _result_class(outcome)
    if outcome.result is None:
        return "missing", _result_class(outcome)
    return outcome.result.outcome, outcome.result.reason


def _is_declared_result(outcome, allowed_results: set[tuple[str, str]]) -> bool:
    return _result_key(outcome) in allowed_results


def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def run_manifest(manifest: Path, output_root: Path) -> dict:
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite event sequence output: {output_root}")
    raw = manifest.read_bytes()
    config = json.loads(raw)
    if config.get("schema_version") != SCHEMA:
        raise ValueError("unsupported event sequence manifest")
    seeds = config.get("seeds")
    event_count = config.get("event_count")
    maximum_ticks = config.get("maximum_ticks")
    scenarios = config.get("scenarios")
    allowed_results_document = config.get("allowed_results")
    if scenarios is None:
        scenarios = [config.get("scenario")]
    if (not isinstance(seeds, list) or not seeds
            or any(type(seed) is not int for seed in seeds)
            or len(set(seeds)) != len(seeds)
            or type(event_count) is not int
            or type(maximum_ticks) is not int
            or not isinstance(scenarios, list) or not scenarios
            or any(type(scenario) is not str or not scenario
                   for scenario in scenarios)
            or len(set(scenarios)) != len(scenarios)
            or not isinstance(allowed_results_document, dict)
            or set(allowed_results_document) != set(scenarios)):
        raise ValueError("invalid event sequence manifest")
    allowed_results: dict[str, set[tuple[str, str]]] = {}
    for scenario in scenarios:
        rows = allowed_results_document[scenario]
        if (not isinstance(rows, list) or not rows
                or any(
                    not isinstance(row, list) or len(row) != 2
                    or any(type(value) is not str or not value for value in row)
                    for row in rows
                )):
            raise ValueError("invalid allowed event-sequence results")
        allowed_results[scenario] = {tuple(row) for row in rows}
    output_root.mkdir(parents=True)
    counts = Counter()
    by_scenario: dict[str, dict[str, int]] = {}
    by_event_kind: dict[str, dict[str, int]] = {}
    by_outcome: dict[str, dict[str, int]] = {}
    by_invariant: dict[str, dict[str, int]] = {}
    rows = []
    for scenario in scenarios:
        for seed in seeds:
            sequence = generate_sequence(
                seed,
                event_count=event_count,
                scenario=scenario,
                max_ticks=maximum_ticks,
            )
            outcome = run_sequence(sequence)
            invariant_failed = outcome.failed_invariant
            verification_failed = outcome.result is not None and not outcome.result.verification_complete
            declared = _is_declared_result(outcome, allowed_results[scenario])
            failed = outcome.failed_gate or not declared
            status = (
                "failed_invariant" if invariant_failed
                else "verification_incomplete" if verification_failed
                else "outside_declared_results" if not declared
                else "accepted"
            )
            counts[status] += 1
            result_class = _result_class(outcome)
            _increment(by_scenario, scenario, "total")
            _increment(by_scenario, scenario, status)
            _increment(by_outcome, result_class, "total")
            for kind in sorted({event.kind.value for event in sequence.events}):
                _increment(by_event_kind, kind, "sequences")
                _increment(by_event_kind, kind, status)
            for event in sequence.events:
                _increment(by_event_kind, event.kind.value, "generated")
            for application in outcome.event_applications:
                _increment(
                    by_event_kind,
                    application.kind.value,
                    application.status,
                )
            if outcome.result is not None:
                for _, invariant, _ in outcome.result.violations:
                    _increment(by_invariant, invariant, "violations")
            row = _document(sequence, outcome)
            row["declared_result"] = declared
            row["gate_status"] = status
            rows.append(row)
            if failed:
                reduced = shrink_sequence(
                    sequence,
                    lambda candidate: (
                        (candidate_outcome := run_sequence(candidate)).failed_gate
                        or not _is_declared_result(
                            candidate_outcome,
                            allowed_results[scenario],
                        )
                    ),
                )
                reduced_outcome = run_sequence(reduced)
                safe_scenario = scenario.replace("/", "-")
                (output_root / f"{safe_scenario}-seed-{seed}-repro.json").write_text(
                    json.dumps(_document(reduced, reduced_outcome),
                               ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
    summary = {
        "schema_version": SCHEMA,
        "source_commit": _commit(),
        "manifest": str(manifest),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "counts": dict(counts),
        "by_scenario": by_scenario,
        "by_event_kind": by_event_kind,
        "by_outcome": by_outcome,
        "by_invariant": by_invariant,
        "results": rows,
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    summary = run_manifest(args.manifest, args.output_root)
    print(json.dumps(summary["counts"], sort_keys=True))
    return int(bool(
        summary["counts"].get("failed_invariant", 0)
        or summary["counts"].get("verification_incomplete", 0)
        or summary["counts"].get("outside_declared_results", 0)
    ))


if __name__ == "__main__":
    raise SystemExit(main())
