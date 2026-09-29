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
            or len(set(scenarios)) != len(scenarios)):
        raise ValueError("invalid event sequence manifest")
    output_root.mkdir(parents=True)
    counts = Counter()
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
            failed = outcome.failed_invariant
            counts["failed_invariant" if failed else "accepted"] += 1
            row = _document(sequence, outcome)
            rows.append(row)
            if failed:
                reduced = shrink_sequence(
                    sequence,
                    lambda candidate: run_sequence(candidate).failed_invariant,
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
        "results": rows,
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    summary = run_manifest(args.manifest, args.output_root)
    print(json.dumps(summary["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
