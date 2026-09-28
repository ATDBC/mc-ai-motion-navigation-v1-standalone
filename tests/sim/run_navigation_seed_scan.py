"""Run the frozen S5 seed scan through the formal navigation path."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import time

from tests.sim.backend import Perturbations
from tests.sim.runner import late_ticks, run
from tests.sim.scenarios import SCENARIOS


def run_scan(manifest_path: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite seed scan: {output}")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != "mc2p.navigation-seed-scan.v1":
        raise ValueError("unsupported navigation seed scan")
    first, last = manifest.get("seed_start"), manifest.get("seed_end")
    if type(first) is not int or type(last) is not int or last < first:
        raise ValueError("invalid frozen seed range")
    scenarios = {scenario.name: scenario for scenario in SCENARIOS}
    output.mkdir(parents=True)
    findings = []
    counts = {
        "runs": 0,
        "within_declared_results": 0,
        "outside_declared_results": 0,
        "task_success": 0,
        "bounded_safe_failure": 0,
        "unexpected_result": 0,
    }
    family_summaries = []
    started = time.perf_counter()
    for family in manifest.get("families", []):
        scenario = scenarios[family["scenario"]]
        allowed = {tuple(item) for item in family["allowed_results"]}
        probability = float(family["late_probability"])
        terminal_distribution: dict[str, int] = {}
        family_counts = {
            "runs": 0,
            "task_success": 0,
            "first_attempt_success": 0,
            "recovered_success": 0,
            "bounded_safe_failure": 0,
            "unexpected_result": 0,
            "total_recovery_failures": 0,
            "maximum_recovery_failures": 0,
        }
        for seed in range(first, last + 1):
            frozen = replace(
                scenario,
                perturbations=replace(
                    scenario.perturbations,
                    late_ticks=late_ticks(probability, seed),
                ),
            )
            result = run(frozen)
            within_declared = (
                (result.outcome, result.reason) in allowed
                and not result.violations
                and result.damage <= scenario.damage_points + 1.0e-9
            )
            outcome_class = result.outcome_class
            recovery_failures = result.recovery_failures
            terminal_key = f"{result.outcome}/{result.reason}"
            terminal_distribution[terminal_key] = (
                terminal_distribution.get(terminal_key, 0) + 1
            )
            counts["runs"] += 1
            counts[
                "within_declared_results" if within_declared
                else "outside_declared_results"
            ] += 1
            counts[outcome_class] += 1
            family_counts["runs"] += 1
            family_counts[outcome_class] += 1
            family_counts["total_recovery_failures"] += recovery_failures
            family_counts["maximum_recovery_failures"] = max(
                family_counts["maximum_recovery_failures"], recovery_failures,
            )
            if outcome_class == "task_success":
                family_counts[
                    "first_attempt_success" if recovery_failures == 0
                    else "recovered_success"
                ] += 1
            if not within_declared or outcome_class == "unexpected_result":
                findings.append({"scenario": scenario.name, "seed": seed,
                                 "result": asdict(result)})
        runs = family_counts["runs"]
        family_summaries.append({
            "scenario": scenario.name,
            "late_probability": probability,
            "counts": family_counts,
            "terminal_distribution": terminal_distribution,
            "task_success_rate": (
                family_counts["task_success"] / runs if runs else 0.0
            ),
            "first_attempt_success_rate": (
                family_counts["first_attempt_success"] / runs if runs else 0.0
            ),
            "success_with_legal_recovery_rate": (
                (family_counts["first_attempt_success"]
                 + family_counts["recovered_success"]) / runs
                if runs else 0.0
            ),
            "mean_recovery_failures": (
                family_counts["total_recovery_failures"] / runs
                if runs else 0.0
            ),
        })
    summary = {
        "schema_version": "mc2p.navigation-seed-scan-result.v2",
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "counts": counts,
        "families": family_summaries,
        "elapsed_seconds": time.perf_counter() - started,
        "findings": findings,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = run_scan(args.manifest, args.output)
    except (OSError, ValueError) as error:
        print(f"seed scan setup failed: {error}")
        return 2
    print(json.dumps({
        "schema_version": summary["schema_version"],
        "counts": summary["counts"],
        "elapsed_seconds": summary["elapsed_seconds"],
        "output": str(args.output),
    }, ensure_ascii=False))
    return 1 if (summary["counts"]["outside_declared_results"]
                 or summary["counts"]["unexpected_result"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
