"""Replay C1-B from the formal runtime trace using production decisions."""
from __future__ import annotations

import math
from pathlib import Path

from mc2p.contracts.observation import Vec3V0
from mc2p.runtime.segmented_trace import iter_segmented_jsonl
from mc2p.runtime.trace import trace_projection
from mc2p.skills.engagement_memory import (
    EngagementEventKind, EngagementStateV1, TargetPositionFactV1,
    TargetPositionSource, advance_engagement, event_from_observation,
    resolve_target_position,
)
from mc2p.skills.fixed_melee import CombatTargetV1, MAX_COARSE_ATTACK_DISTANCE_BLOCKS
from mc2p.skills.moving_melee import MovingMeleePhase, decide_moving_melee
from mc2p.skills.moving_target import decide_moving_goal
from scripts.c1_melee_evidence import _observation_from_record


CAUSAL_LAYERS = (
    "engagement_memory", "moving_decision", "moving_goal",
    "skill_execution", "arbitration_output", "motion_attack_result",
)
MAX_RECORDS = 100_000


def _failure(layer: str, reason: str, replayed: int) -> dict:
    return {
        "schema_version": "mc2p.c1-moving-melee-replay.v2",
        "passed": False,
        "replayed_decisions": replayed,
        "earliest_failure_layer": layer,
        "reason": reason,
        "simulated_entity_ai": False,
    }


def _vec(raw: dict) -> Vec3V0:
    return Vec3V0(raw["x"], raw["y"], raw["z"])


def _fact(raw: dict) -> TargetPositionFactV1:
    return TargetPositionFactV1(
        raw["track_id"], TargetPositionSource(raw["source"]),
        _vec(raw["relative_position"]), _vec(raw["relative_velocity"]),
        raw["observation_sequence_id"], raw["received_at_monotonic_ns"],
        raw.get("health_points"), raw.get("max_health_points"), raw.get("is_dead"),
    )


def _target(payload: dict) -> CombatTargetV1:
    return CombatTargetV1(
        payload["task_id"], payload["goal_id"], payload["target_revision"],
        payload["episode_id"], payload["track_id"],
    )


