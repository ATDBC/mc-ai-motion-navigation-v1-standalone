"""Strict offline replay and earliest-layer attribution for C1-A evidence."""
from __future__ import annotations

from pathlib import Path

from mc2p.runtime.segmented_trace import iter_segmented_jsonl
from mc2p.runtime.trace import trace_projection
from mc2p.skills.fixed_melee import CombatTargetV1, FixedMeleePhase, decide_fixed_melee
from scripts.navigation_motion_evidence import restore_snapshot


MAX_TRACE_RECORDS = 100_000


def _failure(layer: str, reason: str, identity: dict | None = None,
             replayed: int = 0) -> dict:
    return {
        "schema_version": "mc2p.c1-melee-replay.v1",
        "passed": False,
        "replayed_decisions": replayed,
        "earliest_failure_layer": layer,
        "reason": reason,
        "identity": identity,
        "consequences": [],
    }


def _observation_from_record(row: dict):
    record_type, payload = row.get("record_type"), row.get("payload")
    if type(payload) is not dict:
        return None
    if record_type == "reset":
        result = payload.get("result")
        raw = result.get("observation") if type(result) is dict else None
    elif record_type == "step":
        result = payload.get("backend_result")
        raw = result.get("observation") if type(result) is dict else None
    else:
        return None
    return restore_snapshot(raw) if type(raw) is dict else None


def _event_key(payload: dict) -> tuple[str, int, int]:
    task_id = payload.get("task_id")
    revision = payload.get("target_revision")
    generation = payload.get("decision_generation")
    if (type(task_id) is not str or type(revision) is not int or revision < 0
            or type(generation) is not int or generation < 1):
        raise ValueError("invalid combat event identity")
    return task_id, revision, generation


def _generation_map(rows: list[dict], record_type: str) -> dict[tuple[str, int, int], dict]:
    result: dict[tuple[str, int, int], dict] = {}
    for row in rows:
        if row.get("record_type") != record_type or type(row.get("payload")) is not dict:
            continue
        payload = row["payload"]
        key = _event_key(payload)
        if key in result:
            raise ValueError(f"duplicate or invalid {record_type} generation")
        result[key] = payload
    return result


