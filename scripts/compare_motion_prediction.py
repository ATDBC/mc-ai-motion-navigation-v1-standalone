"""Compare B09-R one-tick predictions with sealed Fabric physics evidence."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.evidence.physics_validation import (
    compare_open_loop_rows, compare_tick_rows, declared_fixture_validation_world,
    ordinary_flat_validation_world,
)
from mc2p.runtime.segmented_trace import iter_segmented_jsonl


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path,
                        help="sealed physics-tick-events directory")
    parser.add_argument("--fixture", choices=("ordinary-flat", "low-ceiling-flat", "declared-commands"),
                        default="ordinary-flat")
    parser.add_argument("--fixture-commands", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-pre-pose", action="append",
                        choices=("standing", "crouching", "swimming"),
                        help="compare only rows whose pre-state uses this pose")
    parser.add_argument("--summary-only", action="store_true",
                        help="print compact metrics while retaining the full output file")
    args = parser.parse_args()
    rows = tuple(iter_segmented_jsonl(args.evidence))
    if args.include_pre_pose:
        allowed_poses = frozenset(args.include_pre_pose)
        rows = tuple(row for row in rows
                     if row.get("pre_state", {}).get("pose") in allowed_poses)
        if not rows:
            parser.error("pose filter selected no evidence rows")
    if args.fixture in {"ordinary-flat", "low-ceiling-flat"}:
        world = ordinary_flat_validation_world(
            rows, JAVA_1_21_RULESET, low_ceiling=args.fixture == "low-ceiling-flat")
    else:
        if args.fixture_commands is None:
            parser.error("declared-commands requires --fixture-commands")
        world = declared_fixture_validation_world(
            args.fixture_commands, JAVA_1_21_RULESET)
    report = compare_tick_rows(rows, world, JAVA_1_21_RULESET)
    open_loop = compare_open_loop_rows(rows, world, JAVA_1_21_RULESET)
    payload = {
        "schema_version": "mc2p.physics-validation-report.v1",
        "evidence": str(args.evidence.resolve()),
        "fixture": args.fixture,
        "row_selection": {
            "pre_poses": sorted(set(args.include_pre_pose or ())),
        },
        "ruleset_id": JAVA_1_21_RULESET.ruleset_id,
        "single_tick": asdict(report),
        "open_loop": asdict(open_loop),
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    if args.summary_only:
        print(json.dumps({
            "single_tick": {
                "total_rows": report.total_rows,
                "complete_rows": report.complete_rows,
                "incomplete_rows": report.incomplete_rows,
                "event_mismatch_count": len(report.event_mismatch_rows),
                "position_error_max": report.position_error_max,
                "velocity_error_max": report.velocity_error_max,
            },
            "open_loop": {
                "sequence_count": open_loop.sequence_count,
                "complete_sequences": open_loop.complete_sequences,
                "ticks_compared": open_loop.ticks_compared,
                "issue_count": len(open_loop.issues),
                "divergence_count": len(open_loop.first_divergence),
                "position_error_max": open_loop.position_error_max,
                "velocity_error_max": open_loop.velocity_error_max,
            },
        }, ensure_ascii=False, indent=2))
    else:
        print(encoded, end="")
    return 0 if (report.incomplete_rows == 0 and not report.event_mismatch_rows
                 and not open_loop.issues and not open_loop.first_divergence) else 2


if __name__ == "__main__":
    raise SystemExit(main())
