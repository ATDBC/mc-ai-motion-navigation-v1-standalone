"""Rebuild the action-entry handoff regression comparison.

The v7 product archive does not store success or its behavior signature at the
top level of ``runs.jsonl``.  Reuse the project's frozen S0-R normalizer instead
of inventing a second comparison format here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.navigation_structure_baseline import NORMALIZER_VERSION, product_index


DEFAULT_BASELINE = ROOT / "evidence/motion_navigation/F2REC-recovery-v1/r4"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unique(rows: list[dict], *, name: str) -> dict[str, dict]:
    indexed = {row["id"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError(f"{name} contains duplicate case ids")
    return indexed


def _success(row: dict) -> bool:
    if "success" in row:
        return bool(row["success"])
    outcome = row.get("outcome")
    return outcome in ("success", "succeeded")


def _compare_rows(name: str, baseline_rows: list[dict], current_rows: list[dict]) -> dict:
    baseline = _unique(baseline_rows, name=f"{name} baseline")
    current = _unique(current_rows, name=f"{name} current")
    if baseline.keys() != current.keys():
        missing = sorted(baseline.keys() - current.keys())
        added = sorted(current.keys() - baseline.keys())
        raise ValueError(f"{name} denominator differs: missing={missing}, added={added}")
    comparable = {
        identifier: {key: current[identifier].get(key) for key in row if key != "id"}
        for identifier, row in baseline.items()
    }
    differences = sorted(
        identifier
        for identifier, row in baseline.items()
        if {key: value for key, value in row.items() if key != "id"} != comparable[identifier]
    )
    old_successes = {identifier for identifier, row in baseline.items() if _success(row)}
    current_successes = {identifier for identifier, row in current.items() if _success(row)}
    return {
        "name": name,
        "current_cases": len(current),
        "baseline_cases": len(baseline),
        "common_cases": len(current),
        "current_successes": len(current_successes),
        "old_successes": len(old_successes),
        "old_success_regressions": sorted(old_successes - current_successes),
        "common_signature_differences": differences,
    }


def _v7_comparison(baseline_root: Path, current_root: Path, output_root: Path) -> dict:
    baseline_rows = _read_jsonl(baseline_root / "five-groups/product-index.jsonl")
    baseline = _unique(baseline_rows, name="v7 baseline")
    frozen_summary = _read_json(baseline_root / "five-groups-summary.json")["product"]
    failed_ids = set(frozen_summary["current_failed_ids"])
    if frozen_summary["results"]["success"] != len(baseline) - len(failed_ids):
        raise ValueError("frozen v7 success denominator is inconsistent")

    generated = product_index(current_root)["cases"]
    records = _unique(_read_jsonl(current_root / "runs.jsonl"), name="v7 current runs")
    signatures = _unique(generated, name="v7 current signatures")
    if records.keys() != signatures.keys() or records.keys() != baseline.keys():
        raise ValueError("v7 run, signature, and baseline denominators differ")

    current_rows = []
    for identifier in sorted(records):
        record = records[identifier]
        current_rows.append({
            "id": identifier,
            "signature": signatures[identifier]["signature"],
            "success": bool(record["metrics"]["success"]),
            "outcome": record["metrics"]["outcome"],
            "reason": record["reason"],
        })
    current_index_path = output_root / "v7-current-index.jsonl"
    _write_jsonl(current_index_path, current_rows)

    current_successes = {row["id"] for row in current_rows if row["success"]}
    old_successes = set(baseline) - failed_ids
    differences = sorted(
        identifier
        for identifier, row in signatures.items()
        if row["signature"] != baseline[identifier]["signature"]
    )
    return {
        "name": "v7-product",
        "current_cases": len(current_rows),
        "baseline_cases": len(baseline),
        "common_cases": len(current_rows),
        "current_successes": len(current_successes),
        "old_successes": len(old_successes),
        "old_success_regressions": sorted(old_successes - current_successes),
        "common_signature_differences": differences,
        "normalizer_version": NORMALIZER_VERSION,
        "current_index_sha256": _sha256(current_index_path),
    }


def build_summary(
    *,
    baseline_root: Path,
    v7_root: Path,
    f2_runs: Path,
    v8_fixed_runs: Path,
    v8_clutter_runs: Path,
    output_root: Path,
    source_commit: str,
) -> dict:
    output_root.mkdir(parents=True, exist_ok=True)
    comparisons = [
        _v7_comparison(baseline_root, v7_root, output_root),
        _compare_rows(
            "F2-528",
            _read_jsonl(baseline_root / "f2-528-index.jsonl"),
            _read_jsonl(f2_runs),
        ),
        _compare_rows(
            "v8-fixed",
            _read_jsonl(baseline_root / "v8-fixed-index.jsonl"),
            _read_jsonl(v8_fixed_runs),
        ),
        _compare_rows(
            "v8-clutter",
            _read_jsonl(baseline_root / "v8-clutter-index.jsonl"),
            _read_jsonl(v8_clutter_runs),
        ),
    ]
    return {
        "schema_version": "mc2p.action-entry-handoff-regression.v2",
        "source_commit": source_commit,
        "comparison_basis": {
            "v7": "navigation_structure_baseline.product_index",
            "other_sets": "exact frozen-index fields",
            "baseline_root": str(baseline_root.relative_to(ROOT)),
        },
        "inputs": {
            "v7_runs_sha256": _sha256(v7_root / "runs.jsonl"),
            "v7_index_sha256": _sha256(v7_root / "index.json"),
            "f2_runs_sha256": _sha256(f2_runs),
            "v8_fixed_runs_sha256": _sha256(v8_fixed_runs),
            "v8_clutter_runs_sha256": _sha256(v8_clutter_runs),
        },
        "sets": comparisons,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-root", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--v7-root", type=Path, required=True)
    parser.add_argument("--f2-runs", type=Path, required=True)
    parser.add_argument("--v8-fixed-runs", type=Path, required=True)
    parser.add_argument("--v8-clutter-runs", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    for name in ("baseline_root", "v7_root", "f2_runs", "v8_fixed_runs",
                 "v8_clutter_runs", "output_root"):
        setattr(args, name, getattr(args, name).resolve())
    summary = build_summary(**vars(args))
    path = args.output_root / "regression-summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
