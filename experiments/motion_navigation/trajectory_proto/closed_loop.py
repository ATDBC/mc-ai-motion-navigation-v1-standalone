"""Isolated D098 P3 rolling proof owner and experimental input ledger.

This module deliberately does not import Runtime, NavigationSession or the
client ledger.  It exercises the project physics and P2 proof in a deterministic
closed loop so that drift invalidation and owner lifetime can be measured before
any production integration is authorized.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
import math
import time

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.motion_nav.geometry import query_support, required_cells_for_sweep
from mc2p.motion_nav.movement_transition import GoalSupport, MovementMode, ResourceState
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState, TickInput
from mc2p.motion_nav.world_model import CellFact, CellKnowledge, WorldView

from .contracts import (
    ApplicationEvidence,
    KnownInputApplication,
    SearchReason,
    TimingBranch,
    TrajectorySearchRequest,
)
from .decision import DecisionContext, FrozenInputAdapter
from .incremental_proof import (
    ExecutionCheck,
    Permit,
    PermitStatus,
    RollingProofStatus,
    check_execution,
    start_rolling_proof,
    validate_permit,
)
from .reference_search import _boundary_evidence
from .commitment import support_is_safe
from .rollout_trace import RolloutFailure, build_controller_trace
from .physics import CountLimit


STATE_TOLERANCE = 1e-9
MAX_CONSECUTIVE_REPROOFS = 3
_CONTINUOUS_STATE_FIELDS = frozenset({
    "position", "velocity_blocks_per_tick", "yaw_radians", "pitch_radians",
    "body_width", "body_height", "fall_distance_blocks", "movement_speed_attribute",
    "step_height_blocks", "gravity_attribute", "jump_strength_attribute",
    "saturation_points",
})


class ApplicationStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    MISSING = "missing"


class ActionPhase(StrEnum):
    PRE_COMMIT = "pre_commit"
    COMMIT_BOUNDARY = "commit_boundary"
    POST_COMMIT = "post_commit"


class LoopStatus(StrEnum):
    ACTIVE = "active"
    WAITING_APPLICATION = "waiting_application"
    STOPPING = "stopping"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BOUNDED_FAILURE = "bounded_failure"
    BODY_RESPONSIBILITY_UNRESOLVED = "body_responsibility_unresolved"


class AdvanceSource(StrEnum):
    APPLIED_COMMAND = "applied_command"
    TIMING_BRANCH = "timing_branch"


class SubmissionGateStatus(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"


class SubmissionGateReason(StrEnum):
    TIMING_BRANCH_MISSING = "timing_branch_missing"
    LANDING_RECOVERY_NOT_PROVED = "landing_recovery_not_proved"
    PERMIT_INVALID = "permit_invalid"
    COMMAND_NOT_PERMITTED = "command_not_permitted"
    CANDIDATE_TERMINAL_GOAL_MISMATCH = "candidate_terminal_goal_mismatch"


@dataclass(frozen=True, slots=True)
class StateComparison:
    matches: bool
    field: str | None = None
    expected: object | None = None
    observed: object | None = None


def _compare_value(expected, observed, path: str, *, continuous=False) -> StateComparison:
    if type(expected) in (float, int) and type(observed) in (float, int):
        if continuous:
            ok = math.isclose(float(expected), float(observed), rel_tol=0., abs_tol=STATE_TOLERANCE)
        else:
            ok = type(expected) is type(observed) and expected == observed
        return StateComparison(ok, None if ok else path, expected, observed)
    if type(expected) is tuple and type(observed) is tuple and len(expected) == len(observed):
        for index, (left, right) in enumerate(zip(expected, observed)):
            result = _compare_value(left, right, f"{path}[{index}]", continuous=continuous)
            if not result.matches:
                return result
        return StateComparison(True)
    ok = type(expected) is type(observed) and expected == observed
    return StateComparison(ok, None if ok else path, expected, observed)


def compare_states(expected: PhysicsState, observed: PhysicsState) -> StateComparison:
    if type(expected) is not PhysicsState or type(observed) is not PhysicsState:
        raise ContractViolation("state comparison requires typed physics states")
    for field in fields(PhysicsState):
        result = _compare_value(
            getattr(expected, field.name), getattr(observed, field.name), field.name,
            continuous=field.name in _CONTINUOUS_STATE_FIELDS,
        )
        if not result.matches:
            return result
    return StateComparison(True)


def terminal_goal_check(request: TrajectorySearchRequest, state: PhysicsState,
                        world: PhysicsWorldView) -> TerminalGoalCheck:
    """Evaluate one terminal state against GoalState using public geometry facts."""
    cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
    facts = {position: world.cell(position) for position in cells}
    detached = WorldView.detached(world.session, world.geometry_revision, 0, facts)
    support = query_support(state.body_box, detached)
    if support.missing_cells:
        return TerminalGoalCheck(False, support.missing_cells)
    mode = (MovementMode.CRAWL if state.pose == "swimming" else
            MovementMode.CROUCH if state.sneaking else
            MovementMode.SPRINT if state.sprinting else MovementMode.WALK)
    accepted = support_is_safe(state, support) and request.goal.accepts(
        position=state.position,
        support=GoalSupport.SOLID,
        mode=mode,
        pose=state.pose,
        speed_blocks_per_second=20. * math.hypot(
            state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]),
        resources=ResourceState((("food_points", float(state.food_points)),)),
        yaw_radians=state.yaw_radians,
    )
    speed = 20. * math.hypot(
        state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
    accepted = accepted and speed + 1.e-12 >= request.minimum_terminal_speed_blocks_per_second
    return TerminalGoalCheck(bool(accepted))


def _landing_recovery_proved(request: TrajectorySearchRequest, state: PhysicsState,
                             world: PhysicsWorldView) -> bool:
    region = request.goal.region
    x, y, z = state.position
    if not (region.min_x <= x <= region.max_x
            and region.min_y <= y <= region.max_y
            and region.min_z <= z <= region.max_z):
        return False
    cells = required_cells_for_sweep(state.body_box, (0., -.05, 0.))
    facts = {position: world.cell(position) for position in cells}
    detached = WorldView.detached(world.session, world.geometry_revision, 0, facts)
    support = query_support(state.body_box, detached)
    return not support.missing_cells and support_is_safe(state, support)


@dataclass(frozen=True, slots=True)
class ExperimentalSubmission:
    control_sequence: int
    request_tick: int
    allowed_effect_ticks: tuple[int, ...]
    command: TickInput

    def __post_init__(self):
        require_nonnegative_int(self.control_sequence, "control sequence")
        require_nonnegative_int(self.request_tick, "request tick")
        if (type(self.allowed_effect_ticks) is not tuple or not self.allowed_effect_ticks
                or any(type(value) is not int for value in self.allowed_effect_ticks)
                or tuple(sorted(set(self.allowed_effect_ticks))) != self.allowed_effect_ticks
                or self.allowed_effect_ticks[0] <= self.request_tick):
            raise ContractViolation("submission requires a future ordered effect window")
        if type(self.command) is not TickInput:
            raise ContractViolation("submission requires a typed tick input")


@dataclass(frozen=True, slots=True)
class ExperimentalApplication:
    control_sequence: int
    effect_tick: int
    command: TickInput


@dataclass(frozen=True, slots=True)
class ApplicationCheck:
    status: ApplicationStatus
    submission: ExperimentalSubmission
    application: ExperimentalApplication | None = None


@dataclass(frozen=True, slots=True)
class SubmissionGateCheck:
    status: SubmissionGateStatus
    reason: SubmissionGateReason | None = None
    submission: ExperimentalSubmission | None = None


@dataclass(frozen=True, slots=True)
class TerminalGoalCheck:
    accepted: bool
    missing_cells: tuple = ()


_ADVANCE_ISSUER_SEAL = object()


class ExperimentalInputLedger:
    """P3-only record of submitted and actually applied commands."""

    def __init__(self, session, ledger_id: str):
        require_identifier(ledger_id, "experimental ledger id")
        self.session = session
        self.ledger_id = ledger_id
        self._submissions: dict[int, ExperimentalSubmission] = {}
        self._applications: dict[int, ExperimentalApplication] = {}
        self._closed_through_tick = -1

    def submit(self, control_sequence: int, request_tick: int,
               allowed_effect_ticks: tuple[int, ...], command: TickInput) -> ExperimentalSubmission:
        if control_sequence in self._submissions:
            raise ContractViolation("control sequence was already submitted")
        value = ExperimentalSubmission(control_sequence, request_tick, allowed_effect_ticks, command)
        self._submissions[control_sequence] = value
        return value

    def record_applied(self, control_sequence: int, effect_tick: int, command: TickInput) -> None:
        submission = self._submissions.get(control_sequence)
        if submission is None:
            raise ContractViolation("actual application has no submitted command")
        if control_sequence in self._applications:
            raise ContractViolation("control sequence already has actual application")
        if effect_tick not in submission.allowed_effect_ticks:
            raise ContractViolation("actual application falls outside allowed effect window")
        if command != submission.command:
            raise ContractViolation("actual application command differs from submission")
        self._applications[control_sequence] = ExperimentalApplication(
            control_sequence, effect_tick, command)

    def status(self, control_sequence: int, observed_tick: int) -> ApplicationCheck:
        submission = self._submissions.get(control_sequence)
        if submission is None:
            raise ContractViolation("application status requires a submitted command")
        application = self._applications.get(control_sequence)
        if application is not None:
            return ApplicationCheck(ApplicationStatus.APPLIED, submission, application)
        if (observed_tick > submission.allowed_effect_ticks[-1]
                or self._closed_through_tick >= submission.allowed_effect_ticks[-1]):
            return ApplicationCheck(ApplicationStatus.MISSING, submission)
        return ApplicationCheck(ApplicationStatus.PENDING, submission)

    def close_application_tick(self, tick: int) -> None:
        require_nonnegative_int(tick, "closed application tick")
        self._closed_through_tick = max(self._closed_through_tick, tick)

    @property
    def submission_count(self) -> int:
        return len(self._submissions)


@dataclass(frozen=True, slots=True)
class AuthorizedAdvance:
    owner_id: str
    ledger_id: str
    control_sequence: int
    effect_tick: int
    command: TickInput
    source: AdvanceSource
    permit_boundary: int
    proof_request_id: str
    _issuer_seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        require_identifier(self.owner_id, "advance owner id")
        require_identifier(self.ledger_id, "advance ledger id")
        require_nonnegative_int(self.control_sequence, "advance control sequence")
        require_nonnegative_int(self.effect_tick, "advance effect tick")
        if type(self.command) is not TickInput:
            raise ContractViolation("authorized advance requires a typed tick input")
        if type(self.source) is not AdvanceSource:
            raise ContractViolation("authorized advance requires a typed source")
        require_nonnegative_int(self.permit_boundary, "advance permit boundary")
        require_identifier(self.proof_request_id, "advance proof request id")
        if self._issuer_seal is not _ADVANCE_ISSUER_SEAL:
            # Construction remains public for serialization and review, but an
            # arbitrary instance is not executable authority.
            return


@dataclass(frozen=True, slots=True)
class FrozenInputPlan:
    inputs: tuple[TickInput, ...]

    def __post_init__(self):
        if type(self.inputs) is not tuple or not self.inputs:
            raise ContractViolation("frozen input plan requires commands")

    def adapter(self, consumed: int):
        remaining = self.inputs[consumed:]
        if not remaining:
            remaining = (self.inputs[-1],)
        return FrozenInputAdapter(remaining)


@dataclass(frozen=True, slots=True)
class ProofBundle:
    request: TrajectorySearchRequest
    world: PhysicsWorldView
    inputs: tuple[TickInput, ...]
    proof: object
    trace: object
    total_ns: int
    physics_steps: int


@dataclass(frozen=True, slots=True)
class ProofAttempt:
    bundle: ProofBundle | None
    total_ns: int
    physics_steps: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LoopDecision:
    status: LoopStatus
    phase: ActionPhase
    reason: str | None
    submission: ExperimentalSubmission | None
    consumed_commands: int
    reproved: bool
    reproof_count: int
    old_suffix_invalidated: bool
    owner_retained: bool
    complete_decision_ns: int
    physics_steps: int
    safety_violations: tuple[str, ...] = ()
    recovery_proof_obtained: bool = False


def _simulate_step(state: PhysicsState, command: TickInput,
                    world: PhysicsWorldView) -> PhysicsState:
    result = step(state, command, world, JAVA_1_21_RULESET)
    if result.next_state is None:
        raise ContractViolation(f"experimental backend step incomplete: {result.status}")
    return result.next_state


def advance_backend(state: PhysicsState, authority: AuthorizedAdvance,
                    world: PhysicsWorldView) -> PhysicsState:
    """Advance the experimental client only with traceable input authority."""
    if type(authority) is not AuthorizedAdvance:
        raise ContractViolation("experimental backend requires an authorized advance")
    if authority._issuer_seal is not _ADVANCE_ISSUER_SEAL:
        raise ContractViolation("experimental backend requires owner-issued authority")
    return _simulate_step(state, authority.command, world)


def mutate_world_fact(world: PhysicsWorldView, position, *, unknown=False,
                      fact: CellFact | None = None) -> PhysicsWorldView:
    """Return a detached experimental snapshot with one changed fact."""
    if type(world) is not PhysicsWorldView or not world.is_detached:
        raise ContractViolation("world mutation helper requires a detached snapshot")
    # P3 fixtures use the frozen P0 box.  Rebuild it only through the public
    # cell API; the loop must not inspect a snapshot's private backing store.
    facts = {}
    for x in range(-12, 13):
        for y in range(-50, 8):
            for z in range(-14, 18):
                known = world.cell((x, y, z))
                if known.knowledge is not CellKnowledge.UNKNOWN:
                    facts[(x, y, z)] = known
    if unknown:
        facts.pop(position, None)
    elif fact is None:
        raise ContractViolation("known mutation requires a cell fact")
    else:
        facts[position] = fact
    detached = WorldView.detached(world.session, world.geometry_revision, 0, facts)
    return PhysicsWorldView(detached, world.ruleset)


def _trace_reaches_goal(request, trace, world) -> tuple[bool, str | None]:
    if len(trace.branch_states) != len(request.timing_branches):
        return False, SubmissionGateReason.TIMING_BRANCH_MISSING.value
    for states in trace.branch_states:
        check = terminal_goal_check(request, states[-1], world)
        if not check.accepted:
            return False, SubmissionGateReason.CANDIDATE_TERMINAL_GOAL_MISMATCH.value
    return True, None


def _proof_bundle(request, world, adapter, runahead_steps) -> ProofAttempt:
    started = time.perf_counter_ns()
    context = DecisionContext.create(request)
    try:
        resolved = adapter.resolve(context, request, world)
        trace = build_controller_trace(request, resolved.inputs, world, counter=context.counter)
        reaches_goal, goal_reason = _trace_reaches_goal(request, trace, world)
        if not reaches_goal:
            return ProofAttempt(None, time.perf_counter_ns() - started,
                                context.counter.physics_steps, goal_reason)
        proof = start_rolling_proof(
            request, resolved.inputs, world, _boundary_evidence(request, resolved.inputs),
            counter=context.counter, rollout_trace=trace, runahead_steps=runahead_steps,
        )
        first = proof.advance()
    except (RolloutFailure, CountLimit) as failure:
        reason = getattr(failure, "reason", None)
        return ProofAttempt(None, time.perf_counter_ns() - started,
                            context.counter.physics_steps, _reason_value(reason))
    elapsed = time.perf_counter_ns() - started
    if first.status is RollingProofStatus.REJECTED:
        return ProofAttempt(None, elapsed, context.counter.physics_steps,
                            _reason_value(first.reason))
    reaches_goal, goal_reason = _trace_reaches_goal(request, trace, world)
    if not reaches_goal:
        return ProofAttempt(None, time.perf_counter_ns() - started,
                            context.counter.physics_steps, goal_reason)
    return ProofAttempt(
        ProofBundle(request, world, resolved.inputs, proof, trace,
                    elapsed, context.counter.physics_steps),
        elapsed, context.counter.physics_steps,
    )


def _reason_value(reason) -> str:
    return reason.value if hasattr(reason, "value") else str(reason)


class RollingProofOwner:
    """One body owner for an isolated rolling proof action."""

    def __init__(self, bundle: ProofBundle, plan: FrozenInputPlan, runahead_steps: int):
        self.bundle = bundle
        self.external_request = bundle.request
        self.plan = plan
        self.runahead_steps = runahead_steps
        self.ledger = ExperimentalInputLedger(bundle.request.world_session, "p3-ledger-0")
        self.owner_id = f"p3-owner-{bundle.request.request_id}"
        self.consumed = 0
        self.next_sequence = bundle.request.first_candidate_control_sequence
        self.pending: ExperimentalSubmission | None = None
        self.pending_from: PhysicsState | None = None
        self.pending_world: PhysicsWorldView | None = None
        self.last_applied_command: TickInput | None = None
        self.consecutive_reproofs = 0
        self.owner_retained = True
        self.old_suffix_invalidated = False
        self.commit_started = False
        self.cancel_requested = False
        self.recovery_proof_obtained = False
        self.advance_authorities: list[AuthorizedAdvance] = []
        self._submission_boundaries: dict[int, tuple[int, ActionPhase, str]] = {}
        self.terminal: LoopDecision | None = None

    @classmethod
    def start(cls, request, world, plan, *, runahead_steps=24):
        if type(plan) is not FrozenInputPlan:
            raise ContractViolation("rolling owner requires an experimental input plan")
        attempt = _proof_bundle(request, world, plan.adapter(0), runahead_steps)
        if attempt.bundle is None:
            raise ContractViolation("initial rolling proof was not verified")
        return cls(attempt.bundle, plan, runahead_steps)

    @property
    def current_permit(self) -> Permit:
        permit = self._permit_for(self.consumed)
        if permit is None:
            raise ContractViolation("no permit covers the current command")
        return permit

    def _permit_for(self, boundary: int) -> Permit | None:
        for permit in self.bundle.proof.permits:
            if permit.boundary <= boundary < permit.boundary + len(permit.commands):
                return permit
        return None

    def _advance_proof(self) -> tuple[int, int, str | None]:
        before = self.bundle.proof.counts.physics_steps
        started = time.perf_counter_ns()
        result = self.bundle.proof.advance()
        elapsed = time.perf_counter_ns() - started
        steps = self.bundle.proof.counts.physics_steps - before
        if result.status is RollingProofStatus.REJECTED:
            return elapsed, steps, _reason_value(result.reason)
        return elapsed, steps, None

    def _commit_range(self):
        commits = [permit for permit in self.bundle.proof.permits if permit.locked_count > 0]
        if not commits:
            return None
        permit = commits[0]
        return permit.boundary, permit.boundary + permit.locked_count

    def phase(self) -> ActionPhase:
        return self.phase_at(self.consumed)

    def phase_at(self, boundary: int) -> ActionPhase:
        bounds = self._commit_range()
        if bounds is None or boundary < bounds[0]:
            return ActionPhase.PRE_COMMIT
        if boundary == bounds[0]:
            return ActionPhase.COMMIT_BOUNDARY
        return ActionPhase.POST_COMMIT

    def record_applied(self, control_sequence: int, effect_tick: int, command: TickInput):
        self.ledger.record_applied(control_sequence, effect_tick, command)

    def submission_gate(self, proof, permit: Permit, command: TickInput, boundary: int,
                        request: TrajectorySearchRequest,
                        world: PhysicsWorldView) -> SubmissionGateCheck:
        """Single gate for normal and mutation submissions."""
        branches = getattr(proof, "submission_branches", None)
        if branches is None:
            branches = getattr(proof, "branches", ())
        expected_timing = {branch.value for branch in request.timing_branches}
        actual_timing = {branch.timing_branch.value for branch in branches}
        if actual_timing != expected_timing:
            return SubmissionGateCheck(
                SubmissionGateStatus.BLOCKED,
                SubmissionGateReason.TIMING_BRANCH_MISSING,
            )
        for branch in branches:
            if (not branch.states
                    or not terminal_goal_check(request, branch.states[-1], world).accepted):
                return SubmissionGateCheck(
                    SubmissionGateStatus.BLOCKED,
                    SubmissionGateReason.CANDIDATE_TERMINAL_GOAL_MISMATCH,
                )
            for interval in branch.risk_intervals:
                if (interval.recovered_boundary >= len(branch.states)
                        or not _landing_recovery_proved(
                            request, branch.states[interval.recovered_boundary], world)):
                    return SubmissionGateCheck(
                        SubmissionGateStatus.BLOCKED,
                        SubmissionGateReason.LANDING_RECOVERY_NOT_PROVED,
                    )
        if validate_permit(permit, request, world).status is not PermitStatus.VALID:
            return SubmissionGateCheck(
                SubmissionGateStatus.BLOCKED,
                SubmissionGateReason.PERMIT_INVALID,
            )
        if check_execution(permit, command, boundary) is not ExecutionCheck.ALLOWED:
            return SubmissionGateCheck(
                SubmissionGateStatus.BLOCKED,
                SubmissionGateReason.COMMAND_NOT_PERMITTED,
            )
        return SubmissionGateCheck(SubmissionGateStatus.ALLOWED)

    def submit_through_gate(
        self,
        *,
        proof,
        permit: Permit,
        command: TickInput,
        boundary: int,
        request_tick: int,
        allowed_effect_ticks: tuple[int, ...],
        request: TrajectorySearchRequest,
        world: PhysicsWorldView,
    ) -> SubmissionGateCheck:
        check = self.submission_gate(proof, permit, command, boundary, request, world)
        if check.status is SubmissionGateStatus.BLOCKED:
            return check
        sequence = self.next_sequence
        self.next_sequence += 1
        submission = self.ledger.submit(
            sequence, request_tick, allowed_effect_ticks, command)
        self._submission_boundaries[sequence] = (
            boundary, self.phase_at(boundary), self.bundle.request.request_id)
        return replace(check, submission=submission)

    def authorize_application(self, submission: ExperimentalSubmission,
                              effect_tick: int) -> AuthorizedAdvance:
        if submission is not self.pending:
            raise ContractViolation("application authority requires the pending submission")
        metadata = self._submission_boundaries.get(submission.control_sequence)
        if metadata is None:
            raise ContractViolation("application authority lacks permit provenance")
        boundary, phase, proof_request_id = metadata
        permit = self._permit_for(boundary)
        if permit is None or check_execution(
                permit, submission.command, boundary) is not ExecutionCheck.ALLOWED:
            raise ContractViolation("application authority lacks a valid permit")
        self.ledger.record_applied(submission.control_sequence, effect_tick, submission.command)
        if phase is ActionPhase.COMMIT_BOUNDARY:
            self.commit_started = True
        authority = AuthorizedAdvance(
            self.owner_id, self.ledger.ledger_id, submission.control_sequence,
            effect_tick, submission.command, AdvanceSource.APPLIED_COMMAND,
            boundary, proof_request_id, _ADVANCE_ISSUER_SEAL,
        )
        self.advance_authorities.append(authority)
        return authority

    def authorize_pending_hold(self, effect_tick: int) -> AuthorizedAdvance:
        if self.pending is None:
            raise ContractViolation("pending hold requires a pending submission")
        check = self.ledger.status(self.pending.control_sequence, effect_tick - 1)
        if check.status is not ApplicationStatus.PENDING:
            raise ContractViolation("pending hold requires an open application window")
        if self.last_applied_command is not None:
            command = self.last_applied_command
            sequence = max(0, self.pending.control_sequence - 1)
            source = AdvanceSource.APPLIED_COMMAND
        else:
            branches = self.bundle.request.branch_preludes
            covered = [application for branch in branches for application in branch
                       if application.effect_tick == effect_tick]
            if covered:
                command = covered[0].tick_input
                sequence = covered[0].control_sequence
            elif effect_tick in self.pending.allowed_effect_ticks:
                # At the last permitted effect tick the client may already hold
                # the in-flight submitted command even though its application
                # receipt has not yet reached the owner.  This is the candidate
                # branch covered by the proof, not an invented neutral input.
                command = self.pending.command
                sequence = self.pending.control_sequence
            else:
                raise ContractViolation("pending hold lacks a proved timing branch")
            source = AdvanceSource.TIMING_BRANCH
        authority = AuthorizedAdvance(
            self.owner_id, self.ledger.ledger_id, sequence, effect_tick, command,
            source, self.consumed, self.bundle.request.request_id,
            _ADVANCE_ISSUER_SEAL,
        )
        self.advance_authorities.append(authority)
        return authority

    def _reanchored_request(self, source: TrajectorySearchRequest, observed: PhysicsState,
                            world: PhysicsWorldView) -> TrajectorySearchRequest:
        waiting_input = self.last_applied_command or source.stop_input
        waiting = _simulate_step(observed, waiting_input, world)
        sequence = self.next_sequence
        prelude = KnownInputApplication(
            source.world_session, max(0, sequence - 1), observed.movement_tick_id + 1,
            waiting_input, f"p3-wait-{sequence}", ApplicationEvidence.PREDICTED,
        )
        return replace(
            source,
            request_id=f"{source.request_id}-p3-{observed.movement_tick_id}-{self.consecutive_reproofs + 1}",
            entry_states=(observed, waiting),
            geometry_revision=world.geometry_revision,
            anchor_id=f"p3-anchor-{observed.movement_tick_id}-{self.consecutive_reproofs + 1}",
            input_ledger_id=f"p3-ledger-{self.consecutive_reproofs + 1}",
            allowed_effect_ticks=(observed.movement_tick_id + 1, observed.movement_tick_id + 2),
            branch_preludes=((), (prelude,)),
            first_candidate_control_sequence=sequence,
            input_prefix=(),
        )

    def _reprove(self, observed, world, request) -> tuple[bool, int, int, str | None]:
        if self.consecutive_reproofs >= MAX_CONSECUTIVE_REPROOFS:
            return False, 0, 0, "reproof_limit"
        rebound = self._reanchored_request(request, observed, world)
        attempt = _proof_bundle(rebound, world, self.plan.adapter(self.consumed), self.runahead_steps)
        self.consecutive_reproofs += 1
        if attempt.bundle is None:
            # A direct incremental call would expose a finer reason, but the owner must
            # still stop in this same decision.  UNKNOWN is detected before this path.
            return (False, attempt.total_ns, attempt.physics_steps,
                    attempt.reason or "proof_unavailable")
        bundle = attempt.bundle
        self.bundle = bundle
        self.external_request = request
        self.plan = FrozenInputPlan(self.plan.inputs[self.consumed:])
        self.consumed = 0
        self.ledger = ExperimentalInputLedger(rebound.world_session, rebound.input_ledger_id)
        return True, bundle.total_ns, bundle.physics_steps, None

    def _failure(self, phase, reason, *, started, elapsed=0, steps=0,
                 invalidated=False):
        unresolved = invalidated and phase is not ActionPhase.PRE_COMMIT
        status = (LoopStatus.BODY_RESPONSIBILITY_UNRESOLVED if unresolved
                  else LoopStatus.BOUNDED_FAILURE)
        violations = (("body_responsibility_unresolved",) if unresolved else ())
        decision = LoopDecision(
            status, phase, reason, None, self.consumed,
            elapsed > 0, self.consecutive_reproofs, invalidated,
            self.owner_retained, max(1, time.perf_counter_ns() - started), steps,
            violations, False,
        )
        self.terminal = decision
        return decision

    def _consume_pending(self, observed, world, request):
        if self.pending is None:
            return None
        check = self.ledger.status(self.pending.control_sequence, observed.movement_tick_id)
        if check.status is ApplicationStatus.PENDING:
            # The observation can only be advanced with an already-applied input or
            # an in-flight input represented by the frozen timing branch.  The
            # simulator performs that advance before calling decide(); here we only
            # compare it with the same traceable authority.
            if self.last_applied_command is not None:
                waiting_input = self.last_applied_command
            else:
                matching = [application for branch in self.bundle.request.branch_preludes
                            for application in branch
                            if application.effect_tick == observed.movement_tick_id]
                if not matching:
                    return "unproved_pending_input", False
                waiting_input = matching[0].tick_input
            expected = _simulate_step(self.pending_from, waiting_input, self.pending_world)
            comparison = compare_states(expected, observed)
            if not comparison.matches:
                return "state_deviation", False
            self.pending_from = observed
            self.pending_world = world
            return "pending", False
        if check.status is ApplicationStatus.MISSING:
            self.pending = None
            return "missing_application", True
        expected = _simulate_step(self.pending_from, self.pending.command, self.pending_world)
        comparison = compare_states(expected, observed)
        self.pending = None
        moved = any(abs(a - b) > STATE_TOLERANCE
                    for a, b in zip(self.pending_from.position, observed.position))
        self.consumed += 1
        assert check.application is not None
        self.last_applied_command = check.application.command
        if not comparison.matches:
            return "state_deviation", True
        if moved:
            self.consecutive_reproofs = 0
        return "consumed", False

    def decide(self, observed: PhysicsState, world: PhysicsWorldView,
               request: TrajectorySearchRequest, *, cancel=False) -> LoopDecision:
        started = time.perf_counter_ns()
        if self.terminal is not None:
            return self.terminal
        if type(observed) is not PhysicsState or type(world) is not PhysicsWorldView:
            raise ContractViolation("rolling decision requires typed observation and world")
        if cancel:
            self.cancel_requested = True
        pending = self._consume_pending(observed, world, request)
        # Application evidence changes the consumed boundary.  Phase is therefore
        # derived only after the evidence has been ingested.
        phase = self.phase()
        if pending == ("pending", False):
            return LoopDecision(LoopStatus.WAITING_APPLICATION, phase, None, None,
                                self.consumed, False, self.consecutive_reproofs, False,
                                True, max(1, time.perf_counter_ns() - started), 0)

        elapsed = steps = 0
        permit = self._permit_for(self.consumed)
        if permit is None and self.consumed <= len(self.bundle.inputs):
            proof_elapsed, proof_steps, proof_reason = self._advance_proof()
            elapsed += proof_elapsed
            steps += proof_steps
            permit = self._permit_for(self.consumed)
            phase = self.phase()
            if proof_reason is not None:
                return self._failure(phase, proof_reason, started=started,
                                     elapsed=elapsed, steps=steps)
            if self.consumed >= len(self.bundle.inputs) and permit is None:
                self.owner_retained = False
                final_status = (LoopStatus.CANCELLED if self.cancel_requested
                                else LoopStatus.COMPLETED)
                decision = LoopDecision(LoopStatus.COMPLETED, phase, None, None,
                                        self.consumed, False, self.consecutive_reproofs,
                                        False, False, max(1, time.perf_counter_ns() - started), steps)
                if final_status is LoopStatus.CANCELLED:
                    decision = replace(decision, status=final_status, reason="cancelled")
                self.terminal = decision
                return decision

        deviation = None if pending is None or pending[0] == "consumed" else pending[0]
        if pending is None:
            expected_states = self.bundle.trace.branch_states[0]
            if self.consumed < len(expected_states):
                comparison = compare_states(expected_states[self.consumed], observed)
                if not comparison.matches:
                    deviation = "state_deviation"
        invalidated = False
        request_change = None
        previous = self.external_request
        if request.request_id != previous.request_id:
            request_change = SearchReason.REQUEST_IDENTITY.value
        elif request.goal_revision != previous.goal_revision or request.goal != previous.goal:
            request_change = SearchReason.GOAL_REVISION.value
        elif (request.world_session != previous.world_session
              or request.geometry_revision != previous.geometry_revision):
            request_change = SearchReason.WORLD_DEPENDENCY.value
        elif (request.input_ledger_id != previous.input_ledger_id
              or request.first_candidate_control_sequence
              != previous.first_candidate_control_sequence):
            request_change = SearchReason.INPUT_LEDGER.value

        if cancel and not self.commit_started and phase is ActionPhase.PRE_COMMIT:
            self.owner_retained = False
            decision = LoopDecision(LoopStatus.CANCELLED, phase, "cancelled", None,
                                    self.consumed, False, self.consecutive_reproofs,
                                    False, False, max(1, time.perf_counter_ns() - started), 0)
            self.terminal = decision
            return decision
        if cancel:
            # Cancellation after commitment invalidates the task suffix.  The body
            # owner must synchronously obtain a fresh recovery proof before it can
            # submit another command, and the request remains sticky afterwards.
            deviation = "cancelled"
            invalidated = True
        elif self.cancel_requested:
            # A proved recovery suffix remains active on later frames.  Keep
            # STOPPING until its safe release point rather than reopening intent.
            deviation = None
        elif request_change is not None:
            deviation = request_change
        elif permit is None:
            if self.consumed >= len(self.bundle.inputs):
                self.owner_retained = False
                decision = LoopDecision(LoopStatus.COMPLETED, phase, None, None,
                                        self.consumed, False, self.consecutive_reproofs,
                                        False, False, max(1, time.perf_counter_ns() - started), 0)
                self.terminal = decision
                return decision
            deviation = "permit_missing"
        else:
            check = validate_permit(permit, self.bundle.request, world)
            if check.status is PermitStatus.NEEDS_INFORMATION:
                invalidated = phase is not ActionPhase.PRE_COMMIT
                self.old_suffix_invalidated |= invalidated
                return self._failure(phase, _reason_value(check.reason), started=started,
                                     invalidated=invalidated)
            if check.status is PermitStatus.STALE:
                deviation = _reason_value(check.reason)

        if deviation is not None:
            invalidated = invalidated or phase is not ActionPhase.PRE_COMMIT
            self.old_suffix_invalidated |= invalidated
            ok, reproof_elapsed, reproof_steps, reason = self._reprove(
                observed, world, request)
            elapsed += reproof_elapsed
            steps += reproof_steps
            self.recovery_proof_obtained = invalidated and ok
            permit = self._permit_for(0) if ok else None
            if not ok:
                return self._failure(phase, reason or deviation, elapsed=elapsed,
                                     steps=steps, invalidated=invalidated, started=started)
            if permit is None:
                return self._failure(phase, "permit_missing_after_reproof", elapsed=elapsed,
                                     steps=steps, invalidated=invalidated, started=started)

        command = self.bundle.inputs[self.consumed]
        gate = self.submit_through_gate(
            proof=self.bundle.proof,
            permit=permit,
            command=command,
            boundary=self.consumed,
            request_tick=observed.movement_tick_id,
            allowed_effect_ticks=(
                observed.movement_tick_id + 1, observed.movement_tick_id + 2),
            request=self.bundle.request,
            world=world,
        )
        if gate.status is SubmissionGateStatus.BLOCKED:
            return self._failure(
                phase, gate.reason.value, elapsed=elapsed, steps=steps,
                invalidated=invalidated, started=started)
        assert gate.submission is not None
        submission = gate.submission
        self.pending = submission
        self.pending_from = observed
        self.pending_world = world
        status = LoopStatus.STOPPING if self.cancel_requested else LoopStatus.ACTIVE
        return LoopDecision(
            status, phase, deviation, submission, self.consumed, deviation is not None,
            self.consecutive_reproofs, invalidated, True,
            max(1, time.perf_counter_ns() - started), steps, (),
            invalidated and self.recovery_proof_obtained,
        )