def replay_c1_melee(evidence: Path) -> dict:
    """Replay pure decisions and attribute the first mismatching contract layer."""
    rows: list[dict] = []
    observations = {}
    try:
        for index, row in enumerate(iter_segmented_jsonl(Path(evidence))):
            if index >= MAX_TRACE_RECORDS:
                return _failure("incomplete_evidence", "trace_record_budget_exceeded")
            if (type(row) is not dict
                    or row.get("schema_version") != "mc2p.trace-record.v0"):
                return _failure("incomplete_evidence", "invalid_trace_record")
            rows.append(row)
            if row.get("record_type") == "trace_delivery_summary":
                payload = row.get("payload")
                if type(payload) is not dict or payload.get("dropped_records", 0) != 0:
                    return _failure("incomplete_evidence", "trace_delivery_gap")
            observation = _observation_from_record(row)
            if observation is not None:
                observations[observation.sequence_id] = observation
    except (OSError, ValueError, TypeError) as error:
        return _failure("incomplete_evidence", str(error))

    try:
        assessments = _generation_map(rows, "combat_assessment")
        candidates = _generation_map(rows, "combat_candidates")
        selections = _generation_map(rows, "combat_selection")
        skills = _generation_map(rows, "combat_skill")
    except ValueError as error:
        return _failure("incomplete_evidence", str(error))
    if not assessments or set(candidates) != set(assessments) or set(selections) != set(assessments):
        return _failure("incomplete_evidence", "combat_decision_chain_incomplete")

    task_order = tuple(dict.fromkeys((key[0], key[1]) for key in assessments))
    generations = []
    for task_key in task_order:
        task_generations = tuple(sorted(key[2] for key in assessments if key[:2] == task_key))
        if task_generations != tuple(range(1, len(task_generations) + 1)):
            return _failure("incomplete_evidence", "combat_generation_gap")
        generations.extend((task_key[0], task_key[1], generation)
                           for generation in task_generations)

    decisions = {}
    identities = {}
    identity = None
    for replayed, key in enumerate(generations, 1):
        generation = key[2]
        assessment_event = assessments[key]
        selection_event = selections[key]
        assessment_raw = assessment_event.get("assessment")
        if type(assessment_raw) is not dict:
            return _failure("situation_assessment", "assessment_missing", identity, replayed - 1)
        try:
            identity = {
                "task_id": assessment_event["task_id"],
                "goal_id": assessment_event["goal_id"],
                "target_revision": assessment_event["target_revision"],
                "episode_id": assessment_event["episode_id"],
                "track_id": assessment_event["track_id"],
            }
            target = CombatTargetV1(
                identity["task_id"], identity["goal_id"], identity["target_revision"],
                identity["episode_id"], identity["track_id"],
            )
            observation = observations[assessment_raw["observation_sequence_id"]]
            phase = FixedMeleePhase(assessment_raw["phase"])
            decision = decide_fixed_melee(
                observation,
                target=target,
                phase=phase,
                generation=generation,
                now_ns=assessment_event["decision_time_ns"],
                controller_clock_id=assessment_event["controller_clock_id"],
                attack_observation_sequence_id=assessment_event.get(
                    "attack_observation_sequence_id"
                ),
                pre_attack_hurt_animation_ticks=assessment_event.get(
                    "pre_attack_hurt_animation_ticks"
                ),
                confirmation_deadline_ns=assessment_event.get("confirmation_deadline_ns"),
            )
        except (KeyError, TypeError, ValueError) as error:
            return _failure("situation_assessment", "cannot_reconstruct:" + str(error),
                            identity, replayed - 1)
        decisions[key] = decision
        identities[key] = identity
        if trace_projection(decision.assessment) != assessment_raw:
            return _failure("situation_assessment", "assessment_replay_mismatch",
                            identity, replayed - 1)
        if trace_projection(decision.candidates) != candidates[key].get("candidates"):
            return _failure("candidate_coverage", "candidate_replay_mismatch",
                            identity, replayed - 1)
        if (selection_event.get("selected_candidate_id") != decision.selected_candidate_id
                or selection_event.get("phase") != decision.phase.value):
            return _failure("selection", "selection_replay_mismatch",
                            identity, replayed - 1)

    ordered = {}
    dispatches = []
    steps = {}
    for row in rows:
        payload = row.get("payload")
        if type(payload) is not dict:
            continue
        if row.get("record_type") == "ordered_intent":
            envelope = payload.get("envelope")
            intent = envelope.get("intent") if type(envelope) is dict else None
            if type(intent) is dict and type(intent.get("intent_id")) is str:
                ordered[intent["intent_id"]] = intent
        elif row.get("record_type") == "dispatch":
            decision = payload.get("decision")
            if type(decision) is dict:
                dispatches.append(decision)
        elif row.get("record_type") == "step":
            decision = payload.get("decision")
            if type(decision) is dict and type(decision.get("action")) is dict:
                steps[decision["action"].get("request_sequence_id")] = payload

    expected_skill_generations = {
        key for key, decision in decisions.items()
        if decision.selected_candidate_id in {"aim", "attack"}
    }
    if set(skills) != expected_skill_generations:
        return _failure("skill_execution", "skill_record_set_mismatch", identity,
                        len(generations))
    for key in sorted(expected_skill_generations):
        decision = decisions[key]
        skill_identity = identities[key]
        skill = skills[key]
        intent_id = skill.get("intent_id")
        intent = ordered.get(intent_id)
        expected_operation = ({
            "entity_ref": skill_identity["track_id"],
            "kind": "attack_entity",
        } if decision.selected_candidate_id == "attack" else None)
        if (skill.get("candidate_id") != decision.selected_candidate_id
                or skill.get("operation") != expected_operation
                or type(intent) is not dict
                or intent.get("operation") != expected_operation):
            return _failure("skill_execution", "skill_or_intent_mismatch", skill_identity,
                            len(generations))

        dispatch = next((item for item in dispatches
                         if ["operation", intent_id] in item.get("selected_intents", [])
                         or ["look", intent_id] in item.get("selected_intents", [])), None)
        expected_group = "operation" if expected_operation is not None else "look"
        if (dispatch is None or [expected_group, intent_id] not in dispatch.get("selected_intents", [])
                or dispatch.get("action", {}).get("operation") != expected_operation
                or not skill.get("selected_by_arbiter")):
            return _failure("arbitration", "arbitration_output_mismatch", skill_identity,
                            len(generations))
        if expected_operation is not None:
            request_sequence = dispatch["action"]["request_sequence_id"]
            step = steps.get(request_sequence)
            receipt = (step.get("backend_result", {}).get("receipt")
                       if type(step) is dict else None)
            if (type(receipt) is not dict
                    or receipt.get("status") != "pending_confirmation"
                    or receipt.get("reason") != "entity_attack_dispatched"
                    or skill.get("receipt_status") != receipt.get("status")
                    or skill.get("receipt_reason") != receipt.get("reason")):
                return _failure("motion", "attack_receipt_mismatch", skill_identity,
                                len(generations))

    return {
        "schema_version": "mc2p.c1-melee-replay.v1",
        "passed": True,
        "replayed_decisions": len(generations),
        "earliest_failure_layer": None,
        "reason": "replay_matched",
        "identity": identity,
        "consequences": [],
    }
