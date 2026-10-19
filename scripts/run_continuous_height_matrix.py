"""Run the frozen continuous-height component matrix through the formal path."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.sim.continuous_height_matrix import (
    MANIFEST,
    MatrixCase,
    height_action_matrix_cases,
    load_manifest,
    small_height_matrix_cases,
)
from tests.sim.runner import Result, run


SCHEMA = "mc2p.continuous-height-component-results.v1"


def _json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"


def _route_actions(result: Result) -> tuple[str, ...]:
    actions: list[str] = []
    for row in result.trace:
        kind = row.get("action_kind")
        if kind is not None and (not actions or actions[-1] != kind):
            actions.append(str(kind))
    return tuple(actions)


def _minimum_moving_speed(result: Result) -> float | None:
    speeds = []
    for row in result.trace:
        vx, _, vz = row["velocity"]
        speed = math.hypot(vx, vz) * 20.0
        if speed > 0.1:
            speeds.append(speed)
    return None if not speeds else min(speeds)


def _result_row(case: MatrixCase, result: Result, elapsed_ms: float) -> dict:
    late_applications = sum(
        row["sample_state"] not in {"neutral", "leased"}
        for row in result.trace
    )
    return {
        "case_id": (
            f"{case.family}/{case.condition}/{case.direction}/"
            f"{case.speed_band}/{case.seed}"
        ),
        "family": case.family,
        "condition": case.condition,
        "direction": case.direction,
        "speed_band": case.speed_band,
        "entry_speed_blocks_per_second": case.speed_blocks_per_second,
        "seed": case.seed,
        "outcome": result.outcome,
        "outcome_class": result.outcome_class,
        "reason": result.reason,
        "ticks": result.ticks,
        "elapsed_ms": elapsed_ms,
        "final_position": result.final_position,
        "damage_points": result.damage,
        "recovery_failures": result.recovery_failures,
        "route_actions": _route_actions(result),
        "minimum_moving_speed_blocks_per_second": _minimum_moving_speed(result),
        "late_or_missing_application_frames": late_applications,
        "reference_ticks": None,
        "actual_to_reference_ratio": None,
        "violations": result.violations,
        "coverage_gaps": result.coverage_gaps,
    }


def _write_summary(root: Path, manifest_hash: str, rows: list[dict],
                   *, complete: bool) -> None:
    by_outcome: dict[str, int] = {}
    by_family: dict[str, dict[str, int]] = {}
    for row in rows:
        by_outcome[row["outcome_class"]] = (
            by_outcome.get(row["outcome_class"], 0) + 1
        )
        family = by_family.setdefault(row["family"], {})
        family[row["outcome_class"]] = (
            family.get(row["outcome_class"], 0) + 1
        )
    document = {
        "schema_version": SCHEMA,
        "manifest_sha256": manifest_hash,
        "complete": complete,
        "case_count": len(rows),
        "outcomes": by_outcome,
        "families": by_family,
    }
    temporary = root / "summary.json.tmp"
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(root / "summary.json")


def run_cases(cases: tuple[MatrixCase, ...], output: Path) -> None:
    """Run cases once, refusing to overwrite any prior result directory."""
    if output.exists():
        raise FileExistsError(f"matrix output already exists: {output}")
    output.mkdir(parents=True)
    failures = output / "failures"
    failures.mkdir()
    manifest_bytes = MANIFEST.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    shutil.copy2(MANIFEST, output / "manifest.json")
    metadata = {
        "schema_version": SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": manifest_hash,
        "case_count": len(cases),
    }
    (output / "run.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    rows: list[dict] = []
    result_path = output / "results.jsonl"
    for index, case in enumerate(cases):
        trace_path = output / f"case-{index:05d}.trace.tmp"
        with trace_path.open("w", encoding="utf-8") as trace_file:
            started = time.perf_counter()
            try:
                result = run(
                    case.scenario,
                    trace_sink=lambda row, stream=trace_file:
                        stream.write(_json_line(row)),
                )
                elapsed_ms = (time.perf_counter() - started) * 1_000.0
                row = _result_row(case, result, elapsed_ms)
            except Exception as error:
                elapsed_ms = (time.perf_counter() - started) * 1_000.0
                row = {
                    "case_id": (
                        f"{case.family}/{case.condition}/{case.direction}/"
                        f"{case.speed_band}/{case.seed}"
                    ),
                    "family": case.family,
                    "condition": case.condition,
                    "direction": case.direction,
                    "speed_band": case.speed_band,
                    "entry_speed_blocks_per_second": (
                        case.speed_blocks_per_second
                    ),
                    "seed": case.seed,
                    "outcome": "exception",
                    "outcome_class": "unexpected_result",
                    "reason": f"{type(error).__name__}: {error}",
                    "elapsed_ms": elapsed_ms,
                }
        rows.append(row)
        with result_path.open("a", encoding="utf-8") as stream:
            stream.write(_json_line(row))
        if row["outcome_class"] == "task_success":
            trace_path.unlink()
        else:
            trace_path.replace(failures / f"case-{index:05d}.jsonl")
        _write_summary(output, manifest_hash, rows, complete=False)
    _write_summary(output, manifest_hash, rows, complete=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--condition", choices=("normal", "late", "both"), default="both",
    )
    parser.add_argument(
        "--scope", choices=("small", "actions", "all"), default="all",
    )
    arguments = parser.parse_args()
    load_manifest()
    conditions = (
        ("normal", "late") if arguments.condition == "both"
        else (arguments.condition,)
    )
    factories = {
        "small": (small_height_matrix_cases,),
        "actions": (height_action_matrix_cases,),
        "all": (small_height_matrix_cases, height_action_matrix_cases),
    }[arguments.scope]
    cases = tuple(
        case for condition in conditions for factory in factories
        for case in factory(condition)
    )
    run_cases(cases, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
