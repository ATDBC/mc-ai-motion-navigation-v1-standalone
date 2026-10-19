"""B10-C task binding, admission and receipt-driven execution for verified motion."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from enum import StrEnum
import math
from uuid import uuid4

from mc2p.contracts.action_v1 import LookV1, MovementTickWindowV1, MovementV1
from mc2p.contracts.motion_activation import (
    ActivationRejectReason, ActivationResult, ActivationStatus, FirstStreamApplication,
    PreparedActivationAcceptance, PreparedActivationDelivery, PreparedActivationTransaction,
    StreamActivationProposal, StreamApplicationReason, StreamApplicationResult,
    StreamApplicationStatus, StreamStartBranch, IncumbentFallbackDispatch,
)
from mc2p.contracts.common import (
    ContractViolation, require_identifier, require_nonnegative_int,
)
from mc2p.motion_nav.motion_solver import (
    MotionCommandTick, VerifiedMotionResult, VerifiedMotionStartVariant,
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
    committed_executor: object | None = None

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


def verified_start_state_matches(
        actual: PhysicsState, expected: PhysicsState) -> bool:
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
        if (record.status is InputApplicationStatus.APPLIED
                and record.action.valid_for_ticks == 1
                and record.applied_ticks
                and record.applied_ticks[-1] < first_tick):
            # A confirmed view change before the real source is already in
            # that observation. Its old allowed window is not a future change.
            continue
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
        and verified_start_state_matches(
            anchor.physics_state, variant.entry_state,
        )
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

    def admit(self, candidate, anchor, **values) -> MotionCandidateAdmission:
        return self._admit(candidate, anchor, **values)

    def admit_committed(self, candidate, anchor, *, executor,
                        **values) -> MotionCandidateAdmission:
        """Opt in only for a bound proof and the executor retaining its body."""
        if (type(executor) is not VerifiedMotionExecutor
                or executor.state is not VerifiedMotionExecutorState.IDLE
                or executor.prepared_handle is not None
                or type(candidate) is not VerifiedMotionCandidate
                or not candidate.proof.allow_committed_main_suffix
                or candidate.proof.successor_binding is None):
            return MotionCandidateAdmission(MotionCandidateStatus.REJECTED,
                                            "committed_execution_binding_missing")
        if any(value.forward not in (-1., 0., 1.) or value.strafe not in (-1., 0., 1.)
               or value.sneak and (value.forward != 0. or value.strafe != 0.)
               for variant in (candidate.proof, *candidate.proof.delayed_start_variants)
               for tail in variant.fallback_tails
               for value in (*tail.inputs, *((tail.release_witness_input,)
                   if tail.release_witness_input is not None else ()))):
            return MotionCandidateAdmission(MotionCandidateStatus.REJECTED,
                                            "committed_input_projection_unsupported")
        return self._admit(candidate, anchor, committed_executor=executor, **values)

    def _admit(
            self, candidate: VerifiedMotionCandidate, anchor: StateAnchor, *,
            planning_request_id: str, planning_generation: int,
            goal_id: str, goal_revision: int,
            route_id: str, route_revision: int, action_index: int,
            candidate_revision: int, damage_budget: TaskDamageBudget,
            intended_start_tick: int,
            changed_cells: tuple[BlockPos, ...],
            input_ledger: InputApplicationLedger | None = None,
            committed_executor=None,
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
        if proof.allow_committed_main_suffix and committed_executor is None:
            return MotionCandidateAdmission(
                MotionCandidateStatus.REJECTED,
                "committed_main_suffix_execution_not_enabled",
            )
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
        if not verified_start_state_matches(
                anchor.physics_state, variant.entry_state):
            return MotionCandidateAdmission(
                MotionCandidateStatus.NEEDS_REVALIDATION, "entry_state_changed",
                can_recompute=True,
            )
        return MotionCandidateAdmission(
            MotionCandidateStatus.ACCEPTED, "accepted",
            AdmittedMotionCandidate(
                candidate, anchor.observation_sequence_id,
                anchor.movement_tick_id, intended_start_tick,
                committed_executor,
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


class CommittedExecutionPhase(StrEnum):
    MAIN = "main"
    COMMITTED_SUFFIX = "committed_suffix"
    EXIT_CHOICE = "exit_choice"
    FALLBACK = "fallback"
    WITNESS = "witness"
    RELEASED = "released"
    UNRESOLVED = "unresolved"


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
    committed_phase: CommittedExecutionPhase | None = None


class VerifiedMotionExecutor:
    """Execute one admitted proof and advance only from exact client receipts."""

    def __init__(self, *, prepared_handle=None) -> None:
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
        self._prepared_handle = prepared_handle
        self._activation_delivery: PreparedActivationDelivery | None = None
        self._activation_handoff_token: str | None = None
        self._stream_tracker = None
        self._application_result: StreamApplicationResult | None = None
        self._committed_phase: CommittedExecutionPhase | None = None
        self._committed_variants = {}
        self._tail_command_views = {}
        self._selected_tail = None
        self._tail_cursor = 0
        self._release_sequence: int | None = None
        self._runtime_fallback: IncumbentFallbackDispatch | None = None

    @property
    def prepared_handle(self):
        return self._prepared_handle

    @property
    def activation_delivery(self):
        return self._activation_delivery

    @property
    def stream_tracker(self):
        return self._stream_tracker

    @property
    def remaining_start_branches(self):
        if self._prepared_handle is None:
            return ()
        branches = self._prepared_handle.hot_view.branches
        selected = None if self._stream_tracker is None else self._stream_tracker.start_branch
        return branches if selected is None else tuple(branch for branch in branches if branch.start_branch is selected)

    def prepare_activation(self, transaction: PreparedActivationTransaction,
                           accepted: PreparedActivationAcceptance) -> PreparedActivationDelivery | ActivationResult:
        """Deliver a checked binding to an idle executor, without starting it."""
        from mc2p.motion_nav.prepared_motion import PreparedMotionHandle
        from mc2p.motion_nav.input_stream import InputStreamTracker
        if (type(transaction) is not PreparedActivationTransaction or type(accepted) is not PreparedActivationAcceptance
                or type(self._prepared_handle) is not PreparedMotionHandle):
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.TRANSACTION)
        handle, binding = self._prepared_handle, transaction.binding
        if (self.state is not VerifiedMotionExecutorState.IDLE or self._activation_delivery is not None
                or accepted.transaction_token != transaction.transaction_token or accepted.binding is not binding
                or accepted.permission_token != transaction.permission_token
                or binding.proof_token != handle.proof_token or binding.ledger_id != handle.hot_view.ledger_id):
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.TRANSACTION)
        tracker = InputStreamTracker.start(handle.hot_view.stream)
        bound = tracker.bind_activation(binding)
        if bound.status is not ActivationStatus.ACTIVATED:
            return bound
        delivered = PreparedActivationDelivery._from_native_references(transaction.transaction_token, accepted.acceptance_token,
            binding, "delivery-" + uuid4().hex, transaction.new_action)
        self._activation_delivery, self._stream_tracker = delivered, tracker
        self._activation_handoff_token = transaction.handoff_token
        return delivered

    def commit_activation(self, delivered: PreparedActivationDelivery) -> None:
        """Consume this executor's exact delivery once; perform no registry or I/O work."""
        if (type(delivered) is not PreparedActivationDelivery or delivered is not self._activation_delivery
                or self.state is not VerifiedMotionExecutorState.IDLE or self._stream_tracker is None):
            raise ContractViolation("activation commit requires this idle executor's exact delivered binding")
        self._command_index = 0
        self._start_tick = delivered.binding.requested_window.earliest_tick
        self.state = VerifiedMotionExecutorState.RUNNING

    def record_application(self, application: FirstStreamApplication, *, command_index: int) -> StreamApplicationResult:
        if self._stream_tracker is None or self.state is VerifiedMotionExecutorState.IDLE:
            raise ContractViolation("stream receipt requires a committed delivered activation")
        require_nonnegative_int(command_index, "command index")
        facts = self._stream_tracker.command_facts
        if command_index < len(facts):
            fact = facts[command_index]
            exact = (type(application) is FirstStreamApplication
                     and application.generation_id == fact.generation_id
                     and application.control_sequence == fact.sequence
                     and application.effect_tick == self._stream_tracker.expected_effect_tick(command_index)
                     and self._stream_tracker._matches_input(application, command_index))
            result = StreamApplicationResult._from_native_references(
                StreamApplicationStatus.APPLIED if exact else StreamApplicationStatus.DEVIATION,
                self._stream_tracker.start_branch,
                None if exact else StreamApplicationReason.DUPLICATE)
            if not exact:
                self._application_result = result
                self._recovery_started_at_tick = application.effect_tick
                self.state = VerifiedMotionExecutorState.RECOVERING
            return result
        result = (self._stream_tracker.record_first_application(application) if command_index == 0 else
                  self._stream_tracker.record_application(application, command_index=command_index))
        self._application_result = result
        if result.status is StreamApplicationStatus.APPLIED:
            self._command_index = command_index + 1
            self._start_tick = application.effect_tick - command_index
            self._pending = None
        else:
            self._recovery_started_at_tick = application.effect_tick
            self.state = VerifiedMotionExecutorState.RECOVERING
        return result

    def prepared_command_proposal(self, decision):
        if (self._prepared_handle is None or self.state is not VerifiedMotionExecutorState.RUNNING
                or self._activation_handoff_token is None or not decision.submit_input
                or decision.verified_command_index != self._command_index
                or decision.expected_movement_tick is None or decision.latest_movement_tick is None):
            return None
        handle = self._prepared_handle
        command = handle.hot_view.commands[self._command_index]
        return StreamActivationProposal._from_native_references(
            proof_token=handle.proof_token, generation_id=handle.generation_id, owner_id=handle.owner_id,
            ledger_id=handle.hot_view.ledger_id, world_session_id=handle.hot_view.world_session_id,
            movement_intent_id="prepared-stream-command", expected_movement=command.movement,
            expected_yaw_radians=command.required_movement_yaw_radians,
            requested_window=MovementTickWindowV1(decision.expected_movement_tick, decision.latest_movement_tick),
            handoff_token=self._activation_handoff_token)

    def record_missing_application(self, *, command_index: int) -> StreamApplicationResult:
        require_nonnegative_int(command_index, "missing command index")
        result = StreamApplicationResult._from_native_references(StreamApplicationStatus.DEVIATION,
            None if self._stream_tracker is None else self._stream_tracker.start_branch,
            StreamApplicationReason.TIMING)
        self._application_result = result
        self.state = VerifiedMotionExecutorState.RECOVERING
        return result

    def prepared_decision(self, anchor: StateAnchor) -> VerifiedMotionDecision:
        """Compare the registered branch, preserving responsibility on any deviation."""
        handle, tracker = self._prepared_handle, self._stream_tracker
        if self.state is VerifiedMotionExecutorState.RECOVERING:
            if self._recovery_started_at_tick is None:
                self._recovery_started_at_tick = anchor.movement_tick_id
            horizontal_speed = math.hypot(
                anchor.physics_state.velocity_blocks_per_tick[0],
                anchor.physics_state.velocity_blocks_per_tick[2],
            )
            if (anchor.movement_tick_id > self._recovery_started_at_tick
                    and anchor.physics_state.on_ground
                    and horizontal_speed
                        <= _RECOVERY_GROUND_SPEED_TOLERANCE_PER_TICK):
                self.state = VerifiedMotionExecutorState.INPUT_LOST
                return VerifiedMotionDecision(
                    self.state, MovementV1(), None, self._command_index,
                    None, None, 1, self.state.value,
                )
            return VerifiedMotionDecision(self.state, None, None, self._command_index, None, None, 1,
                                          "BODY_RESPONSIBILITY_UNRESOLVED")
        if tracker.start_branch is None:
            return VerifiedMotionDecision(self.state, None, None, self._command_index, None, None, 1,
                                          "BODY_RESPONSIBILITY_UNRESOLVED")
        branch, = self.remaining_start_branches
        index = self._command_index
        if (index >= len(branch.trajectory)
                or not verified_start_state_matches(
                    anchor.physics_state, branch.trajectory[index],
                )):
            self.state = VerifiedMotionExecutorState.RECOVERING
            self._recovery_started_at_tick = anchor.movement_tick_id
            return VerifiedMotionDecision(self.state, None, None, index, None, None, 1,
                                          "BODY_RESPONSIBILITY_UNRESOLVED")
        if index == len(handle.hot_view.commands):
            self.state = VerifiedMotionExecutorState.COMPLETE
            return VerifiedMotionDecision(self.state, None, None, index, None, None, 1, "prepared_motion_complete")
        command = handle.hot_view.commands[index]
        tick = tracker.expected_effect_tick(index)
        return VerifiedMotionDecision(self.state, command.movement, command.required_movement_yaw_radians,
                                      index, tick, tick, 1, "prepared_stream_command", True)

    @staticmethod
    def prepared_action_proposal(handle, approval, *, supervisor=None, control=None) -> StreamActivationProposal | None:
        """Propose the frozen first input only after supervisor permission.

        This does not start an executor, choose a stream branch, or allocate a
        sequence. The actual activation belongs to Runtime in Task 8.
        """
        from mc2p.motion_nav.prepared_motion import (
            PreparedHandoffApproval, PreparedHandoffStatus, PreparedMotionHandle,
        )
        from mc2p.motion_nav.execution_supervisor import ExecutionSupervisor
        if (type(handle) is not PreparedMotionHandle or type(approval) is not PreparedHandoffApproval
                or type(supervisor) is not ExecutionSupervisor
                or not ExecutionSupervisor.prepared_approval_is_current(supervisor, approval, control=control)):
            return None
        proposal = approval.proposal
        if (approval.status is not PreparedHandoffStatus.APPROVED
                or proposal.proof_token != handle.proof_token
                or proposal.generation_id != handle.generation_id
                or proposal.owner_id != handle.owner_id
                or proposal.route_id != handle.route_id
                or proposal.route_revision != handle.hot_view.route_revision
                or proposal.ledger_id != handle.hot_view.ledger_id
                or proposal.world_session_id != handle.hot_view.world_session_id
                or proposal.permitted_window != handle.hot_view.requested_window
                or proposal.expected_movement != handle.hot_view.commands[0].movement
                or proposal.expected_yaw_radians != handle.hot_view.commands[0].required_movement_yaw_radians
                or proposal.new_action_index != handle.action_index):
            return None
        command, window = handle.hot_view.commands[0], handle.hot_view.requested_window
        return StreamActivationProposal._from_native_references(
            proof_token=handle.proof_token, generation_id=handle.generation_id, owner_id=handle.owner_id,
            ledger_id=handle.hot_view.ledger_id, world_session_id=handle.hot_view.world_session_id,
            movement_intent_id=f"prepared/{handle.proof_token}/0", expected_movement=command.movement,
            expected_yaw_radians=command.required_movement_yaw_radians,
            requested_window=MovementTickWindowV1(window.earliest_start_tick, window.latest_start_tick),
            handoff_token=proposal.handoff_token)

    def start(self, candidate: AdmittedMotionCandidate) -> None:
        if type(candidate) is not AdmittedMotionCandidate:
            raise ContractViolation("verified executor requires an admitted candidate")
        if (candidate.proof.allow_committed_main_suffix
                and candidate.committed_executor is not self):
            raise ContractViolation("committed proof requires this executor's explicit admission")
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
        if candidate.committed_executor is self:
            self._committed_phase = CommittedExecutionPhase.MAIN
            self._committed_variants = {
                variant.start_tick: variant for variant in
                (self._start_variant, *candidate.proof.delayed_start_variants)}
            self._tail_command_views = {
                id(tail): tuple(MotionCommandTick(MovementV1(
                    int(value.forward), int(value.strafe), value.jump,
                    value.sneak, value.sprint), value.movement_yaw_radians)
                    for value in (*tail.inputs, *((tail.release_witness_input,)
                        if tail.release_witness_input is not None else ())))
                for variant in self._committed_variants.values()
                for tail in variant.fallback_tails}
            self._selected_tail = None
            self._tail_cursor = 0
            self._release_sequence = None
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
            self._committed_phase,
        )
        if self._committed_phase is CommittedExecutionPhase.EXIT_CHOICE:
            self._committed_phase = CommittedExecutionPhase.FALLBACK

    def predicted_exit_state(self) -> PhysicsState | None:
        """Return the chosen start variant's exit after its first receipt.

        Before the first command is observed the execution window can still
        select either delayed-start variant, so no single exit is authoritative.
        """
        if (self.state is not VerifiedMotionExecutorState.RUNNING
                or self._command_index < 1 or self._start_variant is None):
            return None
        return self._start_variant.exit_state

    def predicted_exit_hold_input(self):
        """Read the confirmed branch's incumbent input at its proved exit."""
        if self.predicted_exit_state() is None:
            return None
        return self._start_variant.tick_inputs[-1]

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
        if self._committed_phase is not None:
            variant = self._committed_variants.get(anchor.movement_tick_id + 1)
            return variant is not None and verified_start_state_matches(
                anchor.physics_state, variant.entry_state)
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
        if self._committed_phase is not None:
            # Pending input is authoritative. Select its post-boundary only
            # after its real receipt is consumed by decide().
            return
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
        if self._committed_phase is not None:
            self._committed_phase = CommittedExecutionPhase.UNRESOLVED
            self.state = VerifiedMotionExecutorState.RECOVERING
            return self._decision(None, None, "BODY_RESPONSIBILITY_UNRESOLVED")
        if self._prepared_handle is not None and self._stream_tracker is not None:
            self.state = VerifiedMotionExecutorState.RECOVERING
            return VerifiedMotionDecision(self.state, None, None, self._command_index, None, None, 1,
                                          "BODY_RESPONSIBILITY_UNRESOLVED")
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
        if self._committed_phase is not None and (
                self._pending.committed_phase != self._committed_phase
                and not (self._pending.committed_phase is CommittedExecutionPhase.EXIT_CHOICE
                         and self._committed_phase is CommittedExecutionPhase.FALLBACK)):
            self.state = VerifiedMotionExecutorState.INPUT_LOST
            return "applied_phase_changed"
        if self._committed_phase is not None and (
                record.requested_first_tick != self._pending.requested_movement_tick
                or record.latest_allowed_first_tick != self._pending.requested_latest_movement_tick
                or record.action.valid_for_ticks != 1
                or record.session != self._candidate.proof.entry_state.session):
            self.state = VerifiedMotionExecutorState.INPUT_LOST
            return "applied_window_or_identity_changed"
        expected_command = self._current_committed_command().movement if (
            self._committed_phase is not None) else self._candidate.proof.commands[
                self._pending.command_index].movement
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
            variant = (self._committed_variants.get(actual_start_tick)
                if self._committed_phase is not None else
                self._candidate.proof.start_variant(actual_start_tick))
            if variant is None:
                self.state = VerifiedMotionExecutorState.INPUT_LOST
                return "unverified_start_tick"
            self._start_tick = actual_start_tick
            self._start_variant = variant
        self._command_index += 1
        if self._committed_phase in {CommittedExecutionPhase.FALLBACK,
                                    CommittedExecutionPhase.WITNESS}:
            self._tail_cursor += 1
            self._release_sequence = self._pending.control_sequence
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

    @property
    def committed_execution(self) -> bool:
        return self._committed_phase is not None

    @property
    def committed_exit_choice(self) -> bool:
        return (self._committed_phase is CommittedExecutionPhase.EXIT_CHOICE
                and not self._cancel_requested and self._pending is None)

    def incumbent_fallback(self, action, anchor, ledger, *, owner_id, changed_cells):
        if (self._committed_phase is not CommittedExecutionPhase.EXIT_CHOICE
                or self._pending is not None or self._tail_cursor != 0
                or self._selected_tail is not self._start_variant.fallback_tails[-1]
                or anchor.movement_tick_id + 1 != self._expected_tick()
                or ledger.latest_movement_tick_id != anchor.movement_tick_id
                or action.observation_sequence_id != anchor.observation_sequence_id
                or set(changed_cells).intersection(self._candidate.proof.world_dependencies)
                or not verified_start_state_matches(anchor.physics_state,
                    self._selected_tail.trajectory[0])):
            return None
        command = self._current_committed_command()
        delta = math.atan2(math.sin(command.required_movement_yaw_radians - anchor.physics_state.yaw_radians),
                           math.cos(command.required_movement_yaw_radians - anchor.physics_state.yaw_radians))
        final = replace(action, movement=command.movement,
                        look=LookV1(math.degrees(delta), 0.), valid_for_ticks=1)
        self._runtime_fallback = IncumbentFallbackDispatch(final,
            MovementTickWindowV1(self._expected_tick(), self._expected_tick()),
            self._command_index, owner_id, self)
        return self._runtime_fallback

    def cached_incumbent_fallback(self, action):
        cached = self._runtime_fallback
        if cached is None:
            return None
        old = cached.action
        return old if ((old.episode_id, old.request_sequence_id, old.observation_sequence_id,
                        old.deadline_monotonic_ns) ==
                       (action.episode_id, action.request_sequence_id, action.observation_sequence_id,
                        action.deadline_monotonic_ns)) else None

    def matches_successor_handle(self, handle) -> bool:
        from mc2p.motion_nav.motion_chain import SuccessorPreparationReference
        binding = self._candidate.proof.successor_binding
        suffix = handle.hot_view.stream.suffix_identity
        if (not self.committed_exit_choice or binding is None or len(suffix) != 1
                or type(suffix[0]) is not SuccessorPreparationReference):
            return False
        reference = suffix[0]
        return (all(getattr(reference, name) == getattr(binding, name) for name in (
            "parent_request_digest", "parent_main_digest", "exit_fallback_digest", "input_digest",
            "entry_state", "exit_state", "requested_window", "ruleset", "input_projection_version",
            "dependency_versions", "parent_dependency_versions", "damage_remaining_points"))
            and reference.scan_budget == binding.spec.scan_budget
            and handle.hot_view.commands == binding.spec.commands)

    def _current_committed_command(self):
        if self._committed_phase in {CommittedExecutionPhase.MAIN,
                                    CommittedExecutionPhase.COMMITTED_SUFFIX}:
            return self._candidate.proof.commands[self._command_index]
        return self._tail_command_views[id(self._selected_tail)][self._tail_cursor]

    def _committed_expected_state(self):
        if self._selected_tail is None or self._committed_phase in {
                CommittedExecutionPhase.MAIN, CommittedExecutionPhase.COMMITTED_SUFFIX}:
            return self._start_variant.trajectory[self._command_index]
        if self._tail_cursor < len(self._selected_tail.trajectory):
            return self._selected_tail.trajectory[self._tail_cursor]
        return self._selected_tail.release_witness_state

    def exact_release_ready(self, anchor, ledger, *, changed_cells=()) -> bool:
        if (self._committed_phase is not CommittedExecutionPhase.RELEASED
                or self._selected_tail is None or self._pending is not None
                or set(changed_cells).intersection(self._candidate.proof.world_dependencies)):
            return False
        expected = self._committed_expected_state()
        if not verified_start_state_matches(anchor.physics_state, expected):
            return False
        record = ledger.record(self._release_sequence)
        return (record is not None and record.status is InputApplicationStatus.APPLIED
                and record.session == anchor.session
                and record.applied_ticks == (expected.movement_tick_id,))

    def retired_release_still_current(self, anchor, ledger, *, confirmed_tick):
        """Read an already completed witness for the sole source-cleanup call.

        One neutral Runtime receipt may stand the body up after route retirement.
        It does not re-authorize this controller or manufacture another witness.
        """
        if (self._committed_phase is not CommittedExecutionPhase.RELEASED
                or self._pending is not None or self._selected_tail is None):
            return False
        witness = self._committed_expected_state()
        if confirmed_tick != witness.movement_tick_id or not (
                confirmed_tick <= anchor.movement_tick_id <= confirmed_tick + 1):
            return False
        record = ledger.record(self._release_sequence)
        if (record is None or record.status is not InputApplicationStatus.APPLIED
                or record.session != anchor.session or record.applied_ticks != (confirmed_tick,)):
            return False
        if anchor.movement_tick_id == confirmed_tick:
            return verified_start_state_matches(anchor.physics_state, witness)
        sample = ledger.sample(confirmed_tick + 1)
        next_record = (None if sample is None or sample.request_sequence_id is None
                       else ledger.record(sample.request_sequence_id))
        if (next_record is None or next_record.status is not InputApplicationStatus.APPLIED
                or next_record.applied_ticks != (confirmed_tick + 1,)
                or next_record.action.movement != MovementV1()
                or next_record.action.look != LookV1()
                or anchor.physics_state.pose not in {witness.pose, self._start_variant.entry_state.pose}):
            return False
        height = (witness.body_height if anchor.physics_state.pose == witness.pose
                  else self._start_variant.entry_state.body_height)
        return verified_start_state_matches(anchor.physics_state,
            replace(witness, movement_tick_id=anchor.movement_tick_id,
                    pose=anchor.physics_state.pose, body_height=height))

    def _committed_unresolved(self, reason):
        self._committed_phase = CommittedExecutionPhase.UNRESOLVED
        self.state = VerifiedMotionExecutorState.RECOVERING
        return self._decision(None, None, reason)

    def _decide_committed(self, anchor, ledger, changed_cells):
        proof = self._candidate.proof
        if set(changed_cells).intersection(proof.world_dependencies):
            return self._committed_unresolved("committed_world_dependency_changed")
        if self._committed_phase is CommittedExecutionPhase.UNRESOLVED:
            return self._decision(None, None, "BODY_RESPONSIBILITY_UNRESOLVED")
        if (anchor.session != proof.entry_state.session
                or anchor.ruleset_id != proof.ruleset_id
                or anchor.state_schema != proof.entry_state.state_schema
                or anchor.input_projection_version != proof.input_projection_version):
            return self._committed_unresolved("committed_state_identity_changed")
        pending = self._consume_pending(ledger)
        if pending is not None:
            if (self.state is VerifiedMotionExecutorState.INPUT_LOST
                    or (self._pending is not None and anchor.movement_tick_id
                        >= self._pending.requested_latest_movement_tick)):
                return self._committed_unresolved("committed_" + pending)
            return self._decision(None, None, pending)
        if not verified_start_state_matches(
                anchor.physics_state, self._committed_expected_state()):
            return self._committed_unresolved("committed_post_state_changed")
        if self._committed_phase is CommittedExecutionPhase.RELEASED:
            return self._decision(None, None, "committed_fallback_released")
        if self._selected_tail is None and (self._cancel_requested
                or self._command_index == len(proof.commands)):
            self._selected_tail = self._start_variant.fallback_tails[self._command_index]
            self._tail_cursor = 0
            self._committed_phase = (
                CommittedExecutionPhase.COMMITTED_SUFFIX
                if self._command_index < self._selected_tail.recovery_boundary_index
                else (CommittedExecutionPhase.EXIT_CHOICE
                    if not self._cancel_requested else CommittedExecutionPhase.FALLBACK))
        if (self._committed_phase is CommittedExecutionPhase.COMMITTED_SUFFIX
                and self._command_index == self._selected_tail.recovery_boundary_index):
            self._committed_phase = CommittedExecutionPhase.FALLBACK
        if self._selected_tail is not None and self._committed_phase in {
                CommittedExecutionPhase.FALLBACK, CommittedExecutionPhase.WITNESS}:
            view = self._tail_command_views[id(self._selected_tail)]
            if self._tail_cursor == len(view):
                self._committed_phase = CommittedExecutionPhase.RELEASED
                self.state = VerifiedMotionExecutorState.INPUT_LOST
                return self._decision(None, None, "committed_fallback_released")
            if self._tail_cursor == len(self._selected_tail.inputs):
                self._committed_phase = CommittedExecutionPhase.WITNESS
        if anchor.movement_tick_id + 1 != self._expected_tick():
            return self._committed_unresolved("committed_command_window_expired")
        command = self._current_committed_command()
        return self._decision(command.movement, command.required_movement_yaw_radians,
            "committed_" + self._committed_phase.value,
            submittable_as_verified_command=True)

    def decide(self, anchor: StateAnchor,
               ledger: InputApplicationLedger, *,
               changed_cells: tuple[BlockPos, ...] = ()) -> VerifiedMotionDecision:
        if (type(anchor) is not StateAnchor
                or type(ledger) is not InputApplicationLedger
                or type(changed_cells) is not tuple):
            raise ContractViolation("verified decision requires anchor and input ledger")
        if self._prepared_handle is not None:
            if any(position in changed_cells for position, _ in self._prepared_handle.dependency_versions):
                self.state = VerifiedMotionExecutorState.RECOVERING
            return self.prepared_decision(anchor)
        if self._candidate is None:
            self.state = VerifiedMotionExecutorState.IDLE
            return self._decision(None, None, "not_started")
        if self._committed_phase is not None:
            return self._decide_committed(anchor, ledger, changed_cells)
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
                    and verified_start_state_matches(
                        anchor.physics_state, variant.entry_state,
                    )
                    and _proved_prelude_applied(proof, anchor, ledger)):
                # An unselected first intent has not started the action. Reuse
                # only a published later start whose real neutral prelude matches.
                self._start_tick = variant.start_tick
                self._start_variant = variant
        if (self._command_index == 0 and self._pending is None
                and self._start_variant is not None
                and (not verified_start_state_matches(
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
