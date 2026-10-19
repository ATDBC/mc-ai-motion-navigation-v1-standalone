"""Replay C1-C recovery and ownership from the formal runtime trace."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mc2p.contracts.observation import Vec3V0
from mc2p.motion_nav.external_motion import (
    DamageKnockbackDetector, ExternalMotionEventV1, ExternalMotionSource,
)
from mc2p.motion_nav.motion_residual import (
    MotionResidualResult, MotionResidualStatus,
)
from mc2p.motion_nav.physics_types import PhysicsState
from mc2p.motion_nav.external_motion_recovery import ExternalMotionRecoveryController
from mc2p.runtime.segmented_trace import iter_segmented_jsonl
from mc2p.runtime.trace import trace_projection
from scripts.c1_melee_evidence import _observation_from_record


MAX_RECORDS = 100_000


def _failure(layer: str, reason: str, replayed: int) -> dict:
    return {
        "schema_version": "mc2p.c1-external-motion-replay.v2",
        "passed": False,
        "replayed_records": replayed,
        "earliest_failure_layer": layer,
        "reason": reason,
        "simulated_server_physics": False,
    }


def _vec(raw: dict) -> Vec3V0:
    return Vec3V0(raw["x"], raw["y"], raw["z"])


def _event(raw: dict) -> ExternalMotionEventV1:
    return ExternalMotionEventV1(
        raw["event_id"], raw["generation"], raw["episode_id"],
        raw["observation_sequence_id"], raw["movement_tick_id"],
        ExternalMotionSource(raw["source"]), raw["health_delta_points"],
        _vec(raw["previous_position"]), _vec(raw["position"]),
        _vec(raw["previous_velocity"]), _vec(raw["velocity"]),
        raw["was_on_ground"], raw["is_on_ground"],
        raw.get("absorption_delta_points", 0.0),
        raw.get("position_residual_blocks"),
        raw.get("velocity_residual_blocks_per_tick"),
    )


def _residual(raw: object) -> MotionResidualResult | None:
    if raw is None:
        return None
    if type(raw) is not dict:
        raise ValueError("motion residual record is invalid")
    predicted_raw = raw.get("predicted_state")
    predicted = None
    if predicted_raw is not None:
        if type(predicted_raw) is not dict:
            raise ValueError("predicted state record is invalid")
        state_data = dict(predicted_raw)
        session = state_data.get("session")
        if type(session) is dict:
            state_data["session"] = session.get("value")
        predicted = PhysicsState.from_mapping(state_data)
    return MotionResidualResult(
        MotionResidualStatus(raw["status"]),
        raw["anchor_tick"], raw["observed_tick"], predicted,
        raw.get("position_error_blocks"),
        raw.get("velocity_error_blocks_per_tick"),
        raw.get("contact_mismatch", False),
        tuple(tuple(cell) for cell in raw.get("dependencies", ())),
        tuple(raw.get("missing_ticks", ())),
        tuple(tuple(cell) for cell in raw.get("missing_cells", ())),
        tuple(raw.get("reasons", ())),
    )


def _replay_rows(rows: list[dict]) -> dict:
    observations, ordered_intents, dispatches, steps = {}, set(), {}, {}
    controllers, lifecycles, detectors, detected_events = {}, {}, {}, {}
    registered_at, cancelled_at, unregistered_at = {}, {}, {}
    handoffs = reanchors = replayed = decisions = 0
    attack_intents = set()
    engagement_identities = {}
    for row_index, row in enumerate(rows):
        record_type, payload = row.get("record_type"), row.get("payload")
        if record_type == "trace_delivery_summary" and (
            type(payload) is not dict or payload.get("dropped_records", 0) != 0
        ):
            return _failure("trace_integrity", "trace_delivery_gap", replayed)
        observation = _observation_from_record(row)
        if observation is not None:
            observations[observation.sequence_id] = observation
        if type(payload) is not dict:
            continue
        if record_type == "ordered_intent":
            intent = payload.get("envelope", {}).get("intent", {})
            intent_id = intent.get("intent_id")
            if type(intent_id) is str:
                ordered_intents.add(intent_id)
        elif record_type == "ordered_source_registered":
            source_id = payload.get("source", {}).get("source_id")
            if type(source_id) is str:
                registered_at[source_id] = row_index
        elif record_type == "source_cancel":
            source_id = payload.get("source_id")
            if type(source_id) is str:
                cancelled_at[source_id] = row_index
        elif record_type == "ordered_source_unregistered":
            source_id = payload.get("source", {}).get("source_id")
            if type(source_id) is str:
                unregistered_at[source_id] = row_index
        elif record_type == "dispatch":
            action = payload.get("decision", {}).get("action", {})
            if type(action.get("request_sequence_id")) is int:
                dispatches[action["request_sequence_id"]] = payload["decision"]
        elif record_type == "step":
            action = payload.get("decision", {}).get("action", {})
            if type(action.get("request_sequence_id")) is int:
                steps[action["request_sequence_id"]] = payload
        elif record_type == "combat_skill" and payload.get("candidate_id") == "attack":
            intent_id = payload.get("intent_id")
            if intent_id in attack_intents:
                return _failure("attack_responsibility", "duplicate_attack_intent", replayed)
            attack_intents.add(intent_id)
        elif record_type == "moving_engagement":
            key = payload.get("task_id")
            identity = (payload.get("goal_id"), payload.get("track_id"))
            previous = engagement_identities.setdefault(key, identity)
            if previous != identity:
                return _failure("target_identity", "target_identity_changed", replayed)

    for row_index, row in enumerate(rows):
        record_type = row.get("record_type")
        if record_type not in {
                "external_motion_detection", "external_motion_recovery",
        }:
            continue
        payload = row.get("payload")
        if type(payload) is not dict:
            return _failure("incomplete_evidence", "recovery_payload_missing", replayed)
        try:
            if record_type == "external_motion_detection":
                task_id = payload["task_id"]
                observation = observations[payload["observation_sequence_id"]]
                detector = detectors.setdefault(task_id, DamageKnockbackDetector())
                logged_detection = payload.get("detection")
                residual = _residual(
                    None if type(logged_detection) is not dict
                    else logged_detection.get("motion_residual")
                )
                detection = detector.observe(observation, residual)
                if trace_projection(detection) != payload.get("detection"):
                    return _failure(
                        "event_identity", "external_detection_replay_mismatch", replayed,
                    )
                if detection.event is not None:
                    detected_events[task_id] = detection.event
                replayed += 1
                continue
            stage = payload.get("stage")
            scope = payload.get("recovery_scope_id")
            if stage == "parent_handoff":
                old_source = payload.get("old_movement_source_id")
                recovery_source = payload.get("recovery_source_id")
                retired_sources = payload.get("retired_source_ids")
                active_sources = {
                    source_id for source_id, registered in registered_at.items()
                    if (registered < row_index
                        and unregistered_at.get(source_id, len(rows) + 1) >= row_index)
                }
                if (payload.get("old_front_invalidated") is not True
                        or (old_source is not None and type(old_source) is not str)
                        or type(recovery_source) is not str
                        or type(retired_sources) is not list
                        or any(type(source_id) is not str for source_id in retired_sources)
                        or (old_source is not None and old_source not in retired_sources)
                        or registered_at.get(recovery_source, row_index) >= row_index
                        or active_sources != {recovery_source}
                        or any(
                            registered_at.get(source_id, row_index) >= row_index
                            or cancelled_at.get(source_id, row_index) >= row_index
                            or unregistered_at.get(source_id, row_index) >= row_index
                            for source_id in retired_sources
                        )):
                    return _failure("route_invalidation", "old_front_not_invalidated", replayed)
                lifecycle = lifecycles.setdefault(scope, {})
                lifecycle["handoff"] = True
                lifecycle["recovery_source"] = recovery_source
                handoffs += 1
            elif stage == "started":
                logged_event = _event(payload["event"])
                detected = detected_events.get(scope)
                if (detected is None
                        or trace_projection(detected) != payload.get("event")):
                    return _failure(
                        "event_identity", "external_event_replay_mismatch", replayed,
                    )
                controller = controllers.setdefault(scope, ExternalMotionRecoveryController())
                controller.start(logged_event)
                lifecycles[scope] = {
                    "generation": logged_event.generation,
                    "decisions": 0,
                    "complete": False,
                    "completed_stage": False,
                    "complete_tick": None,
                    "handoff": False,
                    "recovery_source": None,
                }
                replayed += 1
            elif stage == "event_updated":
                controller = controllers[scope]
                logged_event = _event(payload["event"])
                detected = detected_events.get(scope)
                if (detected is None
                        or trace_projection(detected) != payload.get("event")
                        or not controller.observe_event(logged_event)):
                    return _failure("event_identity", "event_update_not_accepted", replayed)
                lifecycles[scope]["generation"] = logged_event.generation
                replayed += 1
            elif stage == "decision":
                owners = payload.get("movement_owner_source_ids")
                if type(owners) is not list or len(owners) != 1:
                    return _failure("movement_ownership", "movement_owner_count_invalid", replayed)
                owner = owners[0]
                if (registered_at.get(owner, row_index) >= row_index
                        or unregistered_at.get(owner, len(rows) + 1) < row_index):
                    return _failure("movement_ownership", "movement_owner_not_active", replayed)
                observation = observations[payload["observation_sequence_id"]]
                decision = controllers[scope].decide(observation)
                if trace_projection(decision) != payload.get("decision"):
                    return _failure("stability", "recovery_decision_replay_mismatch", replayed)
                lifecycle = lifecycles[scope]
                lifecycle["decisions"] += 1
                decisions += 1
                if decision.complete:
                    lifecycle["complete"] = True
                    lifecycle["complete_tick"] = payload.get("movement_tick_id")
                replayed += 1
            elif stage == "completed":
                lifecycle = lifecycles[scope]
                if lifecycle.get("decisions", 0) == 0:
                    return _failure(
                        "incomplete_evidence", "recovery_decisions_missing", replayed,
                    )
                if not lifecycle.get("complete"):
                    return _failure("stability", "completion_without_controller_decision", replayed)
                lifecycle["completed_stage"] = True
            elif stage == "parent_reanchored":
                lifecycle = lifecycles[scope]
                movement_tick = payload.get("movement_tick_id")
                if (type(movement_tick) is not int
                        or not lifecycle.get("completed_stage")
                        or lifecycle.get("generation") != payload.get("completed_generation")
                        or lifecycle.get("complete_tick") is None
                        or movement_tick < lifecycle["complete_tick"]):
                    return _failure("stability", "reanchor_tick_missing", replayed)
                reanchors += 1
        except (KeyError, TypeError, ValueError) as error:
            return _failure("event_identity", "cannot_reconstruct:" + str(error), replayed)

    for request_id, dispatch in dispatches.items():
        for _, intent_id in dispatch.get("selected_intents", []):
            if intent_id not in ordered_intents:
                return _failure("input_receipts", "selected_intent_missing", replayed)
        step = steps.get(request_id)
        receipt = None if step is None else step.get("backend_result", {}).get("receipt")
        if type(receipt) is not dict or not receipt.get("status"):
            return _failure("input_receipts", "receipt_missing", replayed)
    if replayed == 0 or decisions == 0 or handoffs == 0 or reanchors == 0:
        return _failure("incomplete_evidence", "recovery_chain_missing", replayed)
    return {
        "schema_version": "mc2p.c1-external-motion-replay.v2",
        "passed": True,
        "replayed_records": replayed,
        "earliest_failure_layer": None,
        "reason": "formal_recovery_trace_matched",
        "simulated_server_physics": False,
        "handoffs": handoffs,
        "reanchors": reanchors,
    }


def replay_c1_external_motion(evidence: Path) -> dict:
    rows = []
    try:
        for index, row in enumerate(iter_segmented_jsonl(Path(evidence))):
            if index >= MAX_RECORDS:
                return _failure("incomplete_evidence", "record_budget_exceeded", 0)
            if (type(row) is not dict
                    or row.get("schema_version") != "mc2p.trace-record.v0"):
                return _failure("incomplete_evidence", "invalid_trace_record", 0)
            rows.append(row)
    except (OSError, ValueError, TypeError) as error:
        return _failure("incomplete_evidence", str(error), 0)
    return _replay_rows(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    result = replay_c1_external_motion(args.evidence)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
