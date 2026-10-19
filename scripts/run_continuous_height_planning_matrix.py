"""Run the frozen continuous-height planner/reference comparison."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.sim.continuous_height_planning_matrix import (  # noqa: E402
    load_planning_manifest, run_planning_case,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.output.exists():
        raise FileExistsError(f"planning matrix output exists: {arguments.output}")
    arguments.output.mkdir(parents=True)
    document = load_planning_manifest()
    rows = []
    path = arguments.output / "results.jsonl"
    for seed in range(document["seed_start"], document["seed_end"] + 1):
        result = run_planning_case(seed)
        row = {**asdict(result), "matches_reference": result.matches_reference}
        rows.append(row)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "schema_version": "mc2p.continuous-height-planning-results.v1",
        "case_count": len(rows),
        "matches": sum(row["matches_reference"] for row in rows),
        "reachable": sum(row["reference_reachable"] for row in rows),
        "maximum_expanded_nodes": max(row["expanded_nodes"] for row in rows),
        "maximum_elapsed_ms": max(row["elapsed_ms"] for row in rows),
    }
    (arguments.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if summary["matches"] == summary["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
