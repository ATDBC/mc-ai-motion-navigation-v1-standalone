"""B10-C task binding, admission and receipt-driven execution for verified motion."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import (
    ContractViolation, require_identifier, require_nonnegative_int,
)
from mc2p.motion_nav.motion_solver import VerifiedMotionResult
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputApplicationStatus, StateAnchor,
)
from mc2p.motion_nav.physics_types import PhysicsState
from mc2p.motion_nav.world_model import BlockPos


_ENTRY_POSITION_TOLERANCE = 0.05
_ENTRY_VELOCITY_TOLERANCE_PER_TICK = 0.01
_ENTRY_YAW_TOLERANCE_RADIANS = math.radians(1.0)


@dataclass(frozen=True, slots=True)
class MotionCandidateContext:
    planning_request_id: str
    planning_generation: int
    goal_id: str
    goal_revision: int
    route_id: str
    route_revision: int
    action_index: int
    candidate_revision: int
    risk_policy_id: str
    accepted_resource_incomplete_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("planning_request_id", "goal_id", "route_id",
                     "risk_policy_id"):
            require_identifier(getattr(self, name), name.replace("_", " "))
        for name in ("planning_generation", "goal_revision", "route_revision",
                     "action_index", "candidate_revision"):
            require_nonnegative_int(getattr(self, name), name.replace("_", " "))
        if (type(self.accepted_resource_incomplete_reasons) is not tuple
                or self.accepted_resource_incomplete_reasons
                   != tuple(sorted(set(self.accepted_resource_incomplete_reasons)))
                or any(type(reason) is not str or not reason
                       for reason in self.accepted_resource_incomplete_reasons)):
            raise ContractViolation("resource assumptions must be sorted and unique")


@dataclass(frozen=True, slots=True)
class VerifiedMotionCandidate:
    proof: VerifiedMotionResult
    context: MotionCandidateContext

    def __post_init__(self) -> None:
        if (type(self.proof) is not VerifiedMotionResult
                or type(self.context) is not MotionCandidateContext):
            raise ContractViolation("verified candidate requires proof and task context")


class MotionCandidateStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AdmittedMotionCandidate:
    candidate: VerifiedMotionCandidate
    admitted_observation_sequence_id: int
    admitted_movement_tick_id: int
    intended_start_tick: int

    def __post_init__(self) -> None:
        if type(self.candidate) is not VerifiedMotionCandidate:
            raise ContractViolation("admitted motion requires a verified candidate")
        for name in ("admitted_observation_sequence_id",
                     "admitted_movement_tick_id", "intended_start_tick"):
            require_nonnegative_int(getattr(self, name), name.replace("_", " "))

    @property
    def proof(self) -> VerifiedMotionResult:
        return self.candidate.proof

    @property
    def context(self) -> MotionCandidateContext:
        return self.candidate.context


@dataclass(frozen=True, slots=True)
class MotionCandidateAdmission:
    status: MotionCandidateStatus
    reason: str
    candidate: AdmittedMotionCandidate | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not MotionCandidateStatus:
            raise ContractViolation("invalid motion candidate admission status")
        if (self.status is MotionCandidateStatus.ACCEPTED) != (
                type(self.candidate) is AdmittedMotionCandidate):
            raise ContractViolation("accepted admission must carry its candidate")


def _angle_error(first: float, second: float) -> float:
    return abs((first - second + math.pi) % (2.0 * math.pi) - math.pi)


def _state_fits_entry(actual: PhysicsState, expected: PhysicsState) -> bool:
    if (actual.session != expected.session
            or actual.ruleset_id != expected.ruleset_id
            or actual.state_schema != expected.state_schema
            or actual.pose != expected.pose
            or actual.on_ground != expected.on_ground
            or actual.swimming != expected.swimming
            or actual.climbing != expected.climbing
            or actual.fall_flying != expected.fall_flying
            or actual.flying != expected.flying
            or actual.is_using_item != expected.is_using_item
            or actual.food_points < expected.food_points):
        return False
    if max(abs(a - b) for a, b in zip(actual.position, expected.position)) \
            > _ENTRY_POSITION_TOLERANCE:
        return False
    if max(abs(a - b) for a, b in zip(
            actual.velocity_blocks_per_tick,
            expected.velocity_blocks_per_tick,
    )) > _ENTRY_VELOCITY_TOLERANCE_PER_TICK:
        return False
    return _angle_error(actual.yaw_radians, expected.yaw_radians) \
        <= _ENTRY_YAW_TOLERANCE_RADIANS


class MotionCandidateAdmitter:
    """Check whether one task-bound proof still has authority to start now."""

    def admit(
            self, candidate: VerifiedMotionCandidate, anchor: StateAnchor, *,
            planning_request_id: str, planning_generation: int,
            goal_id: str, goal_revision: int,
            route_id: str, route_revision: int, action_index: int,
            candidate_revision: int, risk_policy_id: str,
            intended_start_tick: int,
            changed_cells: tuple[BlockPos, ...],
    ) -> MotionCandidateAdmission:
        if (type(candidate) is not VerifiedMotionCandidate
                or type(anchor) is not StateAnchor
                or type(changed_cells) is not tuple):
            raise ContractViolation("motion candidate admission requires typed inputs")
        require_nonnegative_int(intended_start_tick, "intended start tick")
        current = MotionCandidateContext(
            planning_request_id, planning_generation, goal_id, goal_revision,
            route_id, route_revision, action_index, candidate_revision,
            risk_policy_id, candidate.context.accepted_resource_incomplete_reasons,
        )
        expected = candidate.context
        if (current.planning_request_id != expected.planning_request_id
                or current.planning_generation != expected.planning_generation):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "planning_request_replaced",
            )
        if (current.goal_id != expected.goal_id
                or current.goal_revision != expected.goal_revision):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "goal_replaced",
            )
        if (current.route_id != expected.route_id
                or current.route_revision != expected.route_revision
                or current.action_index != expected.action_index):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "route_replaced",
            )
        if current.candidate_revision != expected.candidate_revision:
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "candidate_replaced",
            )
        if current.risk_policy_id != expected.risk_policy_id:
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "risk_policy_changed",
            )
        proof = candidate.proof
        if (anchor.session != proof.entry_state.session
                or anchor.ruleset_id != proof.ruleset_id
                or anchor.state_schema != proof.entry_state.state_schema
                or anchor.input_projection_version
                   != proof.input_projection_version):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "state_identity_changed",
            )
        if not proof.execution_window.allows_start(intended_start_tick):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "execution_window_expired",
            )
        if (anchor.observation_sequence_id
                != proof.anchor_observation_sequence_id
                or anchor.movement_tick_id != proof.anchor_movement_tick_id
                or intended_start_tick != anchor.movement_tick_id + 1):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "state_anchor_advanced",
            )
        if set(changed_cells).intersection(proof.world_dependencies):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "world_dependency_changed",
            )
        if not set(proof.resource_incomplete_reasons).issubset(
                expected.accepted_resource_incomplete_reasons):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "resource_evidence_incomplete",
            )
        if not _state_fits_entry(anchor.physics_state, proof.entry_state):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "entry_state_changed",
            )
        return MotionCandidateAdmission(
            MotionCandidateStatus.ACCEPTED, "accepted",
            AdmittedMotionCandidate(
                candidate, anchor.observation_sequence_id,
                anchor.movement_tick_id, intended_start_tick,
            ),
        )


class VerifiedMotionExecutorState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    RECOVERING = "recovering"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INPUT_LOST = "input_lost"


@dataclass(frozen=True, slots=True)
class VerifiedMotionDecision:
    state: VerifiedMotionExecutorState
    movement: MovementV1 | None
    movement_yaw_radians: float | None
    command_index: int
    expected_movement_tick: int | None
    latest_movement_tick: int | None
    input_lease_ticks: int
    reason: str


@dataclass(frozen=True, slots=True)
class _PendingSubmission:
    command_index: int
    control_sequence: int
    requested_movement_tick: int
    requested_latest_movement_tick: int


class VerifiedMotionExecutor:
    """Execute one admitted proof and advance only from exact client receipts."""

    def __init__(self) -> None:
        self.state = VerifiedMotionExecutorState.IDLE
        self._candidate: AdmittedMotionCandidate | None = None
        self._start_tick: int | None = None
        self._command_index = 0
        self._pending: _PendingSubmission | None = None
        self._cancel_requested = False
        self._recovery_uses_verified_remainder = False
        self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST

    def start(self, candidate: AdmittedMotionCandidate) -> None:
        if type(candidate) is not AdmittedMotionCandidate:
            raise ContractViolation("verified executor requires an admitted candidate")
        intended_start_tick = candidate.intended_start_tick
        if not candidate.proof.execution_window.allows_start(intended_start_tick):
            raise ContractViolation("verified candidate start is outside its window")
        if self.state in {
                VerifiedMotionExecutorState.RUNNING,
                VerifiedMotionExecutorState.RECOVERING}:
            raise ContractViolation("verified executor is already active")
        self._candidate = candidate
        self._start_tick = intended_start_tick
        self._command_index = 0
        self._pending = None
        self._cancel_requested = False
        self._recovery_uses_verified_remainder = False
        self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST
        self.state = VerifiedMotionExecutorState.RUNNING

    def register_submission(self, command_index: int, *, control_sequence: int,
                            requested_movement_tick: int,
                            requested_latest_movement_tick: int | None = None) -> None:
        if self.state not in {
                VerifiedMotionExecutorState.RUNNING,
                VerifiedMotionExecutorState.RECOVERING}:
            raise ContractViolation("verified executor is not accepting submissions")
        require_nonnegative_int(command_index, "command index")
        require_nonnegative_int(control_sequence, "control sequence")
        require_nonnegative_int(requested_movement_tick, "requested movement tick")
        if requested_latest_movement_tick is None:
            requested_latest_movement_tick = self._latest_tick()
        require_nonnegative_int(
            requested_latest_movement_tick, "requested latest movement tick",
        )
        if self._pending is not None:
            raise ContractViolation("verified executor already has an in-flight command")
        if command_index != self._command_index:
            raise ContractViolation("submitted command is not the current proof command")
        expected_tick = self._expected_tick()
        if requested_movement_tick != expected_tick:
            raise ContractViolation("submitted command targets another movement tick")
        if requested_latest_movement_tick != self._latest_tick():
            raise ContractViolation("submitted command uses another movement window")
        self._pending = _PendingSubmission(
            command_index, control_sequence, requested_movement_tick,
            requested_latest_movement_tick,
        )

    def cancel(self, anchor: StateAnchor) -> None:
        if type(anchor) is not StateAnchor:
            raise ContractViolation("verified cancellation requires a state anchor")
        if self.state is not VerifiedMotionExecutorState.RUNNING:
            return
        self._cancel_requested = True
        if anchor.physics_state.on_ground:
            self.state = VerifiedMotionExecutorState.CANCELLED
        else:
            self.state = VerifiedMotionExecutorState.RECOVERING
            self._recovery_uses_verified_remainder = True
            self._terminal_after_recovery = VerifiedMotionExecutorState.CANCELLED

    def _expected_tick(self) -> int:
        assert self._start_tick is not None
        return self._start_tick + self._command_index

    def _latest_tick(self) -> int:
        assert self._candidate is not None
        if self._command_index == 0:
            return self._candidate.proof.execution_window.latest_start_tick
        return self._expected_tick()

    def _decision(self, movement: MovementV1 | None,
                  yaw: float | None, reason: str) -> VerifiedMotionDecision:
        expected = (self._expected_tick()
                    if self.state in {
                        VerifiedMotionExecutorState.RUNNING,
                        VerifiedMotionExecutorState.RECOVERING,
                    } else None)
        latest = (self._latest_tick()
                  if self.state in {
                      VerifiedMotionExecutorState.RUNNING,
                      VerifiedMotionExecutorState.RECOVERING,
                  } else None)
        return VerifiedMotionDecision(
            self.state, movement, yaw, self._command_index, expected, latest,
            1 if movement is not None else 0, reason,
        )

    def _consume_pending(self, ledger: InputApplicationLedger) -> str | None:
        assert self._candidate is not None
        if self._pending is None:
            return None
        records = tuple(record for record in ledger.snapshot()
                        if record.control_sequence == self._pending.control_sequence)
        if not records:
            return "awaiting_application"
        record = records[-1]
        expected_command = self._candidate.proof.commands[
            self._pending.command_index
        ].movement
        if record.action.movement != expected_command:
            self.state = VerifiedMotionExecutorState.INPUT_LOST
            return "applied_command_changed"
        if record.status is InputApplicationStatus.APPLIED_OUTSIDE_WINDOW:
            self.state = VerifiedMotionExecutorState.INPUT_LOST
            return "input_applied_outside_window"
        if record.status in {
                InputApplicationStatus.EXPIRED,
                InputApplicationStatus.REJECTED,
                InputApplicationStatus.AMBIGUOUS}:
            self.state = VerifiedMotionExecutorState.INPUT_LOST
            return f"input_{record.status.value}"
        if record.status is not InputApplicationStatus.APPLIED:
            return "awaiting_application"
        if (len(record.applied_ticks) != 1
                or not (self._pending.requested_movement_tick
                        <= record.applied_ticks[0]
                        <= self._pending.requested_latest_movement_tick)):
            self.state = VerifiedMotionExecutorState.INPUT_LOST
            return "input_tick_changed"
        if self._pending.command_index == 0:
            # The solver proves a bounded start window.  Once the first input
            # is observed, all remaining commands are anchored to that real
            # player movement tick and must continue without gaps.
            self._start_tick = record.applied_ticks[0]
        self._command_index += 1
        self._pending = None
        return None

    def decide(self, anchor: StateAnchor,
               ledger: InputApplicationLedger) -> VerifiedMotionDecision:
        if type(anchor) is not StateAnchor or type(ledger) is not InputApplicationLedger:
            raise ContractViolation("verified decision requires anchor and input ledger")
        if self._candidate is None:
            self.state = VerifiedMotionExecutorState.IDLE
            return self._decision(None, None, "not_started")
        if self.state is VerifiedMotionExecutorState.RECOVERING:
            if anchor.physics_state.on_ground:
                self.state = self._terminal_after_recovery
                return self._decision(MovementV1(), None, self.state.value)
            if self._recovery_uses_verified_remainder:
                pending = self._consume_pending(ledger)
                if pending is not None:
                    if self.state is VerifiedMotionExecutorState.INPUT_LOST:
                        self.state = VerifiedMotionExecutorState.RECOVERING
                        self._recovery_uses_verified_remainder = False
                        self._terminal_after_recovery = (
                            VerifiedMotionExecutorState.INPUT_LOST
                        )
                        self._pending = None
                        return self._decision(
                            MovementV1(), None, "recovery_input_unconfirmed",
                        )
                    return self._decision(None, None, pending)
                if self._command_index < len(self._candidate.proof.commands):
                    command = self._candidate.proof.commands[self._command_index]
                    return self._decision(
                        command.movement,
                        command.required_movement_yaw_radians,
                        "complete_verified_landing_after_cancel",
                    )
            return self._decision(MovementV1(), None, "retain_landing_responsibility")
        if self.state is not VerifiedMotionExecutorState.RUNNING:
            return self._decision(None, None, self.state.value)
        proof = self._candidate.proof
        if anchor.session != proof.entry_state.session:
            self.state = VerifiedMotionExecutorState.FAILED
            return self._decision(MovementV1(), None, "world_session_changed")
        pending = self._consume_pending(ledger)
        if pending is not None:
            if (self.state is VerifiedMotionExecutorState.INPUT_LOST
                    and not anchor.physics_state.on_ground):
                self.state = VerifiedMotionExecutorState.RECOVERING
                self._recovery_uses_verified_remainder = False
                self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST
                self._pending = None
                return self._decision(
                    MovementV1(), None, "retain_landing_after_input_loss",
                )
            movement = (MovementV1()
                        if self.state is VerifiedMotionExecutorState.INPUT_LOST
                        else None)
            return self._decision(movement, None, pending)
        if self._command_index >= len(proof.commands):
            if not anchor.physics_state.on_ground:
                self.state = VerifiedMotionExecutorState.RECOVERING
                return self._decision(MovementV1(), None, "awaiting_verified_landing")
            if not _state_fits_entry(anchor.physics_state, proof.exit_state):
                self.state = VerifiedMotionExecutorState.FAILED
                return self._decision(MovementV1(), None, "verified_exit_not_observed")
            self.state = VerifiedMotionExecutorState.COMPLETE
            return self._decision(MovementV1(), None, "verified_motion_complete")
        command = proof.commands[self._command_index]
        return self._decision(
            command.movement, command.required_movement_yaw_radians,
            "submit_verified_command",
        )
