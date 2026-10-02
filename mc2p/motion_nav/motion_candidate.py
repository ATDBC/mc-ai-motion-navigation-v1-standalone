"""B10-C task binding, admission and receipt-driven execution for verified motion."""
from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import (
    ContractViolation, require_identifier, require_nonnegative_int,
)
from mc2p.motion_nav.motion_solver import (
    VerifiedMotionResult, VerifiedMotionStartVariant,
)
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputApplicationStatus, StateAnchor,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.physics_types import PhysicsState
from mc2p.motion_nav.world_model import BlockPos


_ENTRY_POSITION_TOLERANCE = 0.05
_ENTRY_VELOCITY_TOLERANCE_PER_TICK = 0.01
_ENTRY_YAW_TOLERANCE_RADIANS = math.radians(1.0)
_RECOVERY_GROUND_SPEED_TOLERANCE_PER_TICK = 0.01
_PENDING_APPLICATION_GRACE_TICKS = 1


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
    damage_budget: TaskDamageBudget
    accepted_resource_incomplete_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("planning_request_id", "goal_id", "route_id"):
            require_identifier(getattr(self, name), name.replace("_", " "))
        if type(self.damage_budget) is not TaskDamageBudget:
            raise ContractViolation("motion candidate damage budget must be typed")
        for name in ("planning_generation", "goal_revision", "route_revision",
                     "action_index", "candidate_revision"):
            require_nonnegative_int(getattr(self, name), name.replace("_", " "))
        if (type(self.accepted_resource_incomplete_reasons) is not tuple
                or self.accepted_resource_incomplete_reasons
                   != tuple(sorted(set(self.accepted_resource_incomplete_reasons)))
                or any(type(reason) is not str or not reason
                       for reason in self.accepted_resource_incomplete_reasons)):
            raise ContractViolation("resource assumptions must be sorted and unique")

    @property
    def risk_policy_id(self) -> str:
        return self.damage_budget.risk_policy_id


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
    NEEDS_REVALIDATION = "needs_revalidation"


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
    can_recompute: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not MotionCandidateStatus:
            raise ContractViolation("invalid motion candidate admission status")
        if type(self.can_recompute) is not bool:
            raise ContractViolation("candidate recomputation disposition must be boolean")
        if (self.status is MotionCandidateStatus.ACCEPTED) != (
                type(self.candidate) is AdmittedMotionCandidate):
            raise ContractViolation("accepted admission must carry its candidate")

    @property
    def retryable(self) -> bool:
        """Whether a fresh candidate may repair this admission failure."""
        return self.can_recompute


def _angle_error(first: float, second: float) -> float:
    return abs((first - second + math.pi) % (2.0 * math.pi) - math.pi)


def _physics_conditions_match(actual: PhysicsState, expected: PhysicsState) -> bool:
    continuous = {"position", "velocity_blocks_per_tick", "yaw_radians"}
    for field in fields(PhysicsState):
        if field.name in continuous | {"movement_tick_id"}:
            continue
        first, second = getattr(actual, field.name), getattr(expected, field.name)
        if first == second:
            continue
        # JSON/shape arithmetic can round the same attribute by one ULP.
        # This is representational equality, not a larger physical envelope.
        if (type(first) is float and type(second) is float
                and abs(first - second) <= 4 * max(math.ulp(first), math.ulp(second))):
            continue
        return False
    return True


def _state_matches_verified_start(actual: PhysicsState, expected: PhysicsState) -> bool:
    """Only numerical representation error; no new continuous entry envelope.

    The aligned ordinary-ground audit measured <6e-9 block position error.
    The 1e-7 limit is deliberately much narrower than the tracking envelope;
    unknown rules, attributes, poses or input history are never covered by it.
    """
    return (
        actual.movement_tick_id == expected.movement_tick_id
        and _physics_conditions_match(actual, expected)
        and all(abs(a - b) <= 1e-7 for a, b in zip(actual.position, expected.position))
        and all(abs(a - b) <= 1e-7 for a, b in zip(
            actual.velocity_blocks_per_tick, expected.velocity_blocks_per_tick))
        and _angle_error(actual.yaw_radians, expected.yaw_radians) <= 1e-7
    )


