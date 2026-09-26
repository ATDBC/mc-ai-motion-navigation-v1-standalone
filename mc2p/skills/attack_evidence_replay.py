"""Bounded offline reconstruction of one B12 attack attempt."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from mc2p.skills.attack_evidence import (
    AttackAttemptKeyV1,
    AttackAttemptOutcome,
    AttackAttemptPhase,
    AttackAttemptReportV1,
    AttackEvidenceGrade,
)


MAX_REPLAY_RECORDS = 100_000


def _payload_matches(payload: object, key: AttackAttemptKeyV1) -> bool:
    return bool(
        isinstance(payload, Mapping)
        and payload.get("episode_id") == key.episode_id
        and payload.get("task_id") == key.task_id
        and payload.get("goal_id") == key.goal_id
        and payload.get("target_revision") == key.target_revision
        and payload.get("track_id") == key.track_id
        and payload.get("attempt_sequence") == key.attempt_sequence
    )


def _selected_intent(decision: Mapping, group: str) -> str | None:
    values = decision.get("selected_intents")
    if not isinstance(values, list):
        return None
    for item in values:
        if (isinstance(item, list) and len(item) == 2
                and item[0] == group and isinstance(item[1], str)):
            return item[1]
    return None


def _target_dead_from_step(row: Mapping, key: AttackAttemptKeyV1) -> bool:
    if row.get("record_type") != "step":
        return False
    payload = row.get("payload")
    backend = payload.get("backend_result") if isinstance(payload, Mapping) else None
    observation = backend.get("observation") if isinstance(backend, Mapping) else None
    tracked = observation.get("tracked_entity") if isinstance(observation, Mapping) else None
    value = tracked.get("value") if isinstance(tracked, Mapping) else None
    return bool(
        isinstance(value, Mapping)
        and value.get("track_id") == key.track_id
        and value.get("is_dead") is True
    )


def _target_facts_from_step(
    row: Mapping, key: AttackAttemptKeyV1,
) -> tuple[int, int, int | None, float | None, bool] | None:
    if row.get("record_type") != "step":
        return None
    payload = row.get("payload")
    backend = payload.get("backend_result") if isinstance(payload, Mapping) else None
    observation = backend.get("observation") if isinstance(backend, Mapping) else None
    if not isinstance(observation, Mapping):
        return None
    sequence = observation.get("sequence_id")
    received = observation.get("received_at_monotonic_ns")
    if not isinstance(sequence, int) or not isinstance(received, int):
        return None
    hurt = None
    perception = observation.get("perception")
    perception_value = perception.get("value") if isinstance(perception, Mapping) else None
    visible = (
        perception_value.get("visible_entities")
        if isinstance(perception_value, Mapping) else None
    )
    if isinstance(visible, list):
        entity = next((item for item in visible
                       if isinstance(item, Mapping)
                       and item.get("track_id") == key.track_id), None)
        if isinstance(entity, Mapping) and isinstance(entity.get("hurt_animation_ticks"), int):
            hurt = entity["hurt_animation_ticks"]
    health = None
    dead = False
    tracked = observation.get("tracked_entity")
    value = tracked.get("value") if isinstance(tracked, Mapping) else None
    if isinstance(value, Mapping) and value.get("track_id") == key.track_id:
        if isinstance(value.get("health_points"), (int, float)):
            health = float(value["health_points"])
        dead = value.get("is_dead") is True
    return sequence, received, hurt, health, dead


def replay_attack_attempt(
    records: Iterable[Mapping],
    key: AttackAttemptKeyV1,
) -> AttackAttemptReportV1:
    """Rebuild one terminal result without trusting the online outcome field."""
    if type(key) is not AttackAttemptKeyV1:
        raise TypeError("attack replay requires AttackAttemptKeyV1")
    rows = []
    for index, row in enumerate(records):
        if index >= MAX_REPLAY_RECORDS:
            raise ValueError("attack replay record budget exceeded")
        if not isinstance(row, Mapping):
            raise ValueError("attack replay record must be a mapping")
        rows.append(row)

    matching_payloads = [
        row.get("payload") for row in rows
        if _payload_matches(row.get("payload"), key)
    ]
    if not matching_payloads:
        raise ValueError("attack attempt identity is absent from evidence")
    terminal_index = next((
        index for index, row in enumerate(rows)
        if row.get("record_type") == "attack_attempt"
        and _payload_matches(row.get("payload"), key)
    ), None)
    if terminal_index is not None:
        # The terminal event closes this attempt's evidence window.  Later
        # fixture cleanup or a new task may still observe the same entity
        # becoming hurt or dead; those facts cannot rewrite the old result.
        rows = rows[:terminal_index + 1]
    skills = [
        payload for row in rows
        if row.get("record_type") == "combat_skill"
        and _payload_matches((payload := row.get("payload")), key)
        and payload.get("candidate_id") == "attack"
    ]
    if len(skills) != 1:
        raise ValueError("attack replay requires exactly one attack skill event")
    skill = skills[0]
    intent_id = skill.get("intent_id")
    if not isinstance(intent_id, str):
        raise ValueError("attack replay is missing intent identity")
    if skill.get("selected_by_arbiter") is not True:
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.DEFERRED_BY_ARBITRATION,
            AttackEvidenceGrade.NONE, intent_id=intent_id,
        )

    dispatch = None
    for row in rows:
        if row.get("record_type") != "dispatch":
            continue
        payload = row.get("payload")
        decision = payload.get("decision") if isinstance(payload, Mapping) else None
        if isinstance(decision, Mapping) and _selected_intent(decision, "operation") == intent_id:
            dispatch = decision
            break
    action = dispatch.get("action") if isinstance(dispatch, Mapping) else None
    request_sequence = (
        action.get("request_sequence_id") if isinstance(action, Mapping) else None
    )
    if not isinstance(request_sequence, int) or request_sequence < 0:
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.OBSERVATION_INTERRUPTED,
            AttackEvidenceGrade.NONE, intent_id=intent_id,
        )

    step = None
    for row in rows:
        if row.get("record_type") != "step":
            continue
        payload = row.get("payload")
        decision = payload.get("decision") if isinstance(payload, Mapping) else None
        step_action = decision.get("action") if isinstance(decision, Mapping) else None
        if (isinstance(step_action, Mapping)
                and step_action.get("request_sequence_id") == request_sequence):
            step = row
            break
    payload = step.get("payload") if isinstance(step, Mapping) else None
    backend = payload.get("backend_result") if isinstance(payload, Mapping) else None
    receipt = backend.get("receipt") if isinstance(backend, Mapping) else None
    status = receipt.get("status") if isinstance(receipt, Mapping) else None
    if not isinstance(status, str):
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.OBSERVATION_INTERRUPTED,
            AttackEvidenceGrade.NONE, intent_id=intent_id,
            action_request_sequence_id=request_sequence,
        )
    if status == "operation_rejected":
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.GATE_REJECTED,
            AttackEvidenceGrade.NONE, intent_id=intent_id,
            action_request_sequence_id=request_sequence,
            receipt_status=status,
        )
    if status != "pending_confirmation":
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.INPUT_FAILED,
            AttackEvidenceGrade.NONE, intent_id=intent_id,
            action_request_sequence_id=request_sequence,
            receipt_status=status,
        )

    online_hint = next((
        payload.get("attempt") for row in rows
        if row.get("record_type") == "attack_attempt"
        and _payload_matches((payload := row.get("payload")), key)
        and isinstance(payload.get("attempt"), Mapping)
    ), None)
    pre_assessment = next((
        payload.get("assessment") for row in rows
        if row.get("record_type") == "combat_assessment"
        and _payload_matches((payload := row.get("payload")), key)
        and payload.get("decision_generation") == skill.get("decision_generation")
        and isinstance(payload.get("assessment"), Mapping)
    ), None)
    revision_event = next((
        payload for row in rows
        if row.get("record_type") == "combat_target_revision"
        and _payload_matches((payload := row.get("payload")), key)
        and payload.get("previous_target_revision") == key.target_revision
        and isinstance(payload.get("new_target_revision"), int)
        and payload["new_target_revision"] > key.target_revision
    ), None)
    if revision_event is not None:
        hint = online_hint if isinstance(online_hint, Mapping) else {}
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.TARGET_REVISED,
            AttackEvidenceGrade.NONE,
            intent_id=intent_id,
            action_request_sequence_id=request_sequence,
            attack_observation_sequence_id=hint.get(
                "attack_observation_sequence_id"
            ),
            confirmation_deadline_ns=hint.get("confirmation_deadline_ns"),
            receipt_status=status,
            pre_attack_hurt_animation_ticks=hint.get(
                "pre_attack_hurt_animation_ticks"
            ),
            pre_attack_health_points=hint.get("pre_attack_health_points"),
        )
    cancel_event = next((
        payload for row in rows
        if row.get("record_type") == "combat_cancel"
        and _payload_matches((payload := row.get("payload")), key)
        and isinstance(payload.get("cancel_reason"), str)
    ), None)

    assessments = [
        payload for row in rows
        if row.get("record_type") == "combat_assessment"
        and _payload_matches((payload := row.get("payload")), key)
        and isinstance(payload.get("attack_observation_sequence_id"), int)
    ]
    attack_sequence = (
        pre_assessment.get("observation_sequence_id")
        if isinstance(pre_assessment, Mapping) else None
    )
    deadline = (
        online_hint.get("confirmation_deadline_ns")
        if isinstance(online_hint, Mapping) else None
    )
    pre_hurt = (
        pre_assessment.get("hurt_animation_ticks")
        if isinstance(pre_assessment, Mapping) else None
    )
    pre_health = (
        pre_assessment.get("target_health_points")
        if isinstance(pre_assessment, Mapping) else None
    )
    latest_sequence = None
    latest_hurt = None
    latest_health = None
    target_damaged = False
    target_dead = any(_target_dead_from_step(row, key) for row in rows)
    timed_out = False
    correlated = False
    if (isinstance(attack_sequence, int) and isinstance(deadline, int)
            and isinstance(pre_hurt, int)):
        for row in rows:
            facts = _target_facts_from_step(row, key)
            if facts is None:
                continue
            sequence, received, hurt, health, dead = facts
            if sequence <= attack_sequence:
                continue
            latest_sequence, latest_hurt, latest_health = sequence, hurt, health
            target_dead = target_dead or dead
            if (received <= deadline and isinstance(hurt, int)
                    and hurt > pre_hurt):
                correlated = True
                break
            if (isinstance(pre_health, (int, float)) and health is not None
                    and health < float(pre_health)):
                target_damaged = True
            if received >= deadline:
                timed_out = True
    for event in assessments:
        assessment = event.get("assessment")
        if not isinstance(assessment, Mapping):
            continue
        event_attack_sequence = event.get("attack_observation_sequence_id")
        event_deadline = event.get("confirmation_deadline_ns")
        event_pre_hurt = event.get("pre_attack_hurt_animation_ticks")
        event_pre_health = event.get("pre_attack_health_points")
        observation_sequence = assessment.get("observation_sequence_id")
        hurt = assessment.get("hurt_animation_ticks")
        health = assessment.get("target_health_points")
        decision_time = event.get("decision_time_ns")
        if not all(isinstance(value, int) for value in (
            event_attack_sequence, event_deadline, event_pre_hurt,
            observation_sequence, decision_time,
        )):
            continue
        attack_sequence = event_attack_sequence
        deadline = event_deadline
        pre_hurt = event_pre_hurt
        pre_health = event_pre_health if isinstance(event_pre_health, (int, float)) else None
        latest_sequence = observation_sequence
        latest_hurt = hurt if isinstance(hurt, int) else None
        latest_health = health if isinstance(health, (int, float)) else None
        later = observation_sequence > attack_sequence
        if (later and decision_time <= deadline and isinstance(hurt, int)
                and hurt > pre_hurt):
            correlated = True
            break
        if (later and pre_health is not None and latest_health is not None
                and latest_health < pre_health):
            target_damaged = True
        if decision_time >= deadline:
            timed_out = True
            break

    common = dict(
        intent_id=intent_id,
        action_request_sequence_id=request_sequence,
        attack_observation_sequence_id=attack_sequence,
        confirmation_deadline_ns=deadline,
        receipt_status=status,
        latest_observation_sequence_id=latest_sequence,
        pre_attack_hurt_animation_ticks=pre_hurt,
        latest_hurt_animation_ticks=latest_hurt,
        pre_attack_health_points=pre_health,
        latest_health_points=latest_health,
        target_damaged_unattributed=target_damaged and not correlated,
        target_dead=target_dead,
    )
    if correlated:
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.COMMAND_CORRELATED_HIT,
            AttackEvidenceGrade.COMMAND_CORRELATED,
            **common,
        )
    if cancel_event is not None:
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.CANCELLED,
            (AttackEvidenceGrade.TARGET_STATE_ONLY
             if target_damaged or target_dead else AttackEvidenceGrade.NONE),
            **common,
        )
    if target_dead:
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.TARGET_DEAD_UNATTRIBUTED,
            AttackEvidenceGrade.TARGET_STATE_ONLY,
            **common,
        )
    if timed_out:
        return AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.CONFIRMATION_TIMEOUT,
            (AttackEvidenceGrade.TARGET_STATE_ONLY
             if target_damaged else AttackEvidenceGrade.NONE),
            **common,
        )
    return AttackAttemptReportV1(
        key, AttackAttemptPhase.TERMINAL,
        AttackAttemptOutcome.OBSERVATION_INTERRUPTED,
        (AttackEvidenceGrade.TARGET_STATE_ONLY
         if target_damaged else AttackEvidenceGrade.NONE),
        **common,
    )
