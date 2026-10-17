"""Read-only D114 correction for reset ground-flag blips in one Fabric run."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import b10_gap_solver_runtime as b10


def _jsonl(path: Path):
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trace_bodies(path: Path) -> dict[int, SimpleNamespace]:
    result = {}
    for row in _jsonl(path):
        payload = row.get("payload", {})
        observation = payload.get("backend_result", {}).get("observation", {})
        body = observation.get("self_state", {}).get("value")
        if type(body) is not dict:
            continue
        tick = body.get("movement_tick_id")
        position = body.get("position")
        on_ground = body.get("is_on_ground")
        if (
            type(tick) is not int
            or type(position) is not dict
            or type(on_ground) is not bool
        ):
            continue
        if tick in result:
            raise ValueError(
                f"D114 trace has duplicate body evidence for movement tick {tick}"
            )
        result[tick] = SimpleNamespace(
            position=SimpleNamespace(
                x=position["x"], y=position["y"], z=position["z"]
            ),
            is_on_ground=on_ground,
        )
    return result


def analyze(client: Path) -> dict:
    trial_path = client / "b10-coordinator-trials.jsonl"
    trace_path = client / "trace.jsonl"
    before = {str(path.name): _sha256(path) for path in (trial_path, trace_path)}
    bodies = _trace_bodies(trace_path)
    trials = list(_jsonl(trial_path))
    corrected = []
    for trial in trials:
        if trial.get("scenario_kind") != "worker_delay" or trial.get(
            "worker_delay_ticks"
        ) != 10:
            continue
        submissions = trial["worker_injection"]["submissions"]
        start_tick = min(item["job_anchor_movement_tick"] for item in submissions)
        end_tick = max(
            [item["first_control_visible_tick"] for item in submissions]
            + [sample["grant_movement_tick"] for sample in trial["samples"]]
        )
        expected_ticks = tuple(range(start_tick, end_tick + 1))
        missing_ticks = tuple(tick for tick in expected_ticks if tick not in bodies)
        if missing_ticks:
            raise ValueError(
                "D114 trace omitted body evidence inside the signed control range: "
                f"trial={trial['name']!r}, missing_ticks={missing_ticks}"
            )
        start_position = tuple(trial["samples"][0]["position"])
        frames = tuple(
            SimpleNamespace(
                result=SimpleNamespace(
                    observation=SimpleNamespace(
                        self_state=SimpleNamespace(value=bodies[tick])
                    )
                )
            )
            for tick in expected_ticks
        )
        actual_departure = b10._b10_uncommanded_body_departed(
            frames,
            verified_command_count=trial["verified_command_count"],
            start_position=start_position,
        )
        revised = dict(trial, left_start_support=actual_departure)
        accepted = (
            b10._b10_coordinator_completed(revised)
            or b10._b10_coordinator_typed_safe_terminal(revised)
        )
        corrected.append({
            "name": trial["name"],
            "recorded_left_start_support": trial["left_start_support"],
            "actual_departure": actual_departure,
            "accepted_after_correction": accepted,
            "trace_tick_start": start_tick,
            "trace_tick_end": end_tick,
            "trace_frame_count": len(frames),
            "terminal_state": trial["terminal_state"],
            "terminal_reason": trial["terminal_reason"],
            "damage_points": trial["damage_points"],
            "source_released": trial["source_released"],
        })
    after = {str(path.name): _sha256(path) for path in (trial_path, trace_path)}
    if before != after:
        raise RuntimeError("D114 source evidence changed during read-only analysis")
    return {
        "schema_version": "mc2p.d114-formal-driver-run-analysis.v1",
        "source_hashes": before,
        "source_hashes_unchanged": True,
        "delay_10_count": len(corrected),
        "delay_10_safe_count_before": sum(
            not item["recorded_left_start_support"] for item in corrected
        ),
        "delay_10_safe_count_after": sum(
            item["accepted_after_correction"] for item in corrected
        ),
        "false_departure_count": sum(
            item["recorded_left_start_support"]
            and not item["actual_departure"]
            for item in corrected
        ),
        "trials": corrected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    result = analyze(args.client.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