def _proved_prelude_applied(proof, anchor, ledger) -> bool:
    preparation = proof.preparation
    first_tick = (proof.anchor_movement_tick_id + 1 if preparation is None
                  else preparation.source_anchor.movement_tick_id + 1)
    last_tick = anchor.movement_tick_id
    if last_tick < first_tick:
        return True
    if ledger is None:
        return False
    # Sampled axes omit yaw. Exclude every look that may have affected this
    # interval, including a pure-look command with a different request id.
    for record in ledger.snapshot():
        if (record.action.look != LookV1()
                and record.requested_first_tick <= last_tick
                and record.latest_allowed_first_tick
                    + record.action.valid_for_ticks - 1 >= first_tick):
            return False
    for tick in range(first_tick, last_tick + 1):
        sample = ledger.sample(tick)
        if sample is None:
            return False
        index = tick - first_tick
        if preparation is not None and index < len(preparation.tick_inputs):
            expected = preparation.tick_inputs[index]
            record = (None if sample.request_sequence_id is None else
                      ledger.record(sample.request_sequence_id))
            if record is None or record.session != anchor.session:
                return False
            values = (expected.forward, expected.strafe, expected.jump,
                      expected.sneak, expected.sprint)
        else:
            values = (0.0, 0.0, False, False, False)
        if (sample.forward, sample.strafe, sample.jump, sample.sneak, sample.sprint) != values:
            return False
    return True


def verified_candidate_can_start(
    candidate: AdmittedMotionCandidate,
    anchor: StateAnchor,
) -> bool:
    """Check an unsubmitted candidate against the next real movement tick."""
    if (type(candidate) is not AdmittedMotionCandidate
            or type(anchor) is not StateAnchor):
        raise ContractViolation("verified entry check requires typed inputs")
    variant = candidate.proof.start_variant(anchor.movement_tick_id + 1)
    return (
        variant is not None
        and anchor.session == variant.entry_state.session
        and _state_matches_verified_start(anchor.physics_state, variant.entry_state)
    )


def _state_satisfies_verified_exit(
        actual: PhysicsState,
        proof: VerifiedMotionResult,
        variant: VerifiedMotionStartVariant,
) -> bool:
    """Check the proved landing envelope instead of one simulated point.

    The calculator proves a safe landing region and an exit-speed bound.  The
    game may settle at another point inside that region, especially after a
    sneak-edge release.  Requiring the exact simulated position would reject a
    safe, observed landing even though the proof's real postcondition holds.
    """
    expected = variant.exit_state
    if (actual.session != expected.session
            or actual.ruleset_id != expected.ruleset_id
            or actual.state_schema != expected.state_schema
            or actual.pose != expected.pose
            or not actual.on_ground
            or actual.swimming != expected.swimming
            or actual.climbing != expected.climbing
            or actual.fall_flying != expected.fall_flying
            or actual.flying != expected.flying
            or actual.is_using_item != expected.is_using_item
            or (not proof.continuation.accepts(actual)
                if proof.continuation is not None else
                not proof.landing.contains(actual, epsilon=1.0e-6))):
        return False
    horizontal_speed = math.hypot(
        actual.velocity_blocks_per_tick[0],
        actual.velocity_blocks_per_tick[2],
    )
    maximum_speed = (
        _RECOVERY_GROUND_SPEED_TOLERANCE_PER_TICK
        if proof.exit_direction is None else .22
    )
    return (
        (proof.continuation is not None or horizontal_speed <= maximum_speed + 1.0e-9)
        and _angle_error(actual.yaw_radians, expected.yaw_radians)
            <= _ENTRY_YAW_TOLERANCE_RADIANS
    )


