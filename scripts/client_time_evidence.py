"""Attribute raw client world-time changes without rewriting them or accepting tick parity."""
from __future__ import annotations

import json
from pathlib import Path

from mc2p.runtime.segmented_trace import MAX_RECORD_BYTES, _decode, _lines, _positive, _safe_path
from scripts.control_probe_core import write_json_atomic


def _read_jsonl(path: Path) -> list[dict]:
    with path.open("rb") as stream:
        raw = stream.read(128 * 1024 * 1024 + 1)
    if len(raw) > 128 * 1024 * 1024:
        raise ValueError("time evidence input exceeds 128 MiB")
    return [json.loads(line) for line in raw.decode("utf-8").splitlines()]


def _iter_bounded_jsonl(path: Path, max_record_bytes: int = MAX_RECORD_BYTES):
    maximum = min(_positive(max_record_bytes, "max_record_bytes"), MAX_RECORD_BYTES)
    with _safe_path(path).open("rb") as stream:
        for line in _lines(stream, maximum):
            yield _decode(line)


def export_time_evidence(source: Path, offset: int, run: Path, *, observations: list[dict] | None = None) -> dict:
    try:
        if offset < 0 or source.stat().st_size < offset or source.stat().st_size - offset > 128 * 1024 * 1024:
            raise ValueError("time sidecar offset/size invalid")
        with source.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(128 * 1024 * 1024 + 1)
        if len(raw) > 128 * 1024 * 1024:
            raise ValueError("time sidecar grew beyond the read bound")
        (run / "time-events.jsonl").write_bytes(raw)
        events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
        if observations is None:
            observations = _read_jsonl(run / "observations.jsonl")
        result = evaluate_time_events(events, observations)
    except (OSError, UnicodeError, ValueError) as error:
        result = {"schema_version": "mc2p.client-time-evidence.v1", "status": "failed", "errors": [str(error)]}
    write_json_atomic(run / "time-report.json", result)
    return result


def export_runtime_time_evidence(directory: Path) -> dict:
    """Export after client cleanup; attribution cannot replace the owning probe's result."""
    try:
        trace_path = directory / "trace.jsonl"
        if trace_path.is_file():
            records = _iter_bounded_jsonl(trace_path)
        else:
            from mc2p.runtime.segmented_trace import iter_segmented_jsonl
            records = iter_segmented_jsonl(directory / "runtime-trace" / "trace")
        diagnostics = iter(_iter_bounded_jsonl(directory / "diagnostics.jsonl"))

        def observations():
            expected_sequence = 0
            for record in records:
                if record["record_type"] not in {"reset", "step", "close_release"}:
                    continue
                observation = (
                    record["payload"]["result"]["observation"]
                    if record["record_type"] == "reset" else
                    record["payload"]["backend_result"]["observation"]
                )
                try:
                    row = next(diagnostics)
                except StopIteration:
                    raise ValueError(
                        "deployment timing samples do not cover one exact Runtime episode"
                    ) from None
                client_tick = row["diagnostics"]["client_tick"]
                if (observation["source_backend"] != "fabric"
                        or observation["sequence_id"] != expected_sequence
                        or row["observation_sequence_id"] != expected_sequence
                        or row["episode_id"] != observation["episode_id"]
                        or type(client_tick) is not int or client_tick < 0):
                    raise ValueError(
                        "deployment timing samples do not cover one exact Runtime episode"
                    )
                expected_sequence += 1
                yield {
                    **observation,
                    "_diagnostic_client_tick": client_tick,
                }
            try:
                next(diagnostics)
            except StopIteration:
                return
            raise ValueError(
                "deployment timing samples do not cover one exact Runtime episode"
            )

        result = export_time_evidence(
            directory / "mc2p-client-time.jsonl",
            0,
            directory,
            observations=observations(),
        )
    except (OSError, UnicodeError, KeyError, IndexError, TypeError, ValueError) as error:
        result = {"schema_version": "mc2p.client-time-evidence.v1", "status": "failed", "errors": [str(error)]}
    write_json_atomic(directory / "time-report.json", result)
    return result


def evaluate_lockstep_world_ticks(events: list[dict], observations: list[dict]) -> dict:
    """Timestamp equality alone must never hide skipped/extra native tickTime calls."""
    attribution = evaluate_time_events(events, observations)
    intervals = attribution["intervals"]
    counts = [r["world_tick_calls"] for r in intervals]
    steps = sum(o.get("sequence_id") != 0 for o in observations)
    passed = (attribution["status"] == "passed" and len(counts) == steps > 0
              and all(count == 1 for count in counts))
    return {"name": "one_actual_client_world_tick_per_action", "passed": passed,
            "world_tick_calls": counts, "errors": attribution["errors"],
            "meaning": "actual tickTime calls only; raw normal time corrections are preserved; not full-game equivalence"}


def export_lockstep_world_ticks(source: Path, offset: int, run: Path) -> dict:
    try:
        observations = _read_jsonl(run / "observations.jsonl")
        attribution = export_time_evidence(source, offset, run, observations=observations)
        if attribution["status"] != "passed":
            raise ValueError("lockstep requires complete --time-diagnostics evidence")
        result = evaluate_lockstep_world_ticks(_read_jsonl(run / "time-events.jsonl"), observations)
    except (OSError, UnicodeError, ValueError) as error:
        result = {"name": "one_actual_client_world_tick_per_action", "passed": False, "errors": [str(error)]}
    write_json_atomic(run / "world-tick-report.json", result)
    return result


def evaluate_time_events(events: list[dict], observations: list[dict]) -> dict:
    """Compatibility collector over the same incremental attribution state machine."""
    from scripts.streaming_time_evidence import evaluate_time_stream
    intervals = []
    result = evaluate_time_stream(events, observations, intervals.append)
    result["schema_version"] = "mc2p.client-time-evidence.v1"
    result["intervals"] = intervals
    result.pop("interval_count")
    result.pop("error_count")
    return result