def _replay_rows(rows: list[dict]) -> dict:
    observations, engagement_states, engagement_facts, moving_goals = {}, {}, {}, {}
    ordered_intents, dispatches, steps = set(), {}, {}
    replayed = 0
    saw_engagement = saw_goal = saw_dispatch = False
    if (not any(row.get("record_type") == "moving_engagement" for row in rows)
            and any(row.get("record_type") in {
                "moving_melee_decision", "moving_goal_decision",
            } for row in rows)):
        return _failure("incomplete_evidence", "engagement_chain_missing", replayed)
    for row in rows:
        record_type, payload = row.get("record_type"), row.get("payload")
        if record_type == "trace_delivery_summary" and (
            type(payload) is not dict or payload.get("dropped_records", 0) != 0
        ):
            return _failure("incomplete_evidence", "trace_delivery_gap", replayed)
        observation = _observation_from_record(row)
        if observation is not None:
            observations[observation.sequence_id] = observation
        if type(payload) is not dict:
            continue
        if record_type == "ordered_intent":
            intent = payload.get("envelope", {}).get("intent", {})
            if type(intent.get("intent_id")) is str:
                ordered_intents.add(intent["intent_id"])
        elif record_type == "dispatch":
            action = payload.get("decision", {}).get("action", {})
            if type(action.get("request_sequence_id")) is int:
                dispatches[action["request_sequence_id"]] = payload["decision"]
        elif record_type == "step":
            action = payload.get("decision", {}).get("action", {})
            if type(action.get("request_sequence_id")) is int:
                steps[action["request_sequence_id"]] = payload

    for row in rows:
        record_type, payload = row.get("record_type"), row.get("payload")
        if type(payload) is not dict:
            continue
        try:
            if record_type == "moving_engagement":
                target = _target(payload)
                key = (target.task_id, target.revision)
                state = engagement_states.get(key, EngagementStateV1.for_target(target))
                event_raw = payload["event"]
                observation = observations[event_raw["observation_sequence_id"]]
                event = event_from_observation(
                    target, observation, kind=EngagementEventKind(event_raw["kind"]),
                )
                state = advance_engagement(state, event)
                fact = resolve_target_position(
                    state, observation, target, payload["decision_time_ns"],
                )
                if (trace_projection(event) != event_raw
                        or trace_projection(state) != payload.get("state")
                        or trace_projection(fact) != payload.get("fact")):
                    return _failure("engagement_memory", "engagement_replay_mismatch", replayed)
                engagement_states[key] = state
                engagement_facts[key] = (fact, observation.sequence_id, target)
                saw_engagement = True
                replayed += 1
            elif record_type == "moving_melee_decision":
                target = _target(payload)
                key = (target.task_id, target.revision)
                observation = observations[payload["observation_sequence_id"]]
                fact, fact_sequence, fact_target = engagement_facts[key]
                if (fact_sequence != observation.sequence_id
                        or fact_target != target
                        or payload.get("position_source") != fact.source.value
                        or payload.get("within_attack_distance") != (
                            math.hypot(fact.relative_position.x, fact.relative_position.z)
                            <= MAX_COARSE_ATTACK_DISTANCE_BLOCKS
                        )
                        or payload.get("target_dead") is not (fact.is_dead is True)):
                    return _failure(
                        "moving_decision", "decision_input_not_from_engagement", replayed,
                    )
                decision = decide_moving_melee(
                    MovingMeleePhase(payload["input_phase"]),
                    position_source=fact.source,
                    within_attack_distance=(
                        math.hypot(fact.relative_position.x, fact.relative_position.z)
                        <= MAX_COARSE_ATTACK_DISTANCE_BLOCKS
                    ),
                    target_dead=fact.is_dead is True,
                    strike_reason=payload.get("strike_reason"),
                )
                if trace_projection(decision) != payload.get("decision"):
                    return _failure("moving_decision", "moving_decision_replay_mismatch", replayed)
                replayed += 1
            elif record_type == "moving_goal_decision":
                target = _target(payload)
                key = (target.task_id, target.revision)
                observation = observations[payload["observation_sequence_id"]]
                fact, fact_sequence, fact_target = engagement_facts[key]
                own = observation.self_state.value
                if (fact_sequence != observation.sequence_id
                        or fact_target != target
                        or own is None
                        or trace_projection(fact) != payload.get("fact")
                        or trace_projection(own.position) != payload.get("self_position")):
                    return _failure(
                        "moving_goal", "goal_input_not_from_engagement", replayed,
                    )
                previous = moving_goals.get(key)
                if trace_projection(previous) != payload.get("previous"):
                    return _failure("moving_goal", "moving_goal_predecessor_mismatch", replayed)
                decision = decide_moving_goal(
                    fact, own.position,
                    payload["scope_id"], payload["deadline_ns"], previous=previous,
                )
                if trace_projection(decision) != payload.get("decision"):
                    return _failure("moving_goal", "moving_goal_replay_mismatch", replayed)
                if payload.get("adopted") is True:
                    moving_goals[key] = decision
                saw_goal = True
                replayed += 1
        except (KeyError, TypeError, ValueError) as error:
            layer = ("engagement_memory" if record_type == "moving_engagement" else
                     "moving_goal" if record_type == "moving_goal_decision" else
                     "moving_decision")
            return _failure(layer, "cannot_reconstruct:" + str(error), replayed)

    for request_id, dispatch in dispatches.items():
        saw_dispatch = True
        for _, intent_id in dispatch.get("selected_intents", []):
            if intent_id not in ordered_intents:
                return _failure("skill_execution", "selected_intent_missing", replayed)
        step = steps.get(request_id)
        if step is None:
            return _failure("arbitration_output", "dispatch_without_step", replayed)
        receipt = step.get("backend_result", {}).get("receipt")
        if type(receipt) is not dict or not receipt.get("status"):
            return _failure("motion_attack_result", "receipt_missing", replayed)
    if replayed == 0 or not saw_engagement or not saw_goal or not saw_dispatch:
        return _failure("incomplete_evidence", "decision_or_runtime_chain_missing", replayed)
    return {
        "schema_version": "mc2p.c1-moving-melee-replay.v2",
        "passed": True,
        "replayed_decisions": replayed,
        "earliest_failure_layer": None,
        "reason": "formal_trace_replay_matched",
        "simulated_entity_ai": False,
    }


def replay_c1_moving_melee(evidence: Path) -> dict:
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