class MotionCandidateAdmitter:
    """Check whether one task-bound proof still has authority to start now."""

    def admit(
            self, candidate: VerifiedMotionCandidate, anchor: StateAnchor, *,
            planning_request_id: str, planning_generation: int,
            goal_id: str, goal_revision: int,
            route_id: str, route_revision: int, action_index: int,
            candidate_revision: int, damage_budget: TaskDamageBudget,
            intended_start_tick: int,
            changed_cells: tuple[BlockPos, ...],
            input_ledger: InputApplicationLedger | None = None,
    ) -> MotionCandidateAdmission:
        if (type(candidate) is not VerifiedMotionCandidate
                or type(anchor) is not StateAnchor
                or type(changed_cells) is not tuple
                or (input_ledger is not None
                    and type(input_ledger) is not InputApplicationLedger)):
            raise ContractViolation("motion candidate admission requires typed inputs")
        require_nonnegative_int(intended_start_tick, "intended start tick")
        current = MotionCandidateContext(
            planning_request_id, planning_generation, goal_id, goal_revision,
            route_id, route_revision, action_index, candidate_revision,
            damage_budget, candidate.context.accepted_resource_incomplete_reasons,
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
        if current.damage_budget != expected.damage_budget:
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "risk_policy_changed",
            )
        proof = candidate.proof
        if (proof.damage_budget != current.damage_budget
                or not current.damage_budget.allows(
                    proof.maximum_expected_damage_points,
                    health_points=anchor.health_points,
                    absorption_points=anchor.absorption_points,
                )):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "damage_budget_or_vitality_changed",
            )
        if (anchor.session != proof.entry_state.session
                or anchor.ruleset_id != proof.ruleset_id
                or anchor.state_schema != proof.entry_state.state_schema
                or anchor.input_projection_version
                   != proof.input_projection_version):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "state_identity_changed",
            )
        if not proof.execution_window.allows_start(intended_start_tick):
            retry = intended_start_tick == anchor.movement_tick_id + 1
            return MotionCandidateAdmission(
                (MotionCandidateStatus.NEEDS_REVALIDATION if retry
                 else MotionCandidateStatus.REJECTED), "execution_window_expired",
                can_recompute=retry,
            )
        if set(changed_cells).intersection(proof.world_dependencies):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "world_dependency_changed",
                can_recompute=True,
            )
        if not set(proof.resource_incomplete_reasons).issubset(
                expected.accepted_resource_incomplete_reasons):
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED, "resource_evidence_incomplete",
            )
        variant = proof.start_variant(intended_start_tick)
        if (intended_start_tick != anchor.movement_tick_id + 1
                or variant is None
                or not _proved_prelude_applied(proof, anchor, input_ledger)):
            return MotionCandidateAdmission(
                MotionCandidateStatus.NEEDS_REVALIDATION, "state_anchor_advanced",
                can_recompute=True,
            )
        if not _state_matches_verified_start(anchor.physics_state, variant.entry_state):
            return MotionCandidateAdmission(
                MotionCandidateStatus.NEEDS_REVALIDATION, "entry_state_changed",
                can_recompute=True,
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
    submittable_as_verified_command: bool = False


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
        self._start_variant: VerifiedMotionStartVariant | None = None
        self._start_tick: int | None = None
        self._command_index = 0
        self._pending: _PendingSubmission | None = None
        self._cancel_requested = False
        self._recovery_started_at_tick: int | None = None
        self._recovery_uses_verified_remainder = False
        self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST
        self._coast_checked_through_tick: int | None = None

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
        self._start_variant = candidate.proof.start_variant(intended_start_tick)
        if self._start_variant is None:
            raise ContractViolation("verified candidate omitted its intended start")
        self._start_tick = intended_start_tick
        self._command_index = 0
        self._pending = None
        self._cancel_requested = False
        self._recovery_started_at_tick = None
        self._recovery_uses_verified_remainder = False
        self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST
        self._coast_checked_through_tick = None
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
        replacing_pending = self._pending is not None
        if replacing_pending:
            can_replace = (
                command_index == self._pending.command_index
                and self._pending.requested_movement_tick
                    < requested_movement_tick
                    <= self._pending.requested_latest_movement_tick
                and requested_latest_movement_tick
                    == self._pending.requested_latest_movement_tick
            )
            if not can_replace:
                raise ContractViolation(
                    "verified executor already has an in-flight command"
                )
        if command_index != self._command_index:
            raise ContractViolation("submitted command is not the current proof command")
        expected_tick = self._expected_tick()
        if not replacing_pending and requested_movement_tick != expected_tick:
            raise ContractViolation("submitted command targets another movement tick")
        if requested_latest_movement_tick != self._latest_tick():
            raise ContractViolation("submitted command uses another movement window")
        self._pending = _PendingSubmission(
            command_index, control_sequence, requested_movement_tick,
            requested_latest_movement_tick,
        )

    def predicted_exit_state(self) -> PhysicsState | None:
        """Return the chosen start variant's exit after its first receipt.

        Before the first command is observed the execution window can still
        select either delayed-start variant, so no single exit is authoritative.
        """
        if (self.state is not VerifiedMotionExecutorState.RUNNING
                or self._command_index < 1 or self._start_variant is None):
            return None
        return self._start_variant.exit_state

    def entry_state(self) -> PhysicsState | None:
        """Return the selected proof entry for bounded grounded recovery."""
        return None if self._start_variant is None else self._start_variant.entry_state

    def confirm_observed_exit_after_input_loss(
        self,
        anchor: StateAnchor,
    ) -> bool:
        """Finish a lost command sequence when its proved exit is observed.

        A late request identity can be lost after the physical action has
        already landed.  Returning to the old entry in that case would undo a
        completed action and can make the solver see an impossible reverse
        transition.  The action may finish only when the current state
        satisfies the original landing envelope, speed and heading bounds.
        """
        if type(anchor) is not StateAnchor:
            raise ContractViolation("verified exit confirmation requires an anchor")
        if (self.state is not VerifiedMotionExecutorState.INPUT_LOST
                or self._candidate is None or self._start_variant is None):
            return False
        if not _state_satisfies_verified_exit(
                anchor.physics_state, self._candidate.proof,
                self._start_variant):
            return False
        self._pending = None
        self.state = VerifiedMotionExecutorState.COMPLETE
        return True

    def retain_landing_after_input_loss(self, anchor: StateAnchor) -> bool:
        """Keep body ownership when a grounded flag races edge departure."""
        if type(anchor) is not StateAnchor:
            raise ContractViolation("verified landing recovery requires an anchor")
        if self.state is not VerifiedMotionExecutorState.INPUT_LOST:
            return False
        self._pending = None
        self._recovery_started_at_tick = anchor.movement_tick_id
        self._recovery_uses_verified_remainder = False
        self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST
        self.state = VerifiedMotionExecutorState.RECOVERING
        return True

    def has_started(self) -> bool:
        """Return whether this proof has already taken body responsibility.

        Registering the first command is the action boundary.  From then on,
        missing or late observations belong to execution recovery; callers
        must not send the same action back through its entry preconditions.
        """
        return self._pending is not None or self._command_index > 0

    def can_start_from(self, anchor: StateAnchor) -> bool:
        """Whether no command has been sent and this proof fits now."""
        if type(anchor) is not StateAnchor:
            raise ContractViolation("verified entry check requires an anchor")
        if self._candidate is None or self.has_started():
            return False
        return verified_candidate_can_start(self._candidate, anchor)

    def cancel(
        self, anchor: StateAnchor, *, preserve_verified_remainder: bool = True,
    ) -> None:
        if type(anchor) is not StateAnchor:
            raise ContractViolation("verified cancellation requires a state anchor")
        if type(preserve_verified_remainder) is not bool:
            raise ContractViolation(
                "verified cancellation remainder policy must be boolean"
            )
        if self.state is not VerifiedMotionExecutorState.RUNNING:
            return
        self._cancel_requested = True
        self._recovery_started_at_tick = anchor.movement_tick_id
        self.state = VerifiedMotionExecutorState.RECOVERING
        self._recovery_uses_verified_remainder = (
            preserve_verified_remainder
            and (self._pending is not None or not anchor.physics_state.on_ground)
        )
        if not preserve_verified_remainder:
            self._pending = None
        self._terminal_after_recovery = VerifiedMotionExecutorState.CANCELLED

    def recover_without_anchor(self) -> VerifiedMotionDecision:
        """Keep body ownership when current motion cannot be re-anchored.

        A missing anchor means the proof may no longer describe the observed
        body.  New proof commands therefore cannot be issued.  The executor
        still owns the body and sends neutral movement until a later anchor can
        confirm landing and finish the recovery.
        """
        if self._candidate is None:
            self.state = VerifiedMotionExecutorState.IDLE
            return self._decision(None, None, "not_started")
        if self.state is VerifiedMotionExecutorState.RUNNING:
            self._pending = None
            self.state = VerifiedMotionExecutorState.RECOVERING
            self._recovery_uses_verified_remainder = False
            self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST
        if self.state is VerifiedMotionExecutorState.RECOVERING:
            return self._decision(
                MovementV1(), None,
                "verified_motion_anchor_unavailable_retain_landing",
            )
        return self._decision(None, None, self.state.value)

    def _expected_tick(self) -> int:
        assert self._start_tick is not None
        return self._start_tick + self._command_index

    def _latest_tick(self) -> int:
        assert self._candidate is not None
        if self._command_index == 0:
            return self._candidate.proof.execution_window.latest_start_tick
        return self._expected_tick()

    def _proof_exit_tick(self) -> int:
        assert self._candidate is not None
        assert self._start_tick is not None
        return self._start_tick + len(self._candidate.proof.commands) - 1

    def _decision(self, movement: MovementV1 | None,
                  yaw: float | None, reason: str, *,
                  submittable_as_verified_command: bool = False,
                  expected_movement_tick: int | None = None,
                  latest_movement_tick: int | None = None,
                  ) -> VerifiedMotionDecision:
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
        if expected_movement_tick is not None:
            expected = expected_movement_tick
        if latest_movement_tick is not None:
            latest = latest_movement_tick
        return VerifiedMotionDecision(
            self.state, movement, yaw, self._command_index, expected, latest,
            1 if movement is not None else 0, reason,
            submittable_as_verified_command,
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
        if record.status is InputApplicationStatus.EXPIRED:
            # Expiry of one submitted lease is not yet loss of the proved
            # command while a later start tick is still inside the solver's
            # accepted window.  decide() owns that time comparison and may
            # submit the same proved command again for the remaining tick.
            return "input_expired"
        if record.status in {
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
            actual_start_tick = record.applied_ticks[0]
            variant = self._candidate.proof.start_variant(actual_start_tick)
            if variant is None:
                self.state = VerifiedMotionExecutorState.INPUT_LOST
                return "unverified_start_tick"
            self._start_tick = actual_start_tick
            self._start_variant = variant
        self._command_index += 1
        self._pending = None
        return None

    def _consume_observed_neutral_tick(
        self,
        anchor: StateAnchor,
        ledger: InputApplicationLedger,
    ) -> bool:
        """Accept exact neutral physics evidence when its request arrived late.

        Once an action has started, a later proof row may require no movement
        input.  The client input sample is the authority for what the game
        actually consumed.  If that sample is present and exactly neutral,
        losing only the pending request identity does not change the proved
        physics.  Active inputs and missing samples still use normal input-loss
        recovery.
        """
        if (self._candidate is None or self._pending is None
                or self._command_index == 0
                or anchor.movement_tick_id
                    < self._pending.requested_movement_tick):
            return False
        command = self._candidate.proof.commands[self._pending.command_index]
        if command.movement != MovementV1():
            return False
        sample = ledger.sample(self._pending.requested_movement_tick)
        if (sample is None
                or abs(float(sample.forward)) > 1.0e-9
                or abs(float(sample.strafe)) > 1.0e-9
                or sample.jump or sample.sneak or sample.sprint):
            return False
        self._command_index += 1
        self._pending = None
        return True

    def _recover_from_expired_command_window(
        self,
        anchor: StateAnchor,
    ) -> VerifiedMotionDecision | None:
        """Stop issuing proof commands once their movement tick has passed."""
        if anchor.movement_tick_id + 1 <= self._latest_tick():
            return None
        self._recovery_uses_verified_remainder = False
        self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST
        if self._pending is not None:
            # A submitted command can still be sitting between the Runtime and
            # the client when its proved start window closes.  A neutral
            # replacement does not prove that the older command can no longer
            # reach the game on the next tick.  Keep the body owner for one
            # further observation (and through landing if it arrives late)
            # instead of releasing a body that can become airborne immediately
            # after this grounded frame.
            self.state = VerifiedMotionExecutorState.RECOVERING
            self._recovery_started_at_tick = anchor.movement_tick_id
            return self._decision(
                MovementV1(), None,
                "expired_pending_submission_retain_responsibility",
            )
        self._pending = None
        if anchor.physics_state.on_ground:
            self.state = VerifiedMotionExecutorState.INPUT_LOST
            return self._decision(
                MovementV1(), None, "verified_command_window_expired",
            )
        self.state = VerifiedMotionExecutorState.RECOVERING
        self._recovery_started_at_tick = anchor.movement_tick_id
        return self._decision(
            MovementV1(), None,
            "verified_command_window_expired_retain_landing",
        )

    def _remaining_commands_are_neutral(self) -> bool:
        """Whether the proof only waits for the already-caused motion to land."""
        assert self._candidate is not None
        commands = self._candidate.proof.commands
        return (
            self._command_index < len(commands)
            and all(command.movement == MovementV1()
                    for command in commands[self._command_index:])
        )

    def _coast_input_changed(
        self,
        anchor: StateAnchor,
        ledger: InputApplicationLedger,
    ) -> str | None:
        if self._coast_checked_through_tick is None:
            return None
        first_tick = self._coast_checked_through_tick + 1
        if first_tick > anchor.movement_tick_id:
            return None
        samples = tuple(
            ledger.sample(tick)
            for tick in range(first_tick, anchor.movement_tick_id + 1)
        )
        if any(sample is None for sample in samples):
            return "missing"
        self._coast_checked_through_tick = anchor.movement_tick_id
        return "changed" if any(
            abs(float(sample.forward)) > 1.0e-9
            or abs(float(sample.strafe)) > 1.0e-9
            or sample.jump or sample.sneak or sample.sprint
            for sample in samples if sample is not None
        ) else None

    def decide(self, anchor: StateAnchor,
               ledger: InputApplicationLedger, *,
               changed_cells: tuple[BlockPos, ...] = ()) -> VerifiedMotionDecision:
        if (type(anchor) is not StateAnchor
                or type(ledger) is not InputApplicationLedger
                or type(changed_cells) is not tuple):
            raise ContractViolation("verified decision requires anchor and input ledger")
        if self._candidate is None:
            self.state = VerifiedMotionExecutorState.IDLE
            return self._decision(None, None, "not_started")
        proof = self._candidate.proof
        if (self.state in {
                VerifiedMotionExecutorState.RUNNING,
                VerifiedMotionExecutorState.RECOVERING,
        } and set(changed_cells).intersection(proof.world_dependencies)):
            self._pending = None
            # The changed dependency supersedes an earlier cancellation.
            # Landing now terminates as a failed proof, so it must not wait
            # for the cancellation's next-observation gate.
            self._recovery_started_at_tick = None
            if anchor.physics_state.on_ground:
                self.state = VerifiedMotionExecutorState.FAILED
                return self._decision(
                    MovementV1(), None,
                    "world_dependency_changed_during_execution",
                )
            self.state = VerifiedMotionExecutorState.RECOVERING
            self._recovery_uses_verified_remainder = False
            self._terminal_after_recovery = VerifiedMotionExecutorState.FAILED
            return self._decision(
                MovementV1(), None,
                "world_dependency_changed_retain_landing",
            )
        if self.state is VerifiedMotionExecutorState.RECOVERING:
            recovery_observation_advanced = (
                self._recovery_started_at_tick is None
                or anchor.movement_tick_id > self._recovery_started_at_tick
            )
            horizontal_speed = math.hypot(
                anchor.physics_state.velocity_blocks_per_tick[0],
                anchor.physics_state.velocity_blocks_per_tick[2],
            )
            stable_ground = (
                anchor.physics_state.on_ground
                and horizontal_speed
                    <= _RECOVERY_GROUND_SPEED_TOLERANCE_PER_TICK
            )
            if (stable_ground and self._pending is None
                    and recovery_observation_advanced):
                self.state = self._terminal_after_recovery
                return self._decision(MovementV1(), None, self.state.value)
            if self._recovery_uses_verified_remainder:
                pending = self._consume_pending(ledger)
                if pending is not None:
                    if (pending in {"awaiting_application", "input_expired"}
                            and self._pending is not None
                            and anchor.movement_tick_id
                                > self._pending.requested_latest_movement_tick
                                  + _PENDING_APPLICATION_GRACE_TICKS):
                        # The command can no longer receive an in-window
                        # receipt, and the one-tick transport grace has also
                        # passed.  It may still have affected the body late,
                        # so keep neutral landing ownership, but do not wait
                        # forever for an identity the ledger may never see.
                        self._pending = None
                        self._recovery_uses_verified_remainder = False
                        return self._decision(
                            MovementV1(), None,
                            "recovery_application_unresolved",
                        )
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
                    expired = self._recover_from_expired_command_window(anchor)
                    if expired is not None:
                        return expired
                    command = self._candidate.proof.commands[self._command_index]
                    return self._decision(
                        command.movement,
                        command.required_movement_yaw_radians,
                        "complete_verified_landing_after_cancel",
                        submittable_as_verified_command=True,
                    )
            if stable_ground:
                if not recovery_observation_advanced:
                    return self._decision(
                        MovementV1(), None, "awaiting_cancel_observation",
                    )
                self.state = self._terminal_after_recovery
                return self._decision(MovementV1(), None, self.state.value)
            return self._decision(MovementV1(), None, "retain_landing_responsibility")
        if self.state is not VerifiedMotionExecutorState.RUNNING:
            return self._decision(None, None, self.state.value)
        if anchor.session != proof.entry_state.session:
            self.state = VerifiedMotionExecutorState.FAILED
            return self._decision(MovementV1(), None, "world_session_changed")
        pending = self._consume_pending(ledger)
        consumed_observed_neutral = False
        if pending == "awaiting_application":
            consumed_observed_neutral = self._consume_observed_neutral_tick(
                anchor, ledger,
            )
            if consumed_observed_neutral:
                pending = None
        if pending is not None:
            if self.state is VerifiedMotionExecutorState.INPUT_LOST:
                self.state = VerifiedMotionExecutorState.RECOVERING
                self._recovery_uses_verified_remainder = False
                self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST
                self._recovery_started_at_tick = anchor.movement_tick_id
                self._pending = None
                return self._decision(
                    MovementV1(), None, "retain_landing_after_input_loss",
                )
            if (pending in {"awaiting_application", "input_expired"}
                    and self._pending is not None
                    and anchor.movement_tick_id + 1
                        > self._pending.requested_movement_tick
                    and anchor.movement_tick_id + 1
                        <= self._pending.requested_latest_movement_tick):
                command = proof.commands[self._pending.command_index]
                return self._decision(
                    command.movement,
                    command.required_movement_yaw_radians,
                    "resubmit_verified_command_within_window",
                    submittable_as_verified_command=True,
                    expected_movement_tick=anchor.movement_tick_id + 1,
                    latest_movement_tick=(
                        self._pending.requested_latest_movement_tick
                    ),
                )
            if (pending in {"awaiting_application", "input_expired"}
                    and self._pending is not None
                    and anchor.movement_tick_id
                        >= self._pending.requested_latest_movement_tick):
                expired = self._recover_from_expired_command_window(anchor)
                if expired is not None:
                    return expired
            movement = (MovementV1()
                        if self.state is VerifiedMotionExecutorState.INPUT_LOST
                        else None)
            return self._decision(movement, None, pending)
        if self._command_index == 0 and self._pending is None:
            variant = proof.start_variant(anchor.movement_tick_id + 1)
            if (variant is not None
                    and _state_matches_verified_start(anchor.physics_state, variant.entry_state)
                    and _proved_prelude_applied(proof, anchor, ledger)):
                # An unselected first intent has not started the action. Reuse
                # only a published later start whose real neutral prelude matches.
                self._start_tick = variant.start_tick
                self._start_variant = variant
        if (self._command_index == 0 and self._pending is None
                and self._start_variant is not None
                and (not _state_matches_verified_start(
                    anchor.physics_state, self._start_variant.entry_state,
                ) or not _proved_prelude_applied(proof, anchor, ledger))):
            self.state = (
                VerifiedMotionExecutorState.INPUT_LOST
                if anchor.physics_state.on_ground else
                VerifiedMotionExecutorState.RECOVERING
            )
            self._recovery_uses_verified_remainder = False
            self._terminal_after_recovery = VerifiedMotionExecutorState.INPUT_LOST
            return self._decision(
                MovementV1(), None,
                ("verified_entry_not_observed"
                 if anchor.physics_state.on_ground else
                 "verified_entry_not_observed_retain_landing"),
            )
        # A jump proof commonly has two active inputs followed by many neutral
        # simulation ticks. Requiring a separately identified request for every
        # neutral tick adds no physical guarantee: the client sample already
        # reports neutral input, and the observed landing state is checked below.
        # Keep body ownership and coast instead of turning a scheduler skip into
        # input loss.
        if self._remaining_commands_are_neutral():
            self._coast_checked_through_tick = self._expected_tick() - 1
            self._command_index = len(proof.commands)
        if self._command_index >= len(proof.commands):
            coast_input = self._coast_input_changed(anchor, ledger)
            if coast_input is not None:
                self._pending = None
                self.state = VerifiedMotionExecutorState.RECOVERING
                self._recovery_uses_verified_remainder = False
                self._terminal_after_recovery = (
                    VerifiedMotionExecutorState.INPUT_LOST
                )
                return self._decision(
                    MovementV1(), None,
                    ("coast_input_unobserved_retain_landing"
                     if coast_input == "missing"
                     else "coast_input_not_neutral_retain_landing"),
                )
            # The neutral suffix includes both free fall and the on-ground
            # settling frames used by the solver to define its exit state.
            # Landing early is therefore not completion: keep neutral control
            # until the proved horizon, then compare the observed final state.
            if anchor.movement_tick_id < self._proof_exit_tick():
                return self._decision(
                    MovementV1(), None, "coast_to_verified_landing",
                )
            if not anchor.physics_state.on_ground:
                return self._decision(
                    MovementV1(), None, "coast_to_verified_landing",
                )
            assert self._start_variant is not None
            if not _state_satisfies_verified_exit(
                    anchor.physics_state, proof, self._start_variant):
                self.state = VerifiedMotionExecutorState.FAILED
                return self._decision(MovementV1(), None, "verified_exit_not_observed")
            self.state = VerifiedMotionExecutorState.COMPLETE
            return self._decision(MovementV1(), None, "verified_motion_complete")
        expired = self._recover_from_expired_command_window(anchor)
        if expired is not None:
            return expired
        command = proof.commands[self._command_index]
        return self._decision(
            command.movement, command.required_movement_yaw_radians,
            ("submit_verified_command_after_observed_neutral"
             if consumed_observed_neutral else "submit_verified_command"),
            submittable_as_verified_command=True,
        )
