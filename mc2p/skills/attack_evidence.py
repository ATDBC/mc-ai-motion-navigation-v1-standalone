"""Typed evidence and retry accounting for one bounded melee attack."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from mc2p.contracts.common import (
    ContractViolation,
    require_finite,
    require_identifier,
    require_nonnegative_int,
)


class AttackAttemptPhase(StrEnum):
    PREPARING = "preparing"
    PROPOSED = "proposed"
    DISPATCHED_UNCONFIRMED = "dispatched_unconfirmed"
    TERMINAL = "terminal"


class AttackAttemptOutcome(StrEnum):
    DEFERRED_BY_ARBITRATION = "deferred_by_arbitration"
    GATE_REJECTED = "gate_rejected"
    INPUT_FAILED = "input_failed"
    CONFIRMATION_TIMEOUT = "confirmation_timeout"
    OBSERVATION_INTERRUPTED = "observation_interrupted"
    COMMAND_CORRELATED_HIT = "command_correlated_hit"
    SOURCE_CONFIRMED_HIT = "source_confirmed_hit"
    TARGET_DEAD_UNATTRIBUTED = "target_dead_unattributed"
    TARGET_REVISED = "target_revised"
    CANCELLED = "cancelled"


class AttackEvidenceGrade(StrEnum):
    NONE = "none"
    TARGET_STATE_ONLY = "target_state_only"
    COMMAND_CORRELATED = "command_correlated"
    SOURCE_CONFIRMED = "source_confirmed"


class AttackTaskOutcome(StrEnum):
    GATE_RETRY_EXHAUSTED = "gate_retry_exhausted"
    INPUT_RETRY_EXHAUSTED = "input_retry_exhausted"
    CONFIRMATION_RETRY_EXHAUSTED = "confirmation_retry_exhausted"
    NO_PROGRESS_RETRY_EXHAUSTED = "no_progress_retry_exhausted"


@dataclass(frozen=True, slots=True)
class AttackAttemptKeyV1:
    episode_id: str
    task_id: str
    goal_id: str
    target_revision: int
    track_id: str
    attempt_sequence: int
    schema_version: str = "mc2p.attack-attempt-key.v1"

    def __post_init__(self) -> None:
        for value, name in (
            (self.episode_id, "attack episode"),
            (self.task_id, "attack task"),
            (self.goal_id, "attack goal"),
            (self.track_id, "attack target"),
        ):
            require_identifier(value, name)
        require_nonnegative_int(self.target_revision, "attack target revision")
        require_nonnegative_int(self.attempt_sequence, "attack attempt sequence")
        if self.attempt_sequence == 0:
            raise ContractViolation("attack attempt sequence must be positive")


@dataclass(frozen=True, slots=True)
class AttackAttemptReportV1:
    key: AttackAttemptKeyV1
    phase: AttackAttemptPhase
    outcome: AttackAttemptOutcome | None
    evidence_grade: AttackEvidenceGrade
    intent_id: str | None = None
    action_request_sequence_id: int | None = None
    attack_observation_sequence_id: int | None = None
    attack_world_tick: int | None = None
    pre_attack_damage_event_sequence: int | None = None
    confirmation_deadline_ns: int | None = None
    receipt_status: str | None = None
    latest_observation_sequence_id: int | None = None
    pre_attack_hurt_animation_ticks: int | None = None
    latest_hurt_animation_ticks: int | None = None
    pre_attack_health_points: float | None = None
    latest_health_points: float | None = None
    target_damaged_unattributed: bool = False
    target_dead: bool = False
    source_damage_event_sequence_id: int | None = None
    source_damage_type: str | None = None
    schema_version: str = "mc2p.attack-attempt-report.v1"

    def __post_init__(self) -> None:
        if (type(self.key) is not AttackAttemptKeyV1
                or type(self.phase) is not AttackAttemptPhase
                or self.outcome is not None and type(self.outcome) is not AttackAttemptOutcome
                or type(self.evidence_grade) is not AttackEvidenceGrade):
            raise ContractViolation("attack attempt report has invalid typed fields")
        terminal = self.phase is AttackAttemptPhase.TERMINAL
        if terminal != (self.outcome is not None):
            raise ContractViolation("terminal attack attempt must carry exactly one outcome")
        if self.intent_id is not None:
            require_identifier(self.intent_id, "attack intent")
        for value, name in (
            (self.action_request_sequence_id, "attack action request sequence"),
            (self.attack_observation_sequence_id, "attack observation sequence"),
            (self.attack_world_tick, "attack world tick"),
            (self.pre_attack_damage_event_sequence,
             "pre-attack damage event sequence"),
            (self.confirmation_deadline_ns, "attack confirmation deadline"),
            (self.latest_observation_sequence_id, "attack latest observation sequence"),
            (self.pre_attack_hurt_animation_ticks, "pre-attack hurt animation"),
            (self.latest_hurt_animation_ticks, "latest hurt animation"),
            (self.source_damage_event_sequence_id,
             "source damage event sequence"),
        ):
            if value is not None:
                require_nonnegative_int(value, name)
        for value, name in (
            (self.pre_attack_health_points, "pre-attack health"),
            (self.latest_health_points, "latest target health"),
        ):
            if value is not None:
                require_finite(value, name)
                if value < 0:
                    raise ContractViolation(f"{name} cannot be negative")
        if type(self.target_damaged_unattributed) is not bool or type(self.target_dead) is not bool:
            raise ContractViolation("attack target facts must be booleans")
        if self.receipt_status is not None:
            require_identifier(self.receipt_status, "attack receipt status")
        if self.source_damage_type is not None:
            require_identifier(self.source_damage_type, "source damage type")
        if self.outcome is AttackAttemptOutcome.COMMAND_CORRELATED_HIT:
            if (self.evidence_grade is not AttackEvidenceGrade.COMMAND_CORRELATED
                    or self.intent_id is None
                    or self.action_request_sequence_id is None
                    or self.attack_observation_sequence_id is None
                    or self.attack_world_tick is None
                    or self.pre_attack_damage_event_sequence is None
                    or self.confirmation_deadline_ns is None
                    or self.receipt_status != "pending_confirmation"):
                raise ContractViolation(
                    "command-correlated hit requires command and confirmation evidence"
                )
        elif self.evidence_grade is AttackEvidenceGrade.COMMAND_CORRELATED:
            raise ContractViolation("only a correlated hit may carry correlated evidence")
        if self.outcome is AttackAttemptOutcome.SOURCE_CONFIRMED_HIT:
            if (self.evidence_grade is not AttackEvidenceGrade.SOURCE_CONFIRMED
                    or self.intent_id is None
                    or self.action_request_sequence_id is None
                    or self.attack_observation_sequence_id is None
                    or self.attack_world_tick is None
                    or self.pre_attack_damage_event_sequence is None
                    or self.confirmation_deadline_ns is None
                    or self.receipt_status != "pending_confirmation"
                    or self.source_damage_event_sequence_id is None
                    or self.source_damage_type is None):
                raise ContractViolation(
                    "source-confirmed hit requires command and packet evidence"
                )
        elif self.evidence_grade is AttackEvidenceGrade.SOURCE_CONFIRMED:
            raise ContractViolation(
                "only a source-confirmed hit may carry source evidence"
            )
        if (self.outcome is AttackAttemptOutcome.TARGET_DEAD_UNATTRIBUTED
                and not self.target_dead):
            raise ContractViolation("unattributed target death requires a death fact")

    @property
    def terminal(self) -> bool:
        return self.phase is AttackAttemptPhase.TERMINAL


@dataclass(frozen=True, slots=True)
class AttackRetryLedgerV1:
    gate_rejections_total: int = 0
    input_failures_total: int = 0
    confirmation_timeouts_total: int = 0
    unattributed_target_damage_total: int = 0
    confirmed_hits_total: int = 0
    consecutive_gate_rejections: int = 0
    consecutive_input_failures: int = 0
    consecutive_confirmation_timeouts: int = 0
    failures_since_confirmed_hit: int = 0
    schema_version: str = "mc2p.attack-retry-ledger.v1"

    def __post_init__(self) -> None:
        for name in (
            "gate_rejections_total", "input_failures_total",
            "confirmation_timeouts_total", "unattributed_target_damage_total",
            "confirmed_hits_total", "consecutive_gate_rejections",
            "consecutive_input_failures", "consecutive_confirmation_timeouts",
            "failures_since_confirmed_hit",
        ):
            require_nonnegative_int(getattr(self, name), name.replace("_", " "))


def advance_attack_retry(
    ledger: AttackRetryLedgerV1,
    attempt: AttackAttemptReportV1,
    *,
    gate_limit: int = 2,
    input_limit: int = 2,
    confirmation_limit: int = 2,
    no_progress_limit: int = 6,
) -> tuple[AttackRetryLedgerV1, AttackTaskOutcome | None]:
    """Apply one terminal attempt without mixing independent failure classes."""
    if type(ledger) is not AttackRetryLedgerV1 or type(attempt) is not AttackAttemptReportV1:
        raise ContractViolation("attack retry requires typed ledger and attempt")
    if not attempt.terminal:
        raise ContractViolation("attack retry only accepts a terminal attempt")
    for value, name in (
        (gate_limit, "gate retry limit"),
        (input_limit, "input retry limit"),
        (confirmation_limit, "confirmation retry limit"),
        (no_progress_limit, "attack no-progress limit"),
    ):
        require_nonnegative_int(value, name)
        if value == 0:
            raise ContractViolation(f"{name} must be positive")

    if attempt.outcome is AttackAttemptOutcome.DEFERRED_BY_ARBITRATION:
        return ledger, None

    diagnostic = int(attempt.target_damaged_unattributed)
    next_ledger = replace(
        ledger,
        unattributed_target_damage_total=(
            ledger.unattributed_target_damage_total + diagnostic
        ),
    )
    task_outcome = None
    if attempt.outcome is AttackAttemptOutcome.GATE_REJECTED:
        streak = ledger.consecutive_gate_rejections + 1
        next_ledger = replace(
            next_ledger,
            gate_rejections_total=ledger.gate_rejections_total + 1,
            consecutive_gate_rejections=streak,
            consecutive_input_failures=0,
            consecutive_confirmation_timeouts=0,
            failures_since_confirmed_hit=ledger.failures_since_confirmed_hit + 1,
        )
        if streak >= gate_limit:
            task_outcome = AttackTaskOutcome.GATE_RETRY_EXHAUSTED
    elif attempt.outcome is AttackAttemptOutcome.INPUT_FAILED:
        streak = ledger.consecutive_input_failures + 1
        next_ledger = replace(
            next_ledger,
            input_failures_total=ledger.input_failures_total + 1,
            consecutive_gate_rejections=0,
            consecutive_input_failures=streak,
            consecutive_confirmation_timeouts=0,
            failures_since_confirmed_hit=ledger.failures_since_confirmed_hit + 1,
        )
        if streak >= input_limit:
            task_outcome = AttackTaskOutcome.INPUT_RETRY_EXHAUSTED
    elif attempt.outcome is AttackAttemptOutcome.CONFIRMATION_TIMEOUT:
        streak = ledger.consecutive_confirmation_timeouts + 1
        next_ledger = replace(
            next_ledger,
            confirmation_timeouts_total=ledger.confirmation_timeouts_total + 1,
            consecutive_gate_rejections=0,
            consecutive_input_failures=0,
            consecutive_confirmation_timeouts=streak,
            failures_since_confirmed_hit=ledger.failures_since_confirmed_hit + 1,
        )
        if streak >= confirmation_limit:
            task_outcome = AttackTaskOutcome.CONFIRMATION_RETRY_EXHAUSTED
    elif attempt.outcome in {
        AttackAttemptOutcome.COMMAND_CORRELATED_HIT,
        AttackAttemptOutcome.SOURCE_CONFIRMED_HIT,
    }:
        next_ledger = replace(
            next_ledger,
            confirmed_hits_total=ledger.confirmed_hits_total + 1,
            consecutive_gate_rejections=0,
            consecutive_input_failures=0,
            consecutive_confirmation_timeouts=0,
            failures_since_confirmed_hit=0,
        )
    if (task_outcome is None
            and next_ledger.failures_since_confirmed_hit >= no_progress_limit):
        task_outcome = AttackTaskOutcome.NO_PROGRESS_RETRY_EXHAUSTED
    return next_ledger, task_outcome
