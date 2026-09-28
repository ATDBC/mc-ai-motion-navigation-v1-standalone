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
    counts = {"runs": 0, "accepted": 0, "unexpected": 0}
    started = time.perf_counter()
    for family in manifest.get("families", []):
        scenario = scenarios[family["scenario"]]
        allowed = {tuple(item) for item in family["allowed_results"]}
        probability = float(family["late_probability"])
        for seed in range(first, last + 1):
            frozen = replace(
                scenario,
                perturbations=replace(
                    scenario.perturbations,
                    late_ticks=late_ticks(probability, seed),
                ),
            )
            result = run(frozen)
            accepted = (
                (result.outcome, result.reason) in allowed
                and not result.violations
                and result.damage <= scenario.damage_points + 1.0e-9
            )
            counts["runs"] += 1
            counts["accepted" if accepted else "unexpected"] += 1
            if not accepted:
                findings.append({"scenario": scenario.name, "seed": seed,
                                 "result": asdict(result)})
    summary = {
        "schema_version": "mc2p.navigation-seed-scan-result.v1",
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "counts": counts,
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
    return 1 if summary["counts"]["unexpected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
