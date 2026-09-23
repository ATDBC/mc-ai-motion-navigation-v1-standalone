"""Normalize sealed legacy runs without changing their raw evidence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from mc2p.runtime.segmented_trace import iter_segmented_jsonl


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _json_line(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _duration(end: Any, start: Any, field: str) -> int:
    if not isinstance(end, int) or not isinstance(start, int) or end < start:
        raise ValueError(f"invalid {field} interval")
    return end - start


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_legacy_evidence(
    evidence_dir: Path,
    output_dir: Path,
    *,
    version_id: str,
    scene_id: str,
) -> dict[str, Any]:
    """Write a stable four-file view of one sealed run.

    Controller and client durations remain separate clock-domain measurements.
    """
    source = Path(evidence_dir).resolve()
    destination = Path(output_dir).resolve()
    if destination == source or source in destination.parents:
        raise ValueError("normalized output must stay outside raw evidence")
    if destination.exists():
        raise FileExistsError(destination)
    if not version_id or not scene_id:
        raise ValueError("version_id and scene_id are required")
    trajectory = source / "trajectory"
    run_manifest_path = source / "run-manifest.json"
    terminal_path = source / "terminal.json"
    if not trajectory.is_dir() or not run_manifest_path.is_file() or not terminal_path.is_file():
        raise ValueError("incomplete legacy evidence directory")

    run_manifest = _object(run_manifest_path)
    terminal = _object(terminal_path)
    records = iter_segmented_jsonl(trajectory)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    counts = {"inputs": 0, "body": 0, "timings": 0}
    try:
        with (
            (temporary / "inputs.jsonl").open("wb") as inputs,
            (temporary / "body.jsonl").open("wb") as body,
            (temporary / "timings.jsonl").open("wb") as timings,
        ):
            for ordinal, record in enumerate(records):
                if not isinstance(record, dict):
                    raise ValueError(f"trajectory record {ordinal} is not an object")
                kind = record.get("record_type")
                if kind == "execution":
                    normalized = {
                        "schema_version": "mc2p.motion-navigation-input.v1",
                        "ordinal": ordinal,
                        "controller_ns": record.get("controller_ns"),
                        "request_sequence_id": record.get("request_sequence_id"),
                        "requested_action": record.get("requested_action"),
                        "selected_action": record.get("selected_action"),
                        "confirmed_execution": record.get("confirmed_execution"),
                    }
                    inputs.write(_json_line(normalized))
                    counts["inputs"] += 1
                elif kind == "sample":
                    sample = record.get("sample")
                    if not isinstance(sample, dict):
                        raise ValueError(f"sample record {ordinal} has no sample object")
                    client = sample.get("client_sample")
                    if not isinstance(client, dict):
                        raise ValueError(f"sample record {ordinal} has no client clock interval")
                    controller_clock = sample.get("controller_clock_id")
                    client_clock = client.get("clock_id")
                    if not isinstance(controller_clock, str) or not isinstance(client_clock, str):
                        raise ValueError(f"sample record {ordinal} has no clock identity")
                    normalized_body = {
                        "schema_version": "mc2p.motion-navigation-body.v1",
                        "ordinal": ordinal,
                        "episode_id": sample.get("episode_id"),
                        "sequence_id": sample.get("sequence_id"),
                        "request_sequence_id": sample.get("request_sequence_id"),
                        "position": sample.get("position"),
                        "horizontal_speed_blocks_per_tick": sample.get(
                            "horizontal_speed_blocks_per_tick"
                        ),
                        "on_ground": sample.get("on_ground"),
                        "controller_clock_id": controller_clock,
                        "received_at_ns": sample.get("received_at_ns"),
                        "client_sample": dict(client),
                    }
                    body.write(_json_line(normalized_body))
                    counts["body"] += 1
                    normalized_timing = {
                        "schema_version": "mc2p.motion-navigation-timing.v1",
                        "ordinal": ordinal,
                        "request_sequence_id": sample.get("request_sequence_id"),
                        "controller_clock_id": controller_clock,
                        "controller_observation_delivery_ns": _duration(
                            sample.get("received_at_ns"),
                            sample.get("request_started_at_ns"),
                            "controller observation delivery",
                        ),
                        "client_clock_id": client_clock,
                        "client_sampling_ns": _duration(
                            client.get("completed_at_ns"),
                            client.get("started_at_ns"),
                            "client sampling",
                        ),
                    }
                    timings.write(_json_line(normalized_timing))
                    counts["timings"] += 1
                else:
                    raise ValueError(f"unsupported trajectory record type at {ordinal}: {kind}")

        case_plan = run_manifest.get("case_plan")
        source_case_id = case_plan.get("case") if isinstance(case_plan, dict) else None
        summary = {
            "schema_version": "mc2p.motion-navigation-reference-run.v1",
            "version_id": version_id,
            "scene_id": scene_id,
            "source_case_id": source_case_id,
            "source_archive": run_manifest.get("source_archive"),
            "terminal": terminal,
            "counts": counts,
            "raw_evidence": {
                "path": str(source),
                "trajectory_manifest_sha256": _hash(trajectory / "manifest.jsonl"),
                "trajectory_complete_sha256": _hash(trajectory / "complete.json"),
                "run_manifest_sha256": _hash(run_manifest_path),
                "terminal_sha256": _hash(terminal_path),
            },
            "clock_rule": "durations are only computed within one named clock domain",
        }
        (temporary / "run.json").write_bytes(_json_line(summary))
        os.replace(temporary, destination)
        return summary
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
