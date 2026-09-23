"""B10-A online motion anchors, applied-input ledger and command projection."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math

from mc2p.contracts.action_receipt import (
    ClientBehaviorReceipt, ClientBehaviorReceiptV2, ClientBehaviorReceiptV3,
    ClientInputApplicationV1,
)
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.motion_nav.physics_types import PhysicsRuleset, PhysicsState, TickInput
from mc2p.motion_nav.world_model import WorldSessionId


class InputApplicationStatus(StrEnum):
    IN_FLIGHT = "in_flight"
    PARTIALLY_APPLIED = "partially_applied"
    APPLIED = "applied"
    APPLIED_OUTSIDE_WINDOW = "applied_outside_window"
    EXPIRED = "expired"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


_TERMINAL_INPUT_STATES = frozenset({
    InputApplicationStatus.APPLIED,
    InputApplicationStatus.APPLIED_OUTSIDE_WINDOW,
    InputApplicationStatus.EXPIRED,
    InputApplicationStatus.REJECTED,
    InputApplicationStatus.AMBIGUOUS,
})


@dataclass(frozen=True, slots=True)
class InputApplicationRecord:
    session: WorldSessionId
    control_sequence: int
    action: ActionSnapshotV1
    requested_first_tick: int
    requested_last_tick: int
    latest_allowed_first_tick: int
    status: InputApplicationStatus
    applied_ticks: tuple[int, ...] = ()
    samples: tuple[ClientInputApplicationV1, ...] = ()
    resolved_at_tick: int | None = None

    def __post_init__(self) -> None:
        if type(self.session) is not WorldSessionId or type(self.action) is not ActionSnapshotV1:
            raise ContractViolation("input record requires a session and action")
        for name in (
            "control_sequence", "requested_first_tick", "requested_last_tick",
            "latest_allowed_first_tick",
        ):
            require_nonnegative_int(getattr(self, name), name)
        if self.control_sequence != self.action.request_sequence_id:
            raise ContractViolation("input record control identity mismatch")
        if self.requested_last_tick < self.requested_first_tick:
            raise ContractViolation("input application window is reversed")
        if self.latest_allowed_first_tick < self.requested_first_tick:
            raise ContractViolation("input start window is reversed")
        if type(self.status) is not InputApplicationStatus:
            raise ContractViolation("invalid input application status")
        if self.applied_ticks != tuple(sorted(set(self.applied_ticks))):
            raise ContractViolation("applied ticks must be sorted and unique")
        if len(self.applied_ticks) != len(self.samples):
            raise ContractViolation("applied tick and sample records disagree")


class InputApplicationLedger:
    """Bounded application history maintained by the eventual MotorGateway."""

    def __init__(self, *, max_records: int = 64) -> None:
        if type(max_records) is not int or max_records < 1:
            raise ContractViolation("input ledger limit must be positive")
        self._max_records = max_records
        self._records: dict[int, InputApplicationRecord] = {}
        self._observed_samples: dict[int, ClientInputApplicationV1] = {}
        self._max_samples = max_records * 20 + 1
        self._last_input_samples: int | None = None
        self._last_dropped_input_samples: int | None = None
        self._latest_movement_tick_id: int | None = None

    def submit(self, session: WorldSessionId, action: ActionSnapshotV1, *,
               requested_first_tick: int,
               latest_allowed_first_tick: int | None = None) -> InputApplicationRecord:
        if type(session) is not WorldSessionId or type(action) is not ActionSnapshotV1:
            raise ContractViolation("input submission requires a session and action")
        require_nonnegative_int(requested_first_tick, "requested first tick")
        if latest_allowed_first_tick is None:
            latest_allowed_first_tick = requested_first_tick
        require_nonnegative_int(
            latest_allowed_first_tick, "latest allowed first tick",
        )
        if latest_allowed_first_tick < requested_first_tick:
            raise ContractViolation("input start window is reversed")
        if (latest_allowed_first_tick != requested_first_tick
                and action.valid_for_ticks != 1):
            raise ContractViolation(
                "a widened start window currently supports only one-tick commands"
            )
        sequence = action.request_sequence_id
        if sequence in self._records:
            raise ContractViolation("duplicate control sequence")
        self._make_room()
        record = InputApplicationRecord(
            session, sequence, action, requested_first_tick,
            requested_first_tick + action.valid_for_ticks - 1,
            latest_allowed_first_tick,
            InputApplicationStatus.IN_FLIGHT,
        )
        self._records[sequence] = record
        return record

    @property
    def latest_movement_tick_id(self) -> int | None:
        return self._latest_movement_tick_id

    def observe_sample(self, sample: ClientInputApplicationV1) -> InputApplicationRecord | None:
        if type(sample) is not ClientInputApplicationV1:
            raise ContractViolation("input ledger requires a formal input sample")
        existing = self._observed_samples.get(sample.movement_tick_id)
        if existing is not None:
            if existing != sample:
                raise ContractViolation("conflicting evidence for one movement tick")
            if sample.request_sequence_id is None:
                return None
            return self._records.get(sample.request_sequence_id)
        if (self._latest_movement_tick_id is not None
                and sample.movement_tick_id < self._latest_movement_tick_id):
            raise ContractViolation("movement tick sample regressed")
        if len(self._observed_samples) >= self._max_samples:
            self._observed_samples.pop(min(self._observed_samples))
        self._observed_samples[sample.movement_tick_id] = sample
        self._latest_movement_tick_id = sample.movement_tick_id
        if sample.request_sequence_id is None:
            return None
        record = self._records.get(sample.request_sequence_id)
        if record is None or record.action.episode_id != sample.episode_id:
            raise ContractViolation("input sample has no submitted command")
        if sample.state == "disallowed":
            return self.reject(record.control_sequence, at_tick=sample.movement_tick_id)
        if sample.state in {"expired", "lease_exhausted"}:
            return self.expire(record.control_sequence, at_tick=sample.movement_tick_id)
        if sample.state not in {"leased", "neutral"}:
            return record
        ticks = tuple(sorted((*record.applied_ticks, sample.movement_tick_id)))
        samples_by_tick = {item.movement_tick_id: item for item in record.samples}
        samples_by_tick[sample.movement_tick_id] = sample
        samples = tuple(samples_by_tick[tick] for tick in ticks)
        if record.action.valid_for_ticks == 1:
            in_window = (
                len(ticks) == 1
                and record.requested_first_tick <= ticks[0]
                <= record.latest_allowed_first_tick
            )
            fully_applied = in_window
        else:
            expected_ticks = tuple(range(
                record.requested_first_tick, record.requested_last_tick + 1,
            ))
            in_window = all(tick in expected_ticks for tick in ticks)
            fully_applied = ticks == expected_ticks
        status = (InputApplicationStatus.APPLIED_OUTSIDE_WINDOW if not in_window
                  else InputApplicationStatus.APPLIED
                  if fully_applied
                  else InputApplicationStatus.PARTIALLY_APPLIED)
        updated = replace(
            record, status=status, applied_ticks=ticks, samples=samples,
            resolved_at_tick=max(ticks),
        )
        self._records[record.control_sequence] = updated
        return updated

    def observe_receipt(self, receipt: ClientBehaviorReceipt) -> tuple[InputApplicationRecord, ...]:
        if type(receipt) not in (ClientBehaviorReceiptV2, ClientBehaviorReceiptV3):
            raise ContractViolation("input ledger requires a formal behavior receipt")
        if (self._last_input_samples is not None
                and receipt.input_samples < self._last_input_samples):
            raise ContractViolation("input sample counter regressed")
        if (type(receipt) is ClientBehaviorReceiptV3
                and self._last_dropped_input_samples is not None
                and receipt.dropped_input_samples < self._last_dropped_input_samples):
            raise ContractViolation("dropped input sample counter regressed")
        changed: list[InputApplicationRecord] = []
        if type(receipt) is ClientBehaviorReceiptV3:
            for application in receipt.input_applications:
                record = self.observe_sample(application)
                if record is not None:
                    changed.append(record)
            if self._last_input_samples is not None:
                expected_ticks = set(range(
                    self._last_input_samples + 1, receipt.input_samples + 1,
                ))
                observed_ticks = {
                    application.movement_tick_id
                    for application in receipt.input_applications
                }
                if not expected_ticks.issubset(observed_ticks):
                    for record in tuple(self._records.values()):
                        if record.status in {
                            InputApplicationStatus.IN_FLIGHT,
                            InputApplicationStatus.PARTIALLY_APPLIED,
                        }:
                            changed.append(self.mark_ambiguous(
                                record.control_sequence,
                                at_tick=receipt.input_samples,
                            ))
        elif (self._last_input_samples is not None
              and receipt.input_samples > self._last_input_samples):
            for record in tuple(self._records.values()):
                if record.status in {
                    InputApplicationStatus.IN_FLIGHT,
                    InputApplicationStatus.PARTIALLY_APPLIED,
                }:
                    changed.append(self.mark_ambiguous(
                        record.control_sequence, at_tick=receipt.input_samples))
        if type(receipt) is ClientBehaviorReceiptV3:
            previous_dropped = self._last_dropped_input_samples or 0
            if receipt.dropped_input_samples > previous_dropped:
                for record in tuple(self._records.values()):
                    if record.status in {
                        InputApplicationStatus.IN_FLIGHT,
                        InputApplicationStatus.PARTIALLY_APPLIED,
                    }:
                        changed.append(self.mark_ambiguous(
                            record.control_sequence, at_tick=receipt.input_samples,
                        ))
            self._last_dropped_input_samples = receipt.dropped_input_samples
        self._last_input_samples = receipt.input_samples
        sequence = receipt.request_sequence_id
        if sequence is not None and sequence in self._records:
            record = self._records[sequence]
            if receipt.status in {"rejected", "cancelled"}:
                changed.append(self.reject(sequence, at_tick=receipt.input_samples))
            elif receipt.status == "timed_out":
                changed.append(self.expire(sequence, at_tick=receipt.input_samples))
        return tuple(changed)

    def expire(self, control_sequence: int, *, at_tick: int) -> InputApplicationRecord:
        return self._resolve(control_sequence, InputApplicationStatus.EXPIRED, at_tick)

    def reject(self, control_sequence: int, *, at_tick: int) -> InputApplicationRecord:
        return self._resolve(control_sequence, InputApplicationStatus.REJECTED, at_tick)

    def mark_ambiguous(self, control_sequence: int, *, at_tick: int) -> InputApplicationRecord:
        return self._resolve(control_sequence, InputApplicationStatus.AMBIGUOUS, at_tick)

    def snapshot(self) -> tuple[InputApplicationRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def _resolve(self, sequence: int, status: InputApplicationStatus,
                 tick: int) -> InputApplicationRecord:
        require_nonnegative_int(sequence, "control sequence")
        require_nonnegative_int(tick, "resolution tick")
        record = self._records.get(sequence)
        if record is None:
            raise ContractViolation("unknown control sequence")
        if record.status not in {
                InputApplicationStatus.IN_FLIGHT,
                InputApplicationStatus.PARTIALLY_APPLIED}:
            return record
        updated = replace(record, status=status, resolved_at_tick=tick)
        self._records[sequence] = updated
        return updated

    def _make_room(self) -> None:
        if len(self._records) < self._max_records:
            return
        evictable = next((key for key, record in self._records.items()
                          if record.status in _TERMINAL_INPUT_STATES), None)
        if evictable is None:
            raise ContractViolation("input ledger is full of unresolved records")
        self._records.pop(evictable)


class MotionTickPhase(StrEnum):
    BEFORE_INPUT_SAMPLE = "before_input_sample"
    AFTER_INPUT_BEFORE_MOVEMENT = "after_input_before_movement"
    AFTER_MOVEMENT = "after_movement"


class AnchorBuildStatus(StrEnum):
    READY = "ready"
    NEEDS_INPUT_CONFIRMATION = "needs_input_confirmation"
    DUPLICATE_OBSERVATION = "duplicate_observation"
    INVALID_INPUT = "invalid_input"


@dataclass(frozen=True, slots=True)
class StateAnchor:
    session: WorldSessionId
    observation_sequence_id: int
    movement_tick_id: int
    phase: MotionTickPhase
    confirmed_control_sequence: int | None
    confirmed_control_tick_range: tuple[int, int] | None
    ruleset_id: str
    state_schema: str
    input_projection_version: str
    physics_state: PhysicsState


@dataclass(frozen=True, slots=True)
class AnchorBuildResult:
    status: AnchorBuildStatus
    anchor: StateAnchor | None = None
    unresolved_control_sequences: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()


class StateAnchorBuilder:
    def __init__(self, *, ruleset: PhysicsRuleset, input_projection_version: str) -> None:
        if type(ruleset) is not PhysicsRuleset:
            raise ContractViolation("state anchor requires a physics ruleset")
        require_identifier(input_projection_version, "input projection version")
        self._ruleset = ruleset
        self._projection = input_projection_version
        self._latest_accepted: dict[WorldSessionId, int] = {}

    def build(self, *, session: WorldSessionId, observation_sequence_id: int,
              movement_tick_id: int, phase: MotionTickPhase,
              physics_state: PhysicsState,
              ledger: InputApplicationLedger) -> AnchorBuildResult:
        if (type(session) is not WorldSessionId or type(physics_state) is not PhysicsState
                or type(ledger) is not InputApplicationLedger or type(phase) is not MotionTickPhase):
            raise ContractViolation("invalid state anchor inputs")
        require_nonnegative_int(observation_sequence_id, "observation sequence")
        require_nonnegative_int(movement_tick_id, "movement tick id")
        if observation_sequence_id <= self._latest_accepted.get(session, -1):
            return AnchorBuildResult(AnchorBuildStatus.DUPLICATE_OBSERVATION,
                                     reasons=("observation_did_not_advance",))
        if (physics_state.session != session
                or physics_state.ruleset_id != self._ruleset.ruleset_id
                or physics_state.state_schema != self._ruleset.state_schema):
            return AnchorBuildResult(AnchorBuildStatus.INVALID_INPUT,
                                     reasons=("state_identity_mismatch",))
        relevant = []
        for record in ledger.snapshot():
            if record.session != session or record.requested_first_tick > movement_tick_id:
                continue
            if record.status is InputApplicationStatus.AMBIGUOUS:
                relevant.append(record.control_sequence)
                continue
            if record.status in {
                    InputApplicationStatus.IN_FLIGHT,
                    InputApplicationStatus.PARTIALLY_APPLIED}:
                expected = set(range(
                    record.requested_first_tick,
                    min(record.requested_last_tick, movement_tick_id) + 1,
                ))
                if not expected.issubset(record.applied_ticks):
                    relevant.append(record.control_sequence)
        relevant = tuple(relevant)
        if relevant:
            return AnchorBuildResult(
                AnchorBuildStatus.NEEDS_INPUT_CONFIRMATION,
                unresolved_control_sequences=relevant,
            )
        confirmed = tuple(record for record in ledger.snapshot()
                          if record.session == session
                          and record.status in {
                              InputApplicationStatus.PARTIALLY_APPLIED,
                              InputApplicationStatus.APPLIED,
                              InputApplicationStatus.APPLIED_OUTSIDE_WINDOW,
                          }
                          and record.applied_ticks
                          and max(record.applied_ticks) <= movement_tick_id)
        latest = max(confirmed, key=lambda record: (
            max(record.applied_ticks), record.control_sequence,
        )) if confirmed else None
        state_at_tick = replace(physics_state, movement_tick_id=movement_tick_id)
        anchor = StateAnchor(
            session, observation_sequence_id, movement_tick_id, phase,
            latest.control_sequence if latest is not None else None,
            (min(latest.applied_ticks), max(latest.applied_ticks))
            if latest is not None else None,
            self._ruleset.ruleset_id,
            self._ruleset.state_schema, self._projection, state_at_tick,
        )
        self._latest_accepted[session] = observation_sequence_id
        return AnchorBuildResult(AnchorBuildStatus.READY, anchor=anchor)


class ProjectionStatus(StrEnum):
    READY = "ready"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    status: ProjectionStatus
    tick_input: TickInput | None = None
    reasons: tuple[str, ...] = ()


def project_movement_command(state: PhysicsState, command: MovementV1, *,
                             movement_yaw_radians: float | None = None) -> ProjectionResult:
    if type(state) is not PhysicsState or type(command) is not MovementV1:
        raise ContractViolation("input projection requires physics state and MovementV1")
    yaw = state.yaw_radians if movement_yaw_radians is None else movement_yaw_radians
    if type(yaw) not in (int, float) or not math.isfinite(float(yaw)):
        raise ContractViolation("movement yaw must be finite")
    if state.is_using_item:
        return ProjectionResult(
            ProjectionStatus.UNSUPPORTED,
            reasons=("item_slowdown_not_supported",),
        )
    return ProjectionResult(
        ProjectionStatus.READY,
        TickInput(float(command.forward), float(command.strafe), command.jump,
                  command.sneak, command.sprint, float(yaw)),
    )


@dataclass(frozen=True, slots=True)
class PredictionValidity:
    anchor_movement_tick: int
    valid_through_tick: int
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_nonnegative_int(self.anchor_movement_tick, "anchor movement tick")
        require_nonnegative_int(self.valid_through_tick, "valid through tick")
        if self.valid_through_tick < self.anchor_movement_tick:
            raise ContractViolation("prediction validity ends before its anchor")

    def covers(self, movement_tick: int) -> bool:
        require_nonnegative_int(movement_tick, "movement tick")
        return self.anchor_movement_tick <= movement_tick <= self.valid_through_tick


@dataclass(frozen=True, slots=True)
class CandidateExecutionWindow:
    earliest_start_tick: int
    latest_start_tick: int

    def __post_init__(self) -> None:
        require_nonnegative_int(self.earliest_start_tick, "earliest start tick")
        require_nonnegative_int(self.latest_start_tick, "latest start tick")
        if self.latest_start_tick < self.earliest_start_tick:
            raise ContractViolation("candidate execution window is reversed")

    def allows_start(self, movement_tick: int) -> bool:
        require_nonnegative_int(movement_tick, "movement tick")
        return self.earliest_start_tick <= movement_tick <= self.latest_start_tick
