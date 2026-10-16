#!/usr/bin/env python3
"""Read existing Fabric traces without changing or replaying them.

The report deliberately separates unavailable fields from measured zeroes.  It is
an audit aid for D107, not a causal classifier: the pre-D103 raw run is absent,
so later runs cannot prove whether D103 caused a particular B10 timing change.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.b10_gap_solver_runtime import _receiver_diagnostic_summary


SCHEMA_VERSION = "mc2p.d108-b10-trace-audit.v2"

_CONTROL_TIMING_FIELDS = (
    "control_wall_ns",
    "backend_elapsed_ns",
    "backend_blocking_io_ns",
    "runtime_outside_backend_ns",
    "proposal_elapsed_ns",
)


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def _timing_summary(values_ms: Sequence[float]) -> dict:
    return {
        "sample_count": len(values_ms),
        "p50": _percentile(values_ms, 0.50),
        "p95": _percentile(values_ms, 0.95),
        "p99": _percentile(values_ms, 0.99),
        "maximum": max(values_ms) if values_ms else None,
        "over_50ms_count": sum(value > 50.0 for value in values_ms),
    }


def _count_summary(values: Sequence[int]) -> dict:
    return {
        "sample_count": len(values),
        "total": sum(values),
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
    }


def _tick_summary(ticks: Iterable[int]) -> dict:
    ordered = sorted(ticks)
    unique = sorted(set(ordered))
    gaps = []
    for before, after in zip(unique, unique[1:]):
        if after > before + 1:
            gaps.append({
                "after": before,
                "before": after,
                "missing_count": after - before - 1,
            })
    return {
        "application_count": len(ordered),
        "unique_tick_count": len(unique),
        "duplicate_count": len(ordered) - len(unique),
        "first": unique[0] if unique else None,
        "last": unique[-1] if unique else None,
        "gaps": gaps,
        "contiguous": bool(unique) and not gaps,
    }


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: JSON value must be an object")
            yield value


def _trace_summary(path: Path) -> dict:
    step_frames = 0
    timing_frames_missing = 0
    receipt_frames_missing = 0
    application_frames_missing = 0
    invalid_timing_frames = 0
    control_ms: list[float] = []
    applications_per_frame: list[int] = []
    movement_ticks: list[int] = []
    state_counts: Counter[str] = Counter()
    lease_frames = 0
    lease_applications = 0

    for row in _iter_jsonl(path):
        if row.get("record_type") != "step":
            continue
        step_frames += 1
        payload = row.get("payload")
        if not isinstance(payload, dict):
            timing_frames_missing += 1
            receipt_frames_missing += 1
            application_frames_missing += 1
            continue

        received = payload.get("decision_observation_received_ns")
        dispatched = payload.get("dispatched_monotonic_ns")
        if type(received) is int and type(dispatched) is int:
            if dispatched >= received:
                control_ms.append((dispatched - received) / 1_000_000.0)
            else:
                invalid_timing_frames += 1
        else:
            timing_frames_missing += 1

        backend_result = payload.get("backend_result")
        receipt = backend_result.get("receipt") if isinstance(backend_result, dict) else None
        if not isinstance(receipt, dict):
            receipt_frames_missing += 1
            application_frames_missing += 1
            continue
        applications = receipt.get("input_applications")
        if not isinstance(applications, list):
            application_frames_missing += 1
            continue
        applications_per_frame.append(len(applications))
        frame_has_lease_exhausted = False
        for application in applications:
            if not isinstance(application, dict):
                continue
            state = application.get("state")
            if isinstance(state, str):
                state_counts[state] += 1
                if state == "lease_exhausted":
                    lease_applications += 1
                    frame_has_lease_exhausted = True
            movement_tick = application.get("movement_tick_id")
            if type(movement_tick) is int:
                movement_ticks.append(movement_tick)
        if frame_has_lease_exhausted:
            lease_frames += 1

    result = {
        "status": "available",
        "source": path.as_posix(),
        "step_frames": step_frames,
        "control_prepare_ms": _timing_summary(control_ms),
        "receipt_applications_per_frame": _count_summary(applications_per_frame),
        "application_state_counts": dict(sorted(state_counts.items())),
        "lease_exhausted": {
            "application_count": lease_applications,
            "frame_count": lease_frames,
        },
        "movement_ticks": _tick_summary(movement_ticks),
        "missing_fields": {
            "timing_frames": timing_frames_missing,
            "receipt_frames": receipt_frames_missing,
            "input_application_frames": application_frames_missing,
            "invalid_timing_frames": invalid_timing_frames,
        },
    }
    result["control_prepare_status"] = (
        "available" if control_ms else "unavailable"
    )
    if not control_ms:
        result["control_prepare_unavailable_reason"] = (
            "trace steps do not contain both decision_observation_received_ns "
            "and dispatched_monotonic_ns"
        )
    result["receiver_delivery_timing"] = {
        "status": "unavailable",
        "reason": (
            "legacy trace records do not identify receiver publication and "
            "first control-frame consumption movement ticks"
        ),
    }
    return result


def _control_frame_summary(path: Path) -> dict:
    timings: dict[str, list[float]] = {
        field: [] for field in _CONTROL_TIMING_FIELDS
    }
    missing: Counter[str] = Counter()
    invalid: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    row_count = 0
    for row in _iter_jsonl(path):
        row_count += 1
        for field in _CONTROL_TIMING_FIELDS:
            value = row.get(field)
            if type(value) is int and value >= 0:
                timings[field].append(value / 1_000_000.0)
            elif value is None:
                missing[field] += 1
            else:
                invalid[field] += 1
        reason = row.get("reason_code")
        if isinstance(reason, str):
            reason_counts[reason] += 1
    result = {
        "status": "available",
        "source": path.as_posix(),
        "row_count": row_count,
        "timing_status": "available" if row_count else "unavailable",
        "missing_fields": {
            field: missing[field] for field in _CONTROL_TIMING_FIELDS
        },
        "invalid_fields": {
            field: invalid[field] for field in _CONTROL_TIMING_FIELDS
        },
        "reason_counts": dict(sorted(reason_counts.items())),
    }
    for field in _CONTROL_TIMING_FIELDS:
        result[field.removesuffix("_ns") + "_ms"] = _timing_summary(
            timings[field]
        )
    if not row_count:
        result["timing_unavailable_reason"] = "control-frame file contains no rows"
    return result


def _ftick_summary(client: Path) -> dict:
    summary_candidates = (
        client / "ftick-summary.json",
        client / "ftick-events" / "summary.json",
    )
    summary_path = next((path for path in summary_candidates if path.is_file()), None)
    event_paths = sorted((client / "ftick-events").glob("segment-*.jsonl"))
    if summary_path is None and not event_paths:
        return {
            "status": "unavailable",
            "reason": "ftick summary and event segments are absent",
        }

    result: dict = {"status": "available"}
    if summary_path is not None:
        result["summary_source"] = summary_path.as_posix()
        result["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
    else:
        result["summary"] = "unavailable"
        result["summary_reason"] = "ftick summary file is absent; event counts are reported"

    kind_counts: Counter[str] = Counter()
    sampled_ticks: list[int] = []
    applied_ticks: list[int] = []
    for event_path in event_paths:
        for row in _iter_jsonl(event_path):
            kind = row.get("kind")
            if isinstance(kind, str):
                kind_counts[kind] += 1
            movement_tick = row.get("movement_tick_id")
            if type(movement_tick) is int:
                if kind == "INPUT_SAMPLED":
                    sampled_ticks.append(movement_tick)
                elif kind == "MOVEMENT_APPLIED":
                    applied_ticks.append(movement_tick)
    result["event_sources"] = [path.as_posix() for path in event_paths]
    result["kind_counts"] = dict(sorted(kind_counts.items()))
    result["input_sampled_ticks"] = _tick_summary(sampled_ticks)
    result["movement_applied_ticks"] = _tick_summary(applied_ticks)
    return result


def _receiver_summary(client: Path) -> dict:
    path = client / "b10-receiver-diagnostics.jsonl"
    if not path.is_file():
        return {
            "status": "unavailable",
            "reason": "client-0/b10-receiver-diagnostics.jsonl is absent",
        }
    result = _receiver_diagnostic_summary(list(_iter_jsonl(path)))
    result["status"] = "available"
    result["source"] = path.as_posix()
    return result


def analyze_run(label: str, run: Path) -> dict:
    run = Path(run)
    trace = run / "client-0" / "trace.jsonl"
    if not trace.is_file():
        return {
            "label": label,
            "path": run.as_posix(),
            "status": "unavailable",
            "reason": "client-0/trace.jsonl is absent",
        }
    client = run / "client-0"
    control_frames = client / "b10-coordinator-control-frames.jsonl"
    return {
        "label": label,
        "path": run.as_posix(),
        "status": "available",
        "trace": _trace_summary(trace),
        "control_frames": (
            _control_frame_summary(control_frames)
            if control_frames.is_file()
            else {
                "status": "unavailable",
                "reason": "client-0/b10-coordinator-control-frames.jsonl is absent",
            }
        ),
        "ftick": _ftick_summary(client),
        "receiver_diagnostics": _receiver_summary(client),
    }


def build_report(
        runs: Sequence[tuple[str, Path]],
        unavailable: Sequence[tuple[str, str]] = (),
) -> dict:
    measured = [analyze_run(label, path) for label, path in runs]
    missing = [
        {"label": label, "status": "unavailable", "reason": reason}
        for label, reason in unavailable
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "method": {
            "control_prepare": (
                "trace step dispatched_monotonic_ns minus "
                "decision_observation_received_ns"
            ),
            "percentiles": "nearest-rank",
            "over_50ms": "strictly greater than 50.0 ms",
            "movement_tick_continuity": "sorted unique receipt application tick ids",
            "control_frame_timings": (
                "each recorded duration is summarized independently; missing "
                "fields are unavailable and durations are never added together"
            ),
        },
        "runs": measured + missing,
        "causal_conclusion": "unavailable",
        "causal_boundary": (
            "The pre-D103 2026-09-25 raw run directory is unavailable. "
            "D105 and D106 traces can describe their own timing but cannot prove "
            "whether D103 caused a B10 timing change."
        ),
    }


def _labelled(value: str, option: str) -> tuple[str, str]:
    label, separator, remainder = value.partition("=")
    if not separator or not label or not remainder:
        raise argparse.ArgumentTypeError(f"{option} requires LABEL=VALUE")
    return label, remainder


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument(
        "--unavailable", action="append", default=[], metavar="LABEL=REASON",
        help="record a known missing source without inventing measurements",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.run and not args.unavailable:
        parser.error("at least one --run or --unavailable is required")
    try:
        runs = [
            (label, Path(path))
            for label, path in (_labelled(value, "--run") for value in args.run)
        ]
        unavailable = [
            _labelled(value, "--unavailable") for value in args.unavailable
        ]
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    report = build_report(tuple(runs), tuple(unavailable))
    if args.output is None:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _write_atomic(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
