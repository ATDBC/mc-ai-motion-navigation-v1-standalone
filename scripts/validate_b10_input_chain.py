"""Join B10 formal commands, applied-input receipts and Fabric movement ticks."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.runtime.segmented_trace import iter_segmented_jsonl


_INPUT_KEYS = ("forward", "strafe", "jump", "sneak", "sprint")


def _action_in(record: dict) -> dict | None:
    payload = record.get("payload", {})
    decision = payload.get("decision")
    if type(decision) is dict and type(decision.get("action")) is dict:
        return decision["action"]
    action = payload.get("action")
    return action if type(action) is dict else None


def _receipt_in(record: dict) -> dict | None:
    payload = record.get("payload", {})
    backend = payload.get("backend_result")
    if type(backend) is dict and type(backend.get("receipt")) is dict:
        return backend["receipt"]
    return None


def validate_rows(trace_rows: tuple[dict, ...],
                  physics_rows: tuple[dict, ...]) -> dict:
    actions: dict[int, dict] = {}
    applications: dict[int, dict] = {}
    application_bases: dict[int, int] = {}
    first_application: dict[int, int] = {}
    latest_observed_application: int | None = None
    mismatches: list[dict] = []
    for record in trace_rows:
        action = _action_in(record)
        if action is not None and type(action.get("request_sequence_id")) is int:
            sequence = action["request_sequence_id"]
            if sequence in actions and actions[sequence] != action:
                mismatches.append({"kind": "conflicting_command", "request_sequence_id": sequence})
            actions[sequence] = action
            if (record.get("record_type") == "dispatch"
                    and latest_observed_application is not None):
                application_bases.setdefault(sequence, latest_observed_application)
        receipt = _receipt_in(record)
        if receipt is None or receipt.get("schema_version") != "mc2p.client_action_receipt.v3":
            continue
        batch = receipt.get("input_applications")
        if type(batch) is not list:
            mismatches.append({"kind": "invalid_application_batch"})
            continue
        for application in batch:
            tick = application.get("movement_tick_id") if type(application) is dict else None
            if type(tick) is not int:
                mismatches.append({"kind": "invalid_application_tick"})
            elif tick in applications and applications[tick] != application:
                mismatches.append({"kind": "conflicting_application", "movement_tick_id": tick})
            else:
                applications[tick] = application
                sequence = application.get("request_sequence_id")
                if type(sequence) is int:
                    first_application.setdefault(sequence, tick)
                latest_observed_application = (tick if latest_observed_application is None
                                                else max(latest_observed_application, tick))

    physics = {}
    for row in physics_rows:
        tick = row.get("movement_tick_id") if type(row) is dict else None
        if type(tick) is not int:
            mismatches.append({"kind": "invalid_physics_tick"})
        elif tick in physics:
            mismatches.append({"kind": "duplicate_physics_tick", "movement_tick_id": tick})
        else:
            physics[tick] = row

    for tick, application in sorted(applications.items()):
        row = physics.get(tick)
        if row is None:
            mismatches.append({"kind": "missing_physics_tick", "movement_tick_id": tick})
            continue
        actual = row.get("actual_input")
        if type(actual) is not dict or any(actual.get(key) != application.get(key)
                                           for key in _INPUT_KEYS):
            mismatches.append({"kind": "application_physics_mismatch",
                               "movement_tick_id": tick})
        if row.get("request_sequence_id") != application.get("request_sequence_id"):
            mismatches.append({"kind": "application_owner_mismatch",
                               "movement_tick_id": tick})
        sequence = application.get("request_sequence_id")
        if sequence is None or application.get("state") not in {"leased", "neutral"}:
            continue
        action = actions.get(sequence)
        if action is None:
            mismatches.append({"kind": "missing_command", "request_sequence_id": sequence})
            continue
        movement = action.get("movement")
        if type(movement) is not dict or any(movement.get(key) != application.get(key)
                                             for key in _INPUT_KEYS):
            mismatches.append({"kind": "command_application_mismatch",
                               "request_sequence_id": sequence,
                               "movement_tick_id": tick})

    delays = [first_application[sequence] - baseline
              for sequence, baseline in application_bases.items()
              if sequence in first_application]
    delay_summary = {"samples": len(delays)}
    if delays:
        ordered = sorted(delays)
        delay_summary.update(
            p50=ordered[math.ceil(len(ordered) * .50) - 1],
            p95=ordered[math.ceil(len(ordered) * .95) - 1],
            p99=ordered[math.ceil(len(ordered) * .99) - 1],
            maximum=ordered[-1],
        )
    return {
        "schema_version": "mc2p.b10-input-chain-validation.v1",
        "command_count": len(actions),
        "application_count": len(applications),
        "owned_application_count": sum(
            item.get("request_sequence_id") is not None
            for item in applications.values()),
        "physics_tick_count": len(physics),
        "command_application_delay_ticks": delay_summary,
        "mismatches": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace", type=Path)
    parser.add_argument("physics_evidence", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    trace_rows = tuple(json.loads(line) for line in args.trace.read_text("utf-8").splitlines()
                       if line.strip())
    physics_rows = tuple(iter_segmented_jsonl(args.physics_evidence))
    report = validate_rows(trace_rows, physics_rows)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if not report["mismatches"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
