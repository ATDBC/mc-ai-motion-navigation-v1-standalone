"""Fail-closed D129 classification from immutable run-10 raw evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Mapping

from mc2p.contracts.action_receipt import ClientBehaviorReceiptV4
from mc2p.contracts.common import ContractViolation


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if type(value) is not dict:
                raise ValueError(f"D129 JSONL row is not an object: {path}")
            rows.append(value)
    return rows


def _action_key(action: Mapping) -> tuple:
    window = action.get("first_effect_window")
    return (
        action.get("episode_id"), action.get("request_sequence_id"),
        action.get("observation_sequence_id"),
        None if type(window) is not dict else window.get("earliest_tick"),
        None if type(window) is not dict else window.get("latest_tick"),
    )


def _non_neutral(movement) -> bool:
    return (type(movement) is dict and (
        movement.get("forward") != 0 or movement.get("strafe") != 0
        or movement.get("jump") is True or movement.get("sneak") is True
        or movement.get("sprint") is True
    ))


def _observation_body(observation: Mapping) -> dict:
    state = observation.get("self_state")
    state = None if type(state) is not dict else state.get("value")
    health = observation.get("health_points")
    health = None if type(health) is not dict else health.get("value")
    if type(state) is not dict or type(health) not in (int, float):
        raise ValueError("D129 raw observation lacks typed body or health")
    position = state.get("position")
    if type(position) is not dict:
        raise ValueError("D129 raw observation lacks body position")
    return {
        "sequence_id": observation.get("sequence_id"),
        "movement_tick_id": state.get("movement_tick_id"),
        "position": [position.get(axis) for axis in "xyz"],
        "on_ground": state.get("is_on_ground"),
        "health_points": health,
        "damage_events": observation.get("damage_events"),
    }


def _verify_manifest(run_directory: Path) -> dict:
    manifest_path = run_directory / "manifest.json"
    manifest = _json(manifest_path)
    files = manifest.get("files") if type(manifest) is dict else None
    if (manifest.get("schema") != "mc2p.d129-invalid-harness-run-manifest.v1"
            or type(files) is not list or len(files) != 50):
        raise ValueError("D129 run-10 manifest shape differs")
    seen = set()
    root = run_directory.resolve()
    for item in files:
        if (type(item) is not dict
                or set(item) != {"path", "bytes", "sha256"}
                or type(item["path"]) is not str
                or type(item["bytes"]) is not int
                or type(item["sha256"]) is not str):
            raise ValueError("D129 run-10 manifest entry differs")
        relative = PurePosixPath(item["path"])
        if relative.is_absolute() or ".." in relative.parts or item["path"] in seen:
            raise ValueError("D129 run-10 manifest path is unsafe or duplicated")
        seen.add(item["path"])
        path = (run_directory / Path(*relative.parts)).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError("D129 run-10 manifest path escaped") from error
        if (not path.is_file() or path.stat().st_size != item["bytes"]
                or _sha256(path) != item["sha256"]):
            raise ValueError(f"D129 run-10 manifest hash differs: {item['path']}")
    required = {
        "source-run/client-0/d123-transport/d123-south-0-timely.json",
        "source-run/client-0/ftick-events/segment-00000000.jsonl",
        "source-run/client-0/physics-tick-events/segment-00000000.jsonl",
        "source-run/client-0/trace.jsonl",
        "source-run/result.json",
    }
    if not required <= seen:
        raise ValueError("D129 run-10 manifest omits classification evidence")
    return {
        "path": "manifest.json",
        "sha256": _sha256(manifest_path),
        "verified_file_count": len(files),
    }


def load_invalid_harness_evidence(run_directory: Path) -> dict:
    """Verify all 50 hashes, then extract classification facts from raw files."""
    run_directory = Path(run_directory)
    manifest = _verify_manifest(run_directory)
    client = run_directory / "source-run/client-0"
    trial = _json(client / "d123-transport/d123-south-0-timely.json")
    ftick = _jsonl(client / "ftick-events/segment-00000000.jsonl")
    physics = _jsonl(client / "physics-tick-events/segment-00000000.jsonl")
    trace = _jsonl(client / "trace.jsonl")

    dispatches = [
        row for row in trace
        if row.get("record_type") == "dispatch"
        and type(row.get("payload")) is dict
        and type(row["payload"].get("decision")) is dict
        and type(row["payload"]["decision"].get("action")) is dict
        and row["payload"]["decision"]["action"].get("first_effect_window")
        is not None
        and _non_neutral(
            row["payload"]["decision"]["action"].get("movement")
        )
    ]
    if len(dispatches) != 1:
        raise ValueError("D129 raw evidence has no unique tested dispatch")
    action = dispatches[0]["payload"]["decision"]["action"]
    key = _action_key(action)
    steps = [
        row for row in trace
        if row.get("record_type") == "step"
        and type(row.get("payload")) is dict
        and type(row["payload"].get("decision")) is dict
        and type(row["payload"]["decision"].get("action")) is dict
        and _action_key(row["payload"]["decision"]["action"]) == key
    ]
    backend_steps = [
        row for row in trial.get("backend_steps", ())
        if type(row) is dict and _action_key(row) == key
    ]
    received = [
        row for row in ftick
        if row.get("kind") == "CLIENT_FRAME_RECEIVED"
        and (row.get("episode_id"), row.get("request_sequence_id"),
             row.get("observation_sequence_id")) == key[:3]
    ]
    sent = [
        row for row in ftick
        if row.get("kind") == "CLIENT_OBSERVATION_SENT"
        and (row.get("episode_id"), row.get("request_sequence_id"),
             row.get("observation_sequence_id")) == key[:3]
    ]
    if not steps or not backend_steps:
        raise ValueError("D129 raw evidence lacks tested step or receipt")
    receipt = backend_steps[0].get("receipt")
    if type(receipt) is not dict:
        raise ValueError("D129 raw evidence lacks tested receipt")

    observations = []
    for row in trace:
        payload = row.get("payload")
        backend = None if type(payload) is not dict else payload.get("backend_result")
        observation = None if type(backend) is not dict else backend.get("observation")
        if type(observation) is dict:
            observations.append(observation)
    start_observations = [
        row for row in observations
        if row.get("episode_id") == action.get("episode_id")
        and row.get("sequence_id") == action.get("observation_sequence_id")
    ]
    terminal_rows = [
        row for row in trace
        if row.get("record_type") == "close_release"
        and type(row.get("payload")) is dict
        and type(row["payload"].get("backend_result")) is dict
        and type(row["payload"]["backend_result"].get("observation")) is dict
    ]
    if len(start_observations) != 1 or len(terminal_rows) != 1:
        raise ValueError("D129 raw evidence lacks unique start or terminal observation")
    terminal_observation = terminal_rows[0]["payload"]["backend_result"]["observation"]
    start_body = _observation_body(start_observations[0])
    terminal_body = _observation_body(terminal_observation)
    interval_observations = [
        _observation_body(row) for row in observations
        if (type(row.get("self_state")) is dict
            and type(row["self_state"].get("value")) is dict
            and type(row["self_state"]["value"].get("movement_tick_id")) is int
            and start_body["movement_tick_id"]
            <= row["self_state"]["value"]["movement_tick_id"]
            <= terminal_body["movement_tick_id"])
    ]
    step_payload = steps[0]["payload"]
    return {
        "manifest": manifest,
        "action": action,
        "dispatch": {
            "pre_dispatch_elapsed_ns": (
                step_payload.get("dispatched_monotonic_ns", 0)
                - step_payload.get("decision_observation_received_ns", 0)
            ),
        },
        "identity_records": {
            "dispatches": dispatches,
            "steps": steps,
            "backend_steps": backend_steps,
            "ftick_received": received,
            "ftick_sent": sent,
        },
        "receipt": receipt,
        "physics": physics,
        "start_observation": start_body,
        "terminal_observation": terminal_body,
        "interval_observations": interval_observations,
        "trial": trial,
    }


def evaluate_invalid_harness_evidence(evidence: Mapping) -> dict:
    action = evidence.get("action") if type(evidence) is dict else None
    action = action if type(action) is dict else {}
    window = action.get("first_effect_window")
    window = window if type(window) is dict else {}
    receipt = evidence.get("receipt")
    receipt = receipt if type(receipt) is dict else {}
    try:
        parsed_receipt = ClientBehaviorReceiptV4.from_mapping(receipt)
    except ContractViolation:
        parsed_receipt = None
    records = evidence.get("identity_records")
    records = records if type(records) is dict else {}
    physics = evidence.get("physics")
    physics = physics if type(physics) is list else []
    trial = evidence.get("trial")
    trial = trial if type(trial) is dict else {}
    start = evidence.get("start_observation")
    start = start if type(start) is dict else {}
    terminal = evidence.get("terminal_observation")
    terminal = terminal if type(terminal) is dict else {}
    interval_observations = evidence.get("interval_observations")
    interval_observations = (
        interval_observations if type(interval_observations) is list else []
    )

    key = _action_key(action)
    sent = {}
    identity_unique = parsed_receipt is not None and all(
        type(records.get(name)) is list and len(records[name]) == 1
        for name in (
            "dispatches", "steps", "backend_steps", "ftick_received",
            "ftick_sent",
        )
    )
    if identity_unique:
        backend_step = records["backend_steps"][0]
        sent = records["ftick_sent"][0]
        received = records["ftick_received"][0]
        identity_unique = (
            _action_key(backend_step) == key
            and (parsed_receipt.episode_id,
                 parsed_receipt.request_sequence_id)
            == key[:2]
            and (received.get("episode_id"), received.get("request_sequence_id"),
                 received.get("observation_sequence_id")) == key[:3]
            and (sent.get("episode_id"), sent.get("request_sequence_id"),
                 sent.get("observation_sequence_id")) == key[:3]
            and sent.get("generation_id") == parsed_receipt.generation_id
            and type(parsed_receipt.generation_id) is int
            and received.get("event_sequence") < sent.get("event_sequence")
            and received.get("movement_tick_id") is None
            and sent.get("movement_tick_id") is not None
            and len({row.get("session_id") for row in physics}) == 1
            and len({row.get("world_id") for row in physics}) == 1
        )

    applications = (() if parsed_receipt is None
                    else parsed_receipt.input_applications)
    target_applications = [
        row for row in applications
        if (row.episode_id, row.request_sequence_id) == key[:2]
    ]
    tested_non_neutral = [
        row for row in target_applications if (
            row.forward != 0 or row.strafe != 0 or row.jump
            or row.sneak or row.sprint
        )
    ]
    earliest = window.get("earliest_tick")
    terminal_tick = terminal.get("movement_tick_id")
    relevant_physics = [
        row for row in physics
        if type(row) is dict and type(row.get("movement_tick_id")) is int
        and type(earliest) is int and row["movement_tick_id"] >= earliest
        and type(terminal_tick) is int
        and row["movement_tick_id"] <= terminal_tick
    ]
    physics_ticks = [row["movement_tick_id"] for row in relevant_physics]
    physics_continuous = (
        type(earliest) is int and type(terminal_tick) is int
        and physics_ticks == list(range(earliest, terminal_tick + 1))
        and all(
            previous.get("post_state") == current.get("pre_state")
            for previous, current in zip(
                relevant_physics, relevant_physics[1:],
            )
        )
    )
    identity_unique = identity_unique and physics_continuous
    default_applications = [
        row for row in relevant_physics
        if row.get("input_state") == "default_active"
    ]
    default_receipt_applications = [
        row for row in applications if row.state == "default_active"
    ]
    physics_target_applications = [
        row for row in relevant_physics
        if (row.get("episode_id"), row.get("request_sequence_id"))
        == key[:2]
    ]
    start_position = start.get("position")
    body_unchanged = (
        type(start_position) is list and bool(relevant_physics)
        and all(
            type(row.get("post_state")) is dict
            and type(row["post_state"].get("position")) is dict
            and [row["post_state"]["position"].get(axis) for axis in "xyz"]
            == start_position
            and row["post_state"].get("on_ground") is True
            and not _non_neutral(row.get("actual_input"))
            for row in relevant_physics
        )
        and terminal.get("position") == start_position
        and terminal.get("on_ground") is True
    )
    observed_health = [
        row.get("health_points") for row in interval_observations
    ]
    health_unchanged = (
        type(start.get("health_points")) in (int, float)
        and terminal.get("health_points") == start.get("health_points")
        and bool(interval_observations)
        and all(type(value) in (int, float) for value in observed_health)
        and all(
            current >= previous
            for previous, current in zip(observed_health, observed_health[1:])
        )
        and all(
            row.get("damage_events") == [] for row in interval_observations
        )
    )
    explicit_transport_fact = any(
        type(row) is dict and row.get("transport_fact") is not None
        for row in trial.get("frames", ())
    ) or bool(trial.get("transport_events"))
    dropped_input_samples = (
        None if parsed_receipt is None
        else parsed_receipt.dropped_input_samples
    )
    transport_fact = (
        explicit_transport_fact or dropped_input_samples not in {0}
    )
    formal_failure = (
        trial.get("runtime_state") != "ready"
        or trial.get("runtime_failure") is not None
        or trial.get("error") is not None
        or trial.get("driver_state") in {
            "failed", "unresolved", "cancelled",
        }
    )
    timing = (() if parsed_receipt is None else parsed_receipt.timing_events)
    pending = [row for row in timing
               if row.status.value == "window_pending"
               and (row.episode_id, row.request_sequence_id) == key[:2]]
    missed = [row for row in timing
              if row.status.value == "window_missed"
              and (row.episode_id, row.request_sequence_id) == key[:2]]
    sent_tick = sent.get("movement_tick_id")
    causal = (
        start.get("movement_tick_id") is not None
        and earliest == start.get("movement_tick_id") + 1
        and window.get("latest_tick") == earliest
        and evidence.get("dispatch", {}).get("pre_dispatch_elapsed_ns", 0)
        >= 50_000_000
        and len(pending) == 1 and len(missed) == 1
        and pending[0].movement_tick_id > window.get("latest_tick", 0)
        and missed[0].movement_tick_id > window.get("latest_tick", 0)
        and type(sent_tick) is int
        and pending[0].movement_tick_id == sent_tick - 1
        and missed[0].movement_tick_id == sent_tick
    )
    no_independent_failure = (
        not transport_fact and not formal_failure
        and parsed_receipt is not None
        and parsed_receipt.status == "pending_confirmation"
        and parsed_receipt.reason == "window_pending"
        and len(missed) == 1
    )
    conjunction = {
        "fixture_or_tool_causally_broke_registered_precondition": causal,
        "raw_request_receipt_ftick_physics_identity_is_unique": identity_unique,
        "tested_non_neutral_input_application_count_is_zero": (
            len(tested_non_neutral) == 0
        ),
        "safe_default_application_count_is_zero": (
            len(default_applications) == 0
            and len(default_receipt_applications) == 0
        ),
        "body_did_not_enter_tested_behavior": body_unchanged,
        "health_did_not_decrease": health_unchanged,
        "no_independent_transport_protocol_or_product_safety_failure": (
            no_independent_failure
        ),
    }
    disqualifiers = {
        "fixture_cause_not_proved": not causal,
        "identity_not_unique": not identity_unique,
        "any_tested_input_application": bool(
            target_applications or physics_target_applications
        ),
        "any_default_application": bool(
            default_applications or default_receipt_applications
        ),
        "body_entered_action": not body_unchanged,
        "health_damage": not health_unchanged,
        "transport_fact": transport_fact,
        "formal_driver_or_runtime_failure": formal_failure,
    }
    eligible = all(conjunction.values()) and not any(disqualifiers.values())
    manifest = evidence.get("manifest")
    manifest = manifest if type(manifest) is dict else {}
    return {
        "schema": "mc2p.d129-invalid-harness-validation.v1",
        "status": "PASS" if eligible else "REJECTED",
        "eligible_for_invalid_harness": eligible,
        "manifest": manifest,
        "identity": {
            "episode_id": key[0], "request_sequence_id": key[1],
            "observation_sequence_id": key[2],
            "first_effect_window": {
                "earliest_tick": key[3], "latest_tick": key[4],
            },
            "receipt_generation_id": (
                None if parsed_receipt is None
                else parsed_receipt.generation_id
            ),
        },
        "counts": {
            "target_input_applications": len(target_applications),
            "target_non_neutral_applications": len(tested_non_neutral),
            "default_applications": (
                len(default_applications)
                + len(default_receipt_applications)
            ),
            "physics_rows_checked": len(relevant_physics),
        },
        "conjunction": conjunction,
        "disqualifiers": disqualifiers,
    }


def validate_invalid_harness_run(run_directory: Path) -> dict:
    return evaluate_invalid_harness_evidence(
        load_invalid_harness_evidence(run_directory),
    )
