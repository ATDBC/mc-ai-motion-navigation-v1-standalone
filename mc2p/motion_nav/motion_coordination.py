"""B10-C bounded local preparation of a planned JumpGap action."""
from __future__ import annotations

from mc2p.motion_nav.actions.registry import action_spec

from dataclasses import dataclass, replace
from enum import StrEnum
import math
import time

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.motion_nav.async_work import (
    AsyncComputationScope,
    AsyncAdmissionDisposition,
    AsyncAdmissionRecord,
    AsyncWorkIdentity,
    AsyncWorkKind,
    AsyncWorkWindow,
    WorkCheck,
    AsyncOwnerDiagnostics,
)
from mc2p.motion_nav.action_route import (
    JumpGapSegment, JumpUpSegment, WalkSegment,
)
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision, ActionRouteExecutor, ActionRouteState,
)
from mc2p.motion_nav.body_control import BodyControlPhase
from mc2p.motion_nav.motion_candidate import (
    AdmittedMotionCandidate, MotionCandidateStatus,
)
from mc2p.motion_nav.motion_solver import (
    DEFAULT_AIR_TRANSITION_POLICIES, DEFAULT_GAP_SOLVER_POLICY,
    AirTransitionSolveRequest, AirTransitionSolverPolicy,
    GapSolveRequest, GapSolverPolicy, LandingRegion, MotionSolveKind,
    MotionCommandTick, VerifiedMotionResult, SolveResult, SolveStatus, check_motion_entry,
    gap_entry_heading_delta_radians, gap_entry_heading_is_aligned,
    solve_air_transition, solve_one_cell_gap,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.retry_ledger import (
    LocalAttemptChain, LocalAttemptRegistration, LocalAttemptVerdict,
    RetryLedger, WaitPolicy, WaitVerdict,
)
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveJob, GapMotionSolveResult,
    GroundTerminalSolveJob, GroundTerminalSolveResult, MotionResultInbox,
    MotionJobOperation, MotionWorkerCancelStatus, MotionWorkerComputePort,
)
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, InputApplicationLedger, ProjectionStatus,
    StateAnchor, project_movement_command,
)
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds, PhysicsWorldView
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET
from mc2p.motion_nav.route_admission import ActiveRoute, RouteAdmitter
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.safe_ground_control import (
    verified_ground_recovery_movement, verified_ground_rollout,
    verified_ground_target_movement,
)
from mc2p.motion_nav.world_model import Aabb, BlockPos, CellKnowledge, WorldView
from mc2p.motion_nav.ground_terminal_search import (
    GROUND_TERMINAL_BEAM_PREPARATION_TICKS,
    GroundTerminalSearchLimits, GroundTerminalSearchStatus,
    GroundTerminalSolveRequest,
)
from mc2p.motion_nav.segment_entry import MotionContinuationRequirement, SegmentEntryWindow
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.fixed_route import GroundHandoffDisposition


_RESOURCE_ASSUMPTIONS = ("server_hunger_clock_not_in_physics_state",)
_MAX_ENTRY_ALIGNMENT_DEGREES_PER_TICK = 36.0
_GROUNDED_ENTRY_RECOVERY_POLICY = WaitPolicy(40, 2_000_000_000)
_MOTION_SOLVE_LIMIT_NS = 1_000_000_000
_MOTION_SOLVE_LIMIT_TICKS = 20
_GROUND_TERMINAL_FAILURE_REASONS = frozenset({
    "fixed_route_has_no_forward_control",
    "fixed_route_stalled",
    "no_safe_ground_candidate",
})


def route_needs_motion_coordination(route: ActiveRoute) -> bool:
    """Return whether one route needs the shared background motion worker.

    Ordinary Walk uses it only for the final completion-bearing segment.  A
    plain Walk must stay on the synchronous ground path.
    """
    if type(route) is not ActiveRoute:
        raise ContractViolation("motion coordination predicate requires an active route")
    actions = route.action_route.actions
    for index, action in enumerate(actions):
        if action_spec(action).needs_background_solving:
            return True
        if (index == len(actions) - 1 and type(action) is WalkSegment
                and action.fixed_route.execution_contract is not None
                and action.fixed_route.execution_contract.completion_region is not None
                and (action.transition is None
                     or action.transition.mode is MovementMode.WALK)):
            return True
    return False


def _gap_physics_bounds(
        world: PhysicsWorldView, anchor: StateAnchor,
        request: GapSolveRequest | AirTransitionSolveRequest) -> PhysicsWorldBounds:
    """Copy only the collision volume one bounded gap solve can reach."""
    if (type(world) is not PhysicsWorldView or type(anchor) is not StateAnchor
            or type(request) not in {
                GapSolveRequest, AirTransitionSolveRequest}):
        raise ContractViolation("motion physics snapshot requires typed inputs")
    state = anchor.physics_state
    x, y, z = state.position
    half = state.body_width / 2.0
    horizontal_margin = 2.0
    vertical_margin = 2.0
    min_x, max_x = request.landing.min_x, request.landing.max_x
    min_z, max_z = request.landing.min_z, request.landing.max_z
    if request.continuation is not None:
        window = request.continuation.entry_window
        dx, dz = window.horizontal_approach_direction
        corners = tuple((window.reference_point[0] + dx * along - dz * lateral,
                         window.reference_point[2] + dz * along + dx * lateral)
                        for along in (window.minimum_longitudinal_offset_blocks,
                                      window.maximum_longitudinal_offset_blocks)
                        for lateral in (-window.maximum_lateral_offset_blocks,
                                        window.maximum_lateral_offset_blocks))
        min_x, max_x = min(min_x, *(p[0] for p in corners)), max(max_x, *(p[0] for p in corners))
        min_z, max_z = min(min_z, *(p[1] for p in corners)), max(max_z, *(p[1] for p in corners))
    bounds = PhysicsWorldBounds(
        math.floor(min(x - half, min_x) - horizontal_margin),
        math.ceil(max(x + half, max_x) + horizontal_margin) - 1,
        math.floor(min(y, request.landing.surface_y) - vertical_margin),
        math.ceil(max(
            y + state.body_height,
            request.landing.surface_y + state.body_height,
        ) + vertical_margin) - 1,
        math.floor(min(z - half, min_z) - horizontal_margin),
        math.ceil(max(z + half, max_z) + horizontal_margin) - 1,
    )
    return bounds


def _gap_physics_snapshot(world, anchor, request) -> PhysicsWorldView:
    return world.snapshot(_gap_physics_bounds(world, anchor, request))


class GapPreparationStatus(StrEnum):
    READY = "ready"
    UNSUPPORTED_ROUTE_ACTION = "unsupported_route_action"
    SOLVE_FAILED = "solve_failed"
    ADMISSION_REJECTED = "admission_rejected"
    REVALIDATION_REQUIRED = "revalidation_required"


@dataclass(frozen=True, slots=True)
class GapPreparationResult:
    status: GapPreparationStatus
    candidate: AdmittedMotionCandidate | None = None
    solve_result: SolveResult | None = None
    reason: str = ""
    retryable: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not GapPreparationStatus:
            raise ContractViolation("invalid gap preparation status")
        if (self.status is GapPreparationStatus.READY) != (
                type(self.candidate) is AdmittedMotionCandidate):
            raise ContractViolation("ready gap preparation requires admitted motion")


def _air_action_kind(action) -> MotionSolveKind | None:
    return action_spec(action).solve_kind


def _air_action_direction(action) -> tuple[int, int] | None:
    geometry = action_spec(action).solve_geometry
    return None if geometry is None else geometry(action, None).direction


def _air_action_landing(action, anchor: StateAnchor) -> LandingRegion | None:
    geometry = action_spec(action).solve_geometry
    return None if geometry is None else geometry(action, anchor).landing


def _same_landing_region(
        first: LandingRegion, second: LandingRegion, *,
        epsilon: float = 1.0e-7) -> bool:
    """Compare one geometric region without treating float roundoff as a new edge."""
    return all(math.isclose(
        left, right, rel_tol=0.0, abs_tol=epsilon,
    ) for left, right in zip(
        (first.min_x, first.max_x, first.min_z, first.max_z, first.surface_y),
        (second.min_x, second.max_x, second.min_z, second.max_z, second.surface_y),
    ))


def _following_motion_direction(
        route: ActiveRoute, action_index: int, action) -> tuple[int, int] | None:
    next_index = action_index + 1
    if next_index >= len(route.action_route.actions):
        return None
    following = route.action_route.actions[next_index]
    following_air_direction = _air_action_direction(following)
    if following_air_direction is not None:
        return following_air_direction
    if type(following) is not WalkSegment:
        return None
    if type(action) is JumpUpSegment:
        start_x, _, start_z = (
            action.edge.end[0] + .5,
            float(action.edge.end[1]),
            action.edge.end[2] + .5,
        )
    else:
        start_x, _, start_z = action.end_surface.position
    for point in following.fixed_route.points:
        dx, dz = point.x - start_x, point.z - start_z
        if math.hypot(dx, dz) <= 1.0e-7:
            continue
        if abs(dx) > 1.0e-7 and abs(dz) > 1.0e-7:
            return None
        return (1 if dx > 0 else -1, 0) if abs(dx) > 1.0e-7 \
            else (0, 1 if dz > 0 else -1)
    return None


def _planned_gap_request(
        route: ActiveRoute, action_index: int, anchor: StateAnchor,
        execution_window: CandidateExecutionWindow,
        policy: GapSolverPolicy = DEFAULT_GAP_SOLVER_POLICY,
) -> tuple[GapSolveRequest | None, str]:
    if not 0 <= action_index < len(route.action_route.actions):
        return None, "action_index_outside_route"
    action = route.action_route.actions[action_index]
    if type(action) is not JumpGapSegment:
        return None, "route_action_is_not_jump_gap"
    direction = _air_action_direction(action)
    if direction is None:
        return None, "jump_gap_is_outside_b10b_trial"
    region = action.end_surface.region
    half_width = anchor.physics_state.body_width / 2.0
    min_x, max_x = region.min_x + half_width, region.max_x - half_width
    min_z, max_z = region.min_z + half_width, region.max_z - half_width
    if max_x <= min_x or max_z <= min_z:
        return None, "landing_region_narrower_than_body"
    landing = LandingRegion(
        min_x, max_x, min_z, max_z, action.end_surface.position[1],
    )
    exit_direction = _following_motion_direction(route, action_index, action)
    return GapSolveRequest(
        direction, landing, execution_window,
        max_candidates=12, max_ticks=20,
        recovery_horizon_ticks=20,
        exit_direction=exit_direction,
        exit_motion_ticks=1 if exit_direction is not None else 0,
        policy=policy,
        continuation=_planned_continuation(route, action_index, anchor),
    ), "ready"


def _planned_air_transition_request(
        route: ActiveRoute, action_index: int, anchor: StateAnchor,
        execution_window: CandidateExecutionWindow,
        damage_budget: TaskDamageBudget,
        policies: dict[MotionSolveKind, AirTransitionSolverPolicy],
) -> tuple[AirTransitionSolveRequest | None, str]:
    if not 0 <= action_index < len(route.action_route.actions):
        return None, "action_index_outside_route"
    action = route.action_route.actions[action_index]
    kind = _air_action_kind(action)
    if kind is None:
        return None, "route_action_is_not_air_transition"
    direction = _air_action_direction(action)
    if direction is None:
        return None, "air_transition_relation_is_unsupported"
    landing = _air_action_landing(action, anchor)
    if landing is None:
        return None, "landing_region_narrower_than_body"
    policy = policies.get(kind)
    if policy is None:
        return None, "air_transition_policy_missing"
    exit_direction = _following_motion_direction(route, action_index, action)
    return AirTransitionSolveRequest(
        kind, direction, landing, execution_window, damage_budget,
        max_candidates=min(64, len(policy.templates)),
        max_ticks=action_spec(action).solve_geometry(action, anchor).maximum_ticks,
        recovery_horizon_ticks=action_spec(action).solve_geometry(action, anchor).maximum_ticks,
        exit_direction=exit_direction,
        exit_motion_ticks=1 if exit_direction is not None else 0,
        policy=policy,
        continuation=_planned_continuation(route, action_index, anchor),
    ), "ready"


def _planned_continuation(route, action_index, anchor):
    """Bound the next ordinary straight leg without crossing a turn or action.

    Geometry defines candidate search space only. The calculator must establish
    actual known support, collision clearance, recovery and dependencies there.
    Other modes and proved height legs retain their existing entry contracts.
    """
    following_index = action_index + 1
    if following_index >= len(route.action_route.actions):
        return None
    following = route.action_route.actions[following_index]
    if type(following) is not WalkSegment or following.traversal_plan is not None:
        return None
    transition = following.transition
    mode = transition.mode if transition is not None else MovementMode.WALK
    if mode is not MovementMode.WALK:
        return None
    points = following.fixed_route.points
    if len(points) < 2:
        return None
    first, second = points[:2]
    dx, dz = second.x - first.x, second.z - first.z
    distance = math.hypot(dx, dz)
    direction = _air_action_direction(route.action_route.actions[action_index])
    if (distance <= .6 or abs(second.y - first.y) > 1.0e-7
            or direction is None
            or abs(dx / distance - direction[0]) > 1.0e-7
            or abs(dz / distance - direction[1]) > 1.0e-7):
        return None
    # Search nodes on one straight leg are reference samples, not turns. The
    # permission stops before the first real corner, height or segment boundary.
    for point in points[2:]:
        offset_x, offset_z = point.x - first.x, point.z - first.z
        progress = offset_x * direction[0] + offset_z * direction[1]
        lateral = -offset_x * direction[1] + offset_z * direction[0]
        if (abs(lateral) > 1.0e-7 or abs(point.y - first.y) > 1.0e-7
                or progress <= distance + 1.0e-7):
            break
        distance = progress
    maximum_speed = (transition.entry.maximum_speed_blocks_per_second
                     if transition is not None else 4.4)
    window = SegmentEntryWindow(
        (first.x, first.y, first.z), (float(direction[0]), float(direction[1])),
        -.2, min(4.0, distance - .4), .2,
        first.y - 1.0e-7, first.y + 1.0e-7,
        0.0, maximum_speed, math.radians(5),
        frozenset({"standing"}), frozenset({mode}), None, None,
        "d053-ordinary-successor-entry-v1",
    )
    # Subtracting world-space AABB coordinates can vary by an ULP as the body
    # moves. Keep this same geometric contract stable, below physics tolerance.
    width = round(anchor.physics_state.body_width, 12)
    recovery_window = replace(window,
        minimum_longitudinal_offset_blocks=-.5 - width / 2.0 + .15 * width,
        maximum_lateral_offset_blocks=.5 + width / 2.0 - .15 * width,
        minimum_feet_y=first.y - .10, maximum_feet_y=first.y + .10,
        minimum_speed_blocks_per_second=0.0,
        maximum_velocity_direction_error_radians=math.pi,
    )
    return MotionContinuationRequirement(window, mode, following.fixed_route.route_id,
                                         recovery_window)


def prepare_planned_gap_motion(
        route: ActiveRoute, action_index: int, anchor: StateAnchor,
        world: PhysicsWorldView, *, candidate_revision: int,
        intended_start_tick: int,
        changed_cells: tuple[BlockPos, ...] = (),
        damage_budget: TaskDamageBudget = TaskDamageBudget(),
        admitter: RouteAdmitter | None = None,
        precomputed: SolveResult | None = None,
        policy: GapSolverPolicy = DEFAULT_GAP_SOLVER_POLICY,
        input_ledger: InputApplicationLedger | None = None,
) -> GapPreparationResult:
    """Resolve one planned edge without recomputing the global route."""
    if (type(route) is not ActiveRoute or type(anchor) is not StateAnchor
            or type(world) is not PhysicsWorldView
            or type(changed_cells) is not tuple
            or (precomputed is not None and type(precomputed) is not SolveResult)):
        raise ContractViolation("gap preparation requires route, anchor and world")
    require_nonnegative_int(action_index, "action index")
    require_nonnegative_int(candidate_revision, "candidate revision")
    require_nonnegative_int(intended_start_tick, "intended start tick")
    request, request_reason = _planned_gap_request(
        route, action_index, anchor,
        CandidateExecutionWindow(intended_start_tick, intended_start_tick + 1),
        policy,
    )
    if request is None:
        return GapPreparationResult(
            GapPreparationStatus.UNSUPPORTED_ROUTE_ACTION,
            reason=request_reason,
        )
    solved = precomputed
    if solved is None:
        solved = solve_one_cell_gap(anchor, world, request)
    if solved.status is not SolveStatus.SOLVED or solved.proof is None:
        return GapPreparationResult(
            GapPreparationStatus.SOLVE_FAILED,
            solve_result=solved, reason=solved.status.value,
        )
    if (solved.proof.direction != request.direction
            or not _same_landing_region(
                solved.proof.landing, request.landing,
            )
            or solved.proof.exit_direction != request.exit_direction
            or solved.proof.exit_motion_ticks != request.exit_motion_ticks
            or solved.proof.continuation != request.continuation):
        return GapPreparationResult(
            GapPreparationStatus.SOLVE_FAILED,
            solve_result=solved, reason="precomputed_connection_mismatch",
        )
    route_admitter = admitter or RouteAdmitter()
    reusable = route_admitter.bind_verified_motion(
        route, solved.proof, action_index=action_index,
        candidate_revision=candidate_revision,
        damage_budget=damage_budget,
        accepted_resource_incomplete_reasons=_RESOURCE_ASSUMPTIONS,
    )
    admitted = route_admitter.admit_verified_motion(
        reusable, route, anchor, candidate_revision=candidate_revision,
        intended_start_tick=intended_start_tick,
        changed_cells=changed_cells, damage_budget=damage_budget,
        world=world,
        input_ledger=input_ledger,
    )
    if admitted.status is not MotionCandidateStatus.ACCEPTED:
        return GapPreparationResult(
            (GapPreparationStatus.REVALIDATION_REQUIRED
             if admitted.status is MotionCandidateStatus.NEEDS_REVALIDATION else
             GapPreparationStatus.ADMISSION_REJECTED),
            solve_result=solved, reason=admitted.reason,
            retryable=admitted.retryable,
        )
    return GapPreparationResult(
        GapPreparationStatus.READY, admitted.candidate, solved, "ready",
    )


def prepare_planned_air_transition(
        route: ActiveRoute, action_index: int, anchor: StateAnchor,
        world: PhysicsWorldView, *, candidate_revision: int,
        intended_start_tick: int,
        changed_cells: tuple[BlockPos, ...] = (),
        damage_budget: TaskDamageBudget = TaskDamageBudget(),
        admitter: RouteAdmitter | None = None,
        precomputed: SolveResult | None = None,
        policies: dict[MotionSolveKind, AirTransitionSolverPolicy]
        = DEFAULT_AIR_TRANSITION_POLICIES,
        input_ledger: InputApplicationLedger | None = None,
) -> GapPreparationResult:
    if (type(route) is not ActiveRoute or type(anchor) is not StateAnchor
            or type(world) is not PhysicsWorldView
            or type(changed_cells) is not tuple
            or type(damage_budget) is not TaskDamageBudget
            or type(policies) is not dict
            or (precomputed is not None and type(precomputed) is not SolveResult)):
        raise ContractViolation("air transition preparation requires typed inputs")
    require_nonnegative_int(action_index, "action index")
    require_nonnegative_int(candidate_revision, "candidate revision")
    require_nonnegative_int(intended_start_tick, "intended start tick")
    request, request_reason = _planned_air_transition_request(
        route, action_index, anchor,
        CandidateExecutionWindow(intended_start_tick, intended_start_tick + 1),
        damage_budget, policies,
    )
    if request is None:
        return GapPreparationResult(
            GapPreparationStatus.UNSUPPORTED_ROUTE_ACTION,
            reason=request_reason,
        )
    solved = precomputed or solve_air_transition(anchor, world, request)
    if solved.status is not SolveStatus.SOLVED or solved.proof is None:
        return GapPreparationResult(
            GapPreparationStatus.SOLVE_FAILED,
            solve_result=solved, reason=solved.status.value,
        )
    proof = solved.proof
    if (proof.kind is not request.kind
            or proof.direction != request.direction
            or not _same_landing_region(proof.landing, request.landing)
            or proof.exit_direction != request.exit_direction
            or proof.exit_motion_ticks != request.exit_motion_ticks
            or proof.continuation != request.continuation
            or proof.damage_budget != damage_budget):
        return GapPreparationResult(
            GapPreparationStatus.SOLVE_FAILED,
            solve_result=solved, reason="precomputed_connection_mismatch",
        )
    route_admitter = admitter or RouteAdmitter()
    reusable = route_admitter.bind_verified_motion(
        route, proof, action_index=action_index,
        candidate_revision=candidate_revision,
        damage_budget=damage_budget,
        accepted_resource_incomplete_reasons=_RESOURCE_ASSUMPTIONS,
    )
    admitted = route_admitter.admit_verified_motion(
        reusable, route, anchor, candidate_revision=candidate_revision,
        intended_start_tick=intended_start_tick,
        changed_cells=changed_cells, damage_budget=damage_budget, world=world,
        input_ledger=input_ledger,
    )
    if admitted.status is not MotionCandidateStatus.ACCEPTED:
        return GapPreparationResult(
            (GapPreparationStatus.REVALIDATION_REQUIRED
             if admitted.status is MotionCandidateStatus.NEEDS_REVALIDATION else
             GapPreparationStatus.ADMISSION_REJECTED),
            solve_result=solved, reason=admitted.reason,
            retryable=admitted.retryable,
        )
    return GapPreparationResult(
        GapPreparationStatus.READY, admitted.candidate, solved, "ready",
    )


class MotionRouteCoordinator:
    """Connect one active route to bounded background motion solving."""

    def __init__(self, route: ActiveRoute, executor: ActionRouteExecutor,
                 worker: MotionWorkerComputePort, *,
                 damage_budget: TaskDamageBudget = TaskDamageBudget(),
                 retry_ledger: RetryLedger,
                 computation_scope: AsyncComputationScope,
                 result_inbox: MotionResultInbox | None = None,
                 owner_instance_id: str | None = None,
                 clock_ns=time.monotonic_ns,
                 gap_solver_policy: GapSolverPolicy = DEFAULT_GAP_SOLVER_POLICY,
                 air_transition_policies: dict[
                     MotionSolveKind, AirTransitionSolverPolicy
                 ] = DEFAULT_AIR_TRANSITION_POLICIES) -> None:
        if (type(route) is not ActiveRoute
                or type(executor) is not ActionRouteExecutor
                or not isinstance(worker, MotionWorkerComputePort)
                or type(damage_budget) is not TaskDamageBudget
                or type(retry_ledger) is not RetryLedger
                or type(computation_scope) is not AsyncComputationScope
                or computation_scope.world_session_id != route.world_session
                or computation_scope.task_id != retry_ledger.task_id
                or (result_inbox is not None
                    and type(result_inbox) is not MotionResultInbox)
                or not callable(clock_ns)
                or type(gap_solver_policy) is not GapSolverPolicy
                or type(air_transition_policies) is not dict
                or any(type(kind) is not MotionSolveKind
                       or type(policy) is not AirTransitionSolverPolicy
                       or policy.kind is not kind
                       for kind, policy in air_transition_policies.items())):
            raise ContractViolation("motion route coordination requires typed owners")
        self.route = route
        self.executor = executor
        self.worker = worker
        self.gap_solver_policy = gap_solver_policy
        self.air_transition_policies = dict(air_transition_policies)
        self.damage_budget = damage_budget
        self.retry_ledger = retry_ledger
        self.computation_scope = computation_scope
        self._owns_result_inbox = result_inbox is None
        self.result_inbox = result_inbox or MotionResultInbox()
        self._clock = clock_ns
        from mc2p.motion_nav.async_work import AsyncOwnerScope
        self._owner_instance_id = owner_instance_id or AsyncOwnerScope().allocate()
        self._pending_connection: str | None = None
        self._pending_action_index: int | None = None
        self._pending_submitted_tick: int | None = None
        self._candidate_revision = 0
        from mc2p.motion_nav.async_work import AsyncWorkLifecycle
        self._work = AsyncWorkLifecycle()
        self._known_work_windows: dict[AsyncWorkIdentity, AsyncWorkWindow] = {}
        self._pending_job: GapMotionSolveJob | None = None
        self._solve_basis_job: GapMotionSolveJob | None = None
        self._pending_ground_job: GroundTerminalSolveJob | None = None
        self._ground_solve_basis_job: GroundTerminalSolveJob | None = None
        self._pending_cancellations: dict[
            AsyncWorkIdentity, tuple[GroundTerminalSearchStatus, int]
        ] = {}
        self._ground_beam_requested = False
        self._delivery_ticks = 1
        self._delivery_identity: AsyncWorkIdentity | None = None
        self.last_admission: AsyncAdmissionRecord | None = None
        self._admission_records: list[AsyncAdmissionRecord] = []
        self.unidentified_results = 0
        self.last_failure_attempt_id: str | None = None
        self.last_failure_reason = ""
        self._grounded_recovery_wait_id: str | None = None
        self._local_attempts = LocalAttemptChain(maximum_failures=3)
        self._local_attempt_action_index = executor.action_index
        self._reanchor_after_landing_action: int | None = None

    def start(self, frame: NavigationFrame) -> None:
        if type(frame) is not NavigationFrame:
            raise ContractViolation("motion route coordinator requires a frame")
        self._retire_work("motion_restarted")
        self.last_admission = None
        self.last_failure_attempt_id = None
        self.last_failure_reason = ""
        self._reanchor_after_landing_action = None
        self._end_grounded_recovery_wait()
        self._local_attempts.reset()
        self.executor.start(
            self.route.action_route, frame,
            damage_budget=self.damage_budget,
            require_verified_gap_motion=True,
            require_verified_motion_actions=frozenset(
                index for index, action in enumerate(
                    self.route.action_route.actions
                ) if _air_action_kind(action) is not None
            ),
        )
        self._local_attempt_action_index = self.executor.action_index

    @property
    def admission_records(self) -> tuple[AsyncAdmissionRecord, ...]:
        return tuple(self._admission_records)

    @property
    def async_diagnostics(self) -> AsyncOwnerDiagnostics:
        resources = tuple((self._work_identity, name) for name, value in (
            ("pending_connection", self._pending_connection),
            ("pending_job", self._pending_job),
            ("solve_basis", self._solve_basis_job),
            ("pending_ground_job", self._pending_ground_job),
            ("ground_solve_basis", self._ground_solve_basis_job),
        ) if value is not None)
        return AsyncOwnerDiagnostics(self._owner_instance_id, self._work_identity,
                                    self._work_window, self._work.events,
                                    self.admission_records, resources)

    @property
    def _work_identity(self):
        return self._work.identity

    @property
    def _work_window(self):
        return self._work.window

    def cancel_work(self, cause: str = "motion_route_stopped") -> None:
        if not isinstance(cause, str) or not cause:
            raise ContractViolation("motion work cancellation requires a cause")
        if self._ground_solve_basis_job is not None:
            self.executor.retire_ground_terminal()
        self._retire_work(cause)
        self._end_grounded_recovery_wait()

    def _connection_id(self, action_index: int) -> str:
        return f"{self.route.route_id}/action-{action_index}"

    def _ground_terminal_action_index(self) -> int | None:
        actions = self.route.action_route.actions
        if not actions:
            return None
        index = len(actions) - 1
        action = actions[index]
        if (type(action) is not WalkSegment
                or action.fixed_route.execution_contract is None
                or action.fixed_route.execution_contract.completion_region is None
                or (action.transition is not None
                    and action.transition.mode is not MovementMode.WALK)):
            return None
        return index

    @staticmethod
    def _ground_terminal_snapshot(
        frame: NavigationFrame,
        completion,
    ) -> NavigationFrame:
        points = (frame.body.position, completion.reference_point)
        min_x = math.floor(min(value[0] for value in points) - 3.0)
        max_x = math.ceil(max(value[0] for value in points) + 3.0)
        min_y = math.floor(min(frame.body.position[1], completion.support_height) - 2.0)
        max_y = math.ceil(max(
            frame.body.body_box.max_y, completion.support_height + 2.0,
        ) + 1.0)
        min_z = math.floor(min(value[2] for value in points) - 3.0)
        max_z = math.ceil(max(value[2] for value in points) + 3.0)
        facts = {}
        for x in range(min_x, max_x + 1):
            for y in range(min_y, max_y + 1):
                for z in range(min_z, max_z + 1):
                    fact = frame.world.cell((x, y, z))
                    if fact.knowledge is not CellKnowledge.UNKNOWN:
                        facts[(x, y, z)] = fact
        world = WorldView.detached(
            frame.world.session,
            frame.world.geometry_revision,
            frame.world.evidence_revision,
            facts,
        )
        return replace(frame, world=world, changed_cells=())

    @staticmethod
    def _ground_terminal_corridor(action: WalkSegment) -> tuple[Aabb, ...]:
        boxes = []
        for first, second in zip(
                action.fixed_route.points, action.fixed_route.points[1:]):
            boxes.append(Aabb(
                min(first.x, second.x) - .75,
                min(first.y, second.y) - .10,
                min(first.z, second.z) - .75,
                max(first.x, second.x) + .75,
                max(first.y, second.y) + 1.90,
                max(first.z, second.z) + .75,
            ))
        if not boxes:
            point = action.fixed_route.points[0]
            boxes.append(Aabb(
                point.x - .75, point.y - .10, point.z - .75,
                point.x + .75, point.y + 1.90, point.z + .75,
            ))
        return tuple(boxes)

    def _submit_ground_terminal(
        self,
        frame: NavigationFrame,
        anchor: StateAnchor,
        *,
        preparation_ticks: int = 0,
        replace_pending_basis: bool = False,
    ) -> bool:
        index = self._ground_terminal_action_index()
        if (index is None or index != self.executor.action_index
                or self._work_identity is not None
                or (self.executor.ground_terminal_active
                    and not replace_pending_basis)
                or type(preparation_ticks) is not int
                or not 0 <= preparation_ticks <= 32):
            return False
        action = self.route.action_route.actions[index]
        assert type(action) is WalkSegment
        completion = action.fixed_route.execution_contract.completion_region
        assert completion is not None
        self._candidate_revision += 1
        connection = f"{self._connection_id(index)}/ground-terminal"
        now = self._clock()
        worker_now = time.perf_counter_ns()
        identity = AsyncWorkIdentity(
            self.computation_scope,
            self._owner_instance_id,
            AsyncWorkKind.MOTION_SOLVE,
            connection,
            self._candidate_revision,
        )
        work_window = AsyncWorkWindow(
            anchor.movement_tick_id,
            now,
            now + _MOTION_SOLVE_LIMIT_NS,
        )
        self._work.begin(identity, work_window)
        self._remember_work_window(identity, work_window)
        if not self.result_inbox.register(identity):
            self.last_failure_reason = "motion_inbox_capacity_exhausted"
            self._retire_work(self.last_failure_reason)
            return False
        request = GroundTerminalSolveRequest(
            anchor=anchor,
            work_identity=identity,
            goal_id=self.route.goal_id,
            goal_revision=self.route.goal_revision,
            route_id=self.route.route_id,
            route_revision=self.route.route_revision,
            action_index=index,
            execution_window=CandidateExecutionWindow(
                anchor.movement_tick_id + preparation_ticks + 1,
                anchor.movement_tick_id + preparation_ticks + 2,
            ),
            frame=self._ground_terminal_snapshot(frame, completion),
            completion=completion,
            profile=self.executor.ground_profile,
            limits=GroundTerminalSearchLimits(
                maximum_ticks=8,
                neutral_tail_ticks=30,
                phase_candidate_budget=4096,
                beam_node_budget=32768,
                maximum_final_speed_blocks_per_second=min(
                    .1,
                    self.route.goal_state.maximum_terminal_speed_blocks_per_second
                    if self.route.goal_state is not None else .1,
                ),
                minimum_support_fraction=.15,
                deadline_ns=worker_now + _MOTION_SOLVE_LIMIT_NS,
            ),
            preparation_inputs=(MovementV1(),) * preparation_ticks,
            route_corridor=self._ground_terminal_corridor(action),
        )
        job = GroundTerminalSolveJob(
            connection, self._candidate_revision, request, worker_now,
        )
        if not self.executor.begin_ground_terminal_solve(request):
            self._retire_work("ground_terminal_basis_rejected")
            return False
        self._pending_ground_job = job
        self._ground_solve_basis_job = job
        self._pending_connection = connection
        self._pending_action_index = index
        self._pending_submitted_tick = anchor.movement_tick_id
        if self.worker.submit(job):
            self._pending_ground_job = None
            self.last_failure_reason = ""
        else:
            self.last_failure_reason = "motion_solver_backpressure"
        return True

    @property
    def recovering_grounded_entry(self) -> bool:
        return self._grounded_recovery_wait_id is not None

    def _end_grounded_recovery_wait(self) -> None:
        if self._grounded_recovery_wait_id is not None:
            self.retry_ledger.end_wait_owned(
                self._grounded_recovery_wait_id,
                f"motion-route/{self.route.route_id}",
            )
            self._grounded_recovery_wait_id = None

    def _sync_local_attempt_chain(self) -> None:
        action_index = self.executor.action_index
        if action_index != self._local_attempt_action_index:
            self._local_attempts.reset()
            self._local_attempt_action_index = action_index

    def _record_local_failure(self, attempt_id: str) -> LocalAttemptRegistration:
        self._sync_local_attempt_chain()
        self.last_failure_attempt_id = attempt_id
        return self._local_attempts.record(attempt_id)

    def _sample_solve_delivery(
            self, result: GapMotionSolveResult, anchor: StateAnchor, *,
            current_scope: AsyncComputationScope) -> None:
        if type(result) is not GapMotionSolveResult:
            return
        job = self._solve_basis_job
        if (job is None or job.operation is not MotionJobOperation.SOLVE
                or result.solve_result.status is not SolveStatus.SOLVED
                or result.work_identity is None
                or self._work.check(result.work_identity, self._clock(),
                                    current_scope=current_scope) is not WorkCheck.READY
                or result.connection_id != self._pending_connection
                or result.candidate_revision != self._candidate_revision
                or self._delivery_identity == result.work_identity):
            return
        # Sample first arrival from the real source, before any entry wait.
        self._delivery_ticks = min(4, max(1, anchor.movement_tick_id
            - job.anchor.movement_tick_id))
        self._delivery_identity = result.work_identity

    def _accept_ground_terminal_result(
            self, result: GroundTerminalSolveResult, anchor: StateAnchor,
            world: PhysicsWorldView, changed_cells: tuple[BlockPos, ...],
            ledger: InputApplicationLedger | None = None, *,
            current_scope: AsyncComputationScope) -> bool:
        basis = self._ground_solve_basis_job
        check = self._work.check(
            result.work_identity, self._clock(), current_scope=current_scope,
        )
        if check is not WorkCheck.READY:
            self._record_admission(
                (AsyncAdmissionDisposition.RECOMPUTE
                 if check is WorkCheck.EXPIRED else
                 AsyncAdmissionDisposition.DISCARDED_LATE),
                identity_matched=False,
                facts_valid=None,
                result_identity=result.work_identity,
            )
            self.executor.retire_ground_terminal()
            self._retire_work("ground_terminal_result_not_current")
            return False
        if (basis is None
                or result.connection_id != self._pending_connection
                or result.candidate_revision != self._candidate_revision
                or result.work_identity != basis.work_identity
                or basis.request.work_identity != result.work_identity
                or basis.request.goal_id != self.route.goal_id
                or basis.request.goal_revision != self.route.goal_revision
                or basis.request.route_id != self.route.route_id
                or basis.request.route_revision != self.route.route_revision
                or basis.request.action_index != self.executor.action_index):
            self._record_admission(
                AsyncAdmissionDisposition.DISCARDED_LATE,
                identity_matched=False, facts_valid=False,
                result_identity=result.work_identity,
            )
            self.executor.retire_ground_terminal()
            self._retire_work("ground_terminal_identity_changed")
            return False
        sequence = result.search_result.sequence
        if (result.search_result.status
                is GroundTerminalSearchStatus.INSUFFICIENT_LEAD
                and not basis.request.preparation_inputs
                and anchor.physics_state.on_ground
                and math.hypot(
                    anchor.physics_state.velocity_blocks_per_tick[0],
                    anchor.physics_state.velocity_blocks_per_tick[2],
                ) * 20.0 <= .1):
            self._record_admission(
                AsyncAdmissionDisposition.RECOMPUTE,
                identity_matched=True, facts_valid=True,
                result_identity=result.work_identity,
            )
            self._retire_work("ground_terminal_beam_lead_required")
            self._ground_beam_requested = True
            return False
        if (result.search_result.status is not GroundTerminalSearchStatus.SOLVED
                or sequence is None):
            attempt = self._record_local_failure(
                f"{result.connection_id}/candidate-{result.candidate_revision}/"
                f"{result.search_result.status.value}"
            )
            self._record_admission(
                (AsyncAdmissionDisposition.RECOMPUTE
                 if result.search_result.status in {
                     GroundTerminalSearchStatus.BUDGET_EXHAUSTED,
                     GroundTerminalSearchStatus.STALE,
                 } else AsyncAdmissionDisposition.TERMINATED),
                identity_matched=True, facts_valid=False,
                result_identity=result.work_identity,
            )
            self.executor.retire_ground_terminal()
            self._retire_work("ground_terminal_no_sequence")
            self.last_failure_reason = result.search_result.status.value
            if attempt.verdict is LocalAttemptVerdict.EXHAUSTED:
                self.last_failure_reason = "ground_terminal_retry_exhausted"
            return False
        changed = set(changed_cells)
        for position in sequence.dependencies:
            before = basis.request.frame.world.cell(position)
            current = world.cell(position)
            if (before.knowledge != current.knowledge
                    or before.block != current.block):
                changed.add(position)
        if changed.intersection(sequence.dependencies):
            self._record_admission(
                AsyncAdmissionDisposition.RECOMPUTE,
                identity_matched=True, facts_valid=False,
                result_identity=result.work_identity,
            )
            self.executor.retire_ground_terminal()
            self._retire_work("ground_terminal_dependency_changed")
            self.last_failure_reason = "ground_terminal_dependency_changed"
            return False
        accepted_ns = self._clock()
        if not self._work.try_apply(
                result.work_identity, accepted_ns,
                current_scope=current_scope):
            self.executor.retire_ground_terminal()
            self._retire_work("ground_terminal_apply_rejected")
            return False
        if not self.executor.install_ground_terminal_sequence(
                sequence, anchor, ledger):
            self._record_admission(
                AsyncAdmissionDisposition.RECOMPUTE,
                identity_matched=True, facts_valid=False,
                result_identity=result.work_identity,
            )
            self.executor.retire_ground_terminal()
            self._retire_work("ground_terminal_entry_changed")
            self.last_failure_reason = "ground_terminal_entry_changed"
            return False
        self._record_admission(
            AsyncAdmissionDisposition.APPLIED,
            identity_matched=True, facts_valid=True,
            result_identity=result.work_identity,
            accepted_ns=accepted_ns,
        )
        self._retire_work("ground_terminal_sequence_installed")
        self.last_failure_reason = ""
        self.last_failure_attempt_id = None
        return True

    def _accept_result(
            self, result: GapMotionSolveResult | GroundTerminalSolveResult,
            anchor: StateAnchor,
            world: PhysicsWorldView, changed_cells: tuple[BlockPos, ...],
            ledger: InputApplicationLedger | None = None, *,
            current_scope: AsyncComputationScope) -> bool:
        if type(result) is GroundTerminalSolveResult:
            return self._accept_ground_terminal_result(
                result, anchor, world, changed_cells, ledger,
                current_scope=current_scope,
            )
        if type(result) is not GapMotionSolveResult:
            raise ContractViolation("motion result domain is unsupported")
        if result.work_identity is None:
            self.unidentified_results += 1
            return False
        check = self._work.check(result.work_identity, self._clock(), current_scope=current_scope)
        if check is not WorkCheck.READY:
            if check is WorkCheck.EXPIRED:
                self._expire_delivered_result(result)
            else:
                self._record_admission(AsyncAdmissionDisposition.DISCARDED_LATE,
                    identity_matched=False, facts_valid=None,
                    result_identity=result.work_identity)
            return False
        if (result.connection_id != self._pending_connection
                or result.candidate_revision != self._candidate_revision):
            self._record_admission(AsyncAdmissionDisposition.DISCARDED_LATE,
                identity_matched=True, facts_valid=False, result_identity=result.work_identity)
            return False
        connection = result.connection_id
        self._sample_solve_delivery(result, anchor, current_scope=current_scope)
        action_index = (
            self._pending_action_index
            if self._pending_action_index is not None
            else self.executor.action_index
        )
        preparation_anchor = anchor
        self._pending_connection = None
        self._pending_action_index = None
        self._pending_submitted_tick = None
        self._pending_job = None
        action = self.route.action_route.actions[action_index]
        proof = result.solve_result.proof
        if proof is not None and self._solve_basis_job is not None:
            changed = set(changed_cells)
            for position in proof.world_dependencies:
                before = self._solve_basis_job.world.cell(position)
                current = world.cell(position)
                if before.knowledge != current.knowledge or before.block != current.block:
                    changed.add(position)
            changed_cells = tuple(sorted(changed))
        negative_stale = (
            result.solve_result.status not in {
                SolveStatus.SOLVED, SolveStatus.INTERNAL_ERROR, SolveStatus.BUDGET_EXHAUSTED,
            }
            and self._negative_basis_changed(preparation_anchor, world)
        )
        if negative_stale and self._solve_basis_job is not None:
            # A fresh necessary-condition rejection is already a current proof.
            # Do not spend repeated solves while a pushed body is still settling.
            current_rejection = check_motion_entry(
                preparation_anchor, world, self._solve_basis_job.request,
            )
            if current_rejection is not None:
                result = replace(result, solve_result=current_rejection)
                negative_stale = False
        if negative_stale:
            prepared = GapPreparationResult(
                GapPreparationStatus.ADMISSION_REJECTED,
                solve_result=result.solve_result,
                reason="motion_negative_basis_changed", retryable=True,
            )
        elif type(action) is JumpGapSegment:
            prepared = prepare_planned_gap_motion(
                self.route, action_index, preparation_anchor, world,
                candidate_revision=result.candidate_revision,
                intended_start_tick=preparation_anchor.movement_tick_id + 1,
                changed_cells=changed_cells,
                damage_budget=self.damage_budget,
                precomputed=result.solve_result,
                policy=self.gap_solver_policy,
                input_ledger=ledger,
            )
        else:
            prepared = prepare_planned_air_transition(
                self.route, action_index, preparation_anchor, world,
                candidate_revision=result.candidate_revision,
                intended_start_tick=preparation_anchor.movement_tick_id + 1,
                changed_cells=changed_cells,
                damage_budget=self.damage_budget,
                precomputed=result.solve_result,
                policies=self.air_transition_policies,
                input_ledger=ledger,
            )
        if (prepared.status is GapPreparationStatus.SOLVE_FAILED
                and result.solve_result.status is SolveStatus.NEEDS_STATE
                and self._solve_basis_job is not None
                and self._solve_basis_job.operation is MotionJobOperation.REVALIDATE
                and not gap_entry_heading_is_aligned(
                    preparation_anchor.physics_state.yaw_radians,
                    self._solve_basis_job.request.direction,
                )):
            # A look command can change the observed yaw while old commands
            # are revalidated. Re-enter the existing bounded alignment path;
            # a current velocity/pose rejection does not gain blanket retries.
            prepared = replace(prepared, retryable=True)
        if (prepared.status is GapPreparationStatus.READY
                and proof.execution_window.latest_start_tick
                    - (anchor.movement_tick_id + 1) < 1):
            # Only a new grant needs fresh slack. An already admitted action
            # keeps its original variants and is never re-anchored in flight.
            prepared = GapPreparationResult(
                GapPreparationStatus.REVALIDATION_REQUIRED,
                solve_result=result.solve_result,
                reason="new_motion_grant_has_no_start_slack", retryable=True,
            )
        if self._work.check(result.work_identity, self._clock(), current_scope=current_scope) is not WorkCheck.READY:
            self._expire_delivered_result(result)
            return False
        if (prepared.status is GapPreparationStatus.SOLVE_FAILED
                and result.solve_result.status is SolveStatus.NEEDS_STATE
                and not anchor.physics_state.on_ground):
            # A grounded retry can leave its support while the calculation is
            # in flight. Its missing entry state is not a no-solution proof.
            self._record_admission(AsyncAdmissionDisposition.RECOMPUTE,
                                   identity_matched=True, facts_valid=False)
            self._retire_work("airborne_entry_needs_state")
            if (action_index == self.executor.action_index
                    and not self.executor.current_verified_action_started()):
                self._reanchor_after_landing_action = action_index
            self.last_failure_reason = ""
            return False
        if prepared.status is not GapPreparationStatus.READY:
            self._record_admission(
                (AsyncAdmissionDisposition.RECOMPUTE
                 if prepared.retryable else AsyncAdmissionDisposition.TERMINATED),
                identity_matched=True,
                facts_valid=False,
            )
            preparation_ticks = self._delivery_ticks
            self._retire_work("motion_result_rejected")
            self.last_failure_reason = prepared.reason
            if prepared.status is GapPreparationStatus.REVALIDATION_REQUIRED:
                attempt_id = f"{connection}/revalidate-{result.candidate_revision}"
                registration = self._record_local_failure(attempt_id)
                if registration.verdict is LocalAttemptVerdict.RETRY:
                    self._submit_action(
                        action_index, anchor, world,
                        revalidate_proof=result.solve_result.proof,
                        entry_prefix=tuple(MotionCommandTick(
                            MovementV1(), anchor.physics_state.yaw_radians,
                        ) for _ in range(preparation_ticks)),
                    )
                else:
                    self.last_failure_reason = f"motion_retry_exhausted:{prepared.reason}"
                    self.executor.cancel()
                return False
            if action_index > self.executor.action_index:
                # An anticipated entry can differ from the next observation.
                # Keep the still-valid ground segment and try again from the
                # next applied state instead of cancelling the whole route.
                if prepared.retryable:
                    registration = self._record_local_failure(
                        f"{connection}/prepare-{result.candidate_revision}"
                    )
                    if registration.verdict is not LocalAttemptVerdict.RETRY:
                        self.last_failure_reason = (
                            f"motion_retry_exhausted:{prepared.reason}"
                        )
                        self.executor.cancel()
                return False
            if prepared.retryable:
                attempt_id = (f"{connection}/candidate-"
                              f"{result.candidate_revision}/{prepared.reason}")
                registration = self._record_local_failure(attempt_id)
                if registration.verdict is not LocalAttemptVerdict.RETRY:
                    self.last_failure_reason = (
                        f"motion_retry_exhausted:{prepared.reason}"
                    )
                    self.executor.cancel()
            else:
                self.executor.cancel()
            return False
        accepted_ns = self._clock()
        if not self._work.try_apply(result.work_identity, accepted_ns, current_scope=current_scope):
            self._expire_delivered_result(result)
            return False
        self.executor.install_verified_motion(prepared.candidate)
        self._record_admission(
            AsyncAdmissionDisposition.APPLIED,
            identity_matched=True,
            facts_valid=True,
            accepted_ns=accepted_ns,
        )
        self._retire_work("motion_proof_installed")
        self.last_failure_attempt_id = None
        self.last_failure_reason = ""
        return True

    def _negative_basis_changed(self, anchor, world) -> bool:
        job = self._solve_basis_job
        if job is None:
            return False  # Legacy direct preparation does not use background admission.
        old = job.anchor.physics_state
        current = anchor.physics_state
        if replace(old, movement_tick_id=current.movement_tick_id) != current:
            return True
        if (job.anchor.health_points, job.anchor.absorption_points,
                job.anchor.ruleset_id, job.anchor.input_projection_version) != (
                anchor.health_points, anchor.absorption_points,
                anchor.ruleset_id, anchor.input_projection_version):
            return True
        bounds = _gap_physics_bounds(job.world, job.anchor, job.request)
        for x in range(bounds.min_x, bounds.max_x + 1):
            for y in range(bounds.min_y, bounds.max_y + 1):
                for z in range(bounds.min_z, bounds.max_z + 1):
                    position = (x, y, z)
                    old_cell = job.world.cell(position)
                    cell = world.cell(position)
                    if old_cell.knowledge != cell.knowledge or old_cell.block != cell.block:
                        return True
        return False

    def _expire_delivered_result(self, result) -> None:
        self._record_admission(
            AsyncAdmissionDisposition.RECOMPUTE,
            identity_matched=True, facts_valid=None,
        )
        attempt_id = f"{result.connection_id}/candidate-{result.candidate_revision}/solver-request-expired"
        self._retire_work("motion_solver_request_expired")
        registration = self._record_local_failure(attempt_id)
        self.last_failure_reason = ""
        if registration.verdict is not LocalAttemptVerdict.RETRY:
            self.last_failure_reason = "motion_solver_retry_exhausted"
            self.executor.cancel()

    def _submit_action(
            self, index: int, anchor: StateAnchor,
            world: PhysicsWorldView, *,
            revalidate_proof: VerifiedMotionResult | None = None,
            entry_prefix: tuple[MotionCommandTick, ...] | None = None) -> None:
        if entry_prefix is None:
            # The existing one-tick waiting input is part of the prediction,
            # rather than silently consuming the first action's delay variant.
            entry_prefix = tuple(MotionCommandTick(
                MovementV1(), anchor.physics_state.yaw_radians,
            ) for _ in range(self._delivery_ticks))
        connection = self._connection_id(index)
        if (self._work_identity is not None
                and self._pending_connection == connection
                and self._pending_action_index == index):
            self._flush_pending_job()
            return
        if self._work_identity is not None:
            self._retire_work("motion_work_superseded")
        window = CandidateExecutionWindow(
            anchor.movement_tick_id + len(entry_prefix) + 1,
            anchor.movement_tick_id + len(entry_prefix) + 2,
        )
        action = self.route.action_route.actions[index]
        if type(action) is JumpGapSegment:
            request, reason = _planned_gap_request(
                self.route, index, anchor, window, self.gap_solver_policy,
            )
        else:
            request, reason = _planned_air_transition_request(
                self.route, index, anchor, window, self.damage_budget,
                self.air_transition_policies,
            )
        if request is None:
            self.last_failure_reason = reason
            self.executor.cancel()
            return
        self._candidate_revision += 1
        now = self._clock()
        identity = AsyncWorkIdentity(
            self.computation_scope,
            self._owner_instance_id,
            AsyncWorkKind.MOTION_SOLVE,
            connection,
            self._candidate_revision,
        )
        window = AsyncWorkWindow(
            anchor.movement_tick_id,
            now,
            now + _MOTION_SOLVE_LIMIT_NS,
        )
        self._work.begin(identity, window)
        self._remember_work_window(self._work_identity, self._work_window)
        if not self.result_inbox.register(self._work_identity):
            self.last_failure_reason = "motion_inbox_capacity_exhausted"
            self._retire_work(self.last_failure_reason)
            self.executor.cancel()
            return
        solve_world = _gap_physics_snapshot(world, anchor, request)
        self._pending_job = GapMotionSolveJob(
            connection, self._candidate_revision, anchor, solve_world, request,
            self._work_identity,
            operation=(MotionJobOperation.SOLVE if revalidate_proof is None
                       else MotionJobOperation.REVALIDATE),
            proof=revalidate_proof, entry_prefix=entry_prefix,
        )
        self._solve_basis_job = self._pending_job
        self._pending_connection = connection
        self._pending_action_index = index
        self._pending_submitted_tick = anchor.movement_tick_id
        submitted = self.worker.submit(self._pending_job)
        if submitted:
            self._pending_job = None
            self.last_failure_reason = ""
        else:
            self.last_failure_reason = "motion_solver_backpressure"
        if revalidate_proof is not None:
            from mc2p.motion_nav.fixed_route import GroundHandoffTarget
            self.executor.prepare_ground_handoff(GroundHandoffTarget(
                anchor.physics_state.position, anchor.movement_tick_id + 1,
                tuple(command.movement for command in entry_prefix),
                anchor.movement_tick_id + _MOTION_SOLVE_LIMIT_TICKS,
                movement_yaws_radians=tuple(command.required_movement_yaw_radians
                                           for command in entry_prefix),
                mode=MovementMode.WALK,
            ))

    def _flush_pending_job(self) -> None:
        job = self._pending_job or self._pending_ground_job
        if job is None:
            return
        if self.worker.submit(job):
            self._pending_job = None
            self._pending_ground_job = None
            self.last_failure_reason = ""

    def _submit_current(
            self, anchor: StateAnchor, world: PhysicsWorldView) -> None:
        self._submit_action(self.executor.action_index, anchor, world)

    def _work_expired(self, anchor: StateAnchor) -> bool:
        window = self._work_window
        if window is None:
            return (
                self._pending_connection is not None
                and self._pending_submitted_tick is not None
                and anchor.movement_tick_id
                    > self._pending_submitted_tick + _MOTION_SOLVE_LIMIT_TICKS
            )
        return (
            (
                window.expired(self._clock())
                or anchor.movement_tick_id
                    > window.started_movement_tick + _MOTION_SOLVE_LIMIT_TICKS
            )
        )

    def _retire_work(self, _cause: str) -> None:
        self.executor.prepare_ground_handoff(None)
        identity = self._work_identity
        if identity is not None:
            cancel = getattr(self.worker, "cancel", None)
            if callable(cancel):
                status = (
                    GroundTerminalSearchStatus.TIMEOUT
                    if "expired" in _cause or "timeout" in _cause else
                    GroundTerminalSearchStatus.CANCELLED
                    if "cancel" in _cause or "stopped" in _cause else
                    GroundTerminalSearchStatus.STALE
                )
                outcome = cancel(identity, status)
                if outcome in {False, MotionWorkerCancelStatus.BACKPRESSURE}:
                    if len(self._pending_cancellations) < 64:
                        self._pending_cancellations[identity] = (
                            status, self._clock() + 2_000_000_000,
                        )
                    else:
                        self.last_failure_reason = (
                            "motion_cancel_retry_capacity_exhausted"
                        )
            self.result_inbox.retire(identity)
            self._work.finish(identity, _cause, self._clock())
        self._pending_job = None
        self._pending_ground_job = None
        self._pending_connection = None
        self._pending_action_index = None
        self._pending_submitted_tick = None
        self._solve_basis_job = None
        self._ground_solve_basis_job = None

    def _retry_pending_cancellations(self) -> None:
        cancel = getattr(self.worker, "cancel", None)
        if not callable(cancel):
            self._pending_cancellations.clear()
            return
        now = self._clock()
        for identity, pending in tuple(self._pending_cancellations.items()):
            status, expires_ns = pending
            if now >= expires_ns:
                self._pending_cancellations.pop(identity, None)
                self.last_failure_reason = "motion_cancel_retry_expired"
                continue
            outcome = cancel(identity, status)
            if outcome in {
                    True,
                    MotionWorkerCancelStatus.ACCEPTED,
                    MotionWorkerCancelStatus.ALREADY_FINISHED,
                    MotionWorkerCancelStatus.WORKER_UNAVAILABLE,
            }:
                self._pending_cancellations.pop(identity, None)

    def _record_admission(
        self,
        disposition: AsyncAdmissionDisposition,
        *,
        identity_matched: bool,
        facts_valid: bool | None,
        result_identity: AsyncWorkIdentity | None = None,
        accepted_ns: int | None = None,
    ) -> None:
        identity = result_identity or self._work_identity
        if identity is None:
            self.unidentified_results += 1
            return
        window = self._known_work_windows.get(identity)
        deadline = 0 if window is None else window.deadline_monotonic_ns
        record = AsyncAdmissionRecord(
            identity,
            self._clock() if accepted_ns is None else accepted_ns,
            deadline,
            identity_matched,
            facts_valid,
            disposition,
        )
        self.last_admission = record
        if len(self._admission_records) >= 64:
            del self._admission_records[0]
        self._admission_records.append(record)

    def _remember_work_window(
        self,
        identity: AsyncWorkIdentity,
        window: AsyncWorkWindow,
    ) -> None:
        if len(self._known_work_windows) >= 64 and identity not in self._known_work_windows:
            del self._known_work_windows[next(iter(self._known_work_windows))]
        self._known_work_windows[identity] = window

    def _align_current_gap_entry(
            self, decision: ActionRouteDecision,
            anchor: StateAnchor) -> ActionRouteDecision | None:
        index = self.executor.action_index
        actions = self.route.action_route.actions
        if not 0 <= index < len(actions):
            return None
        action = actions[index]
        if _air_action_kind(action) is None:
            return None
        direction = _air_action_direction(action)
        if (direction is None or gap_entry_heading_is_aligned(
                anchor.physics_state.yaw_radians, direction)):
            return None
        delta_degrees = math.degrees(gap_entry_heading_delta_radians(
            anchor.physics_state.yaw_radians, direction,
        ))
        bounded_delta = max(
            -_MAX_ENTRY_ALIGNMENT_DEGREES_PER_TICK,
            min(_MAX_ENTRY_ALIGNMENT_DEGREES_PER_TICK, delta_degrees),
        )
        return replace(
            decision,
            movement=MovementV1(),
            look=LookV1(bounded_delta, 0.0),
            input_lease_ticks=1,
            reason_code="aligning_verified_motion_heading",
            submit_input=True,
            verified_command_index=None,
            expected_movement_tick=None,
            latest_movement_tick=None,
        )

    def _upcoming_gap_index(self) -> int | None:
        index = self.executor.action_index
        actions = self.route.action_route.actions
        if (not 0 <= index < len(actions)
                or type(actions[index]) is not WalkSegment
                or index + 1 >= len(actions)
                or _air_action_kind(actions[index + 1]) is None
                or self.executor.has_verified_motion(index + 1)):
            return None
        return index + 1

    def _prepare_upcoming_from_applied_state(
            self, decision: ActionRouteDecision, anchor: StateAnchor,
            world: PhysicsWorldView) -> None:
        action_index = self._upcoming_gap_index()
        if action_index is None:
            # A future airborne exit is not a real observation or a start grant.
            # Strict successors are prepared from their actual observed entry.
            return
        upcoming = self.route.action_route.actions[action_index]
        entry = action_spec(upcoming).entry_observation(upcoming, None)
        if entry is not None and entry.needs_acquisition_before_solve:
            # A multi-block drop needs fresh lower-volume evidence at its
            # actual entry.  Solving it while the preceding segment is still
            # moving would allow an already-installed proof to bypass that
            # action-boundary check.
            return
        if not decision.submit_input or decision.look is not None:
            return
        action = upcoming
        assert _air_action_kind(action) is not None
        sx, sy, sz = action_spec(action).solve_geometry(action, anchor).start
        state = anchor.physics_state
        speed = math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2])
        # One conditional prefix, at most four ticks / 200 ms of preparation.
        # Each real tick still passes arbitration, and the ledger must match it.
        if math.hypot(state.position[0] - sx, state.position[2] - sz) > max(.20, speed * 4 + .20):
            return
        prefix = []
        selected = None
        kind = _air_action_kind(upcoming)
        policy = (self.gap_solver_policy if kind is MotionSolveKind.JUMP_GAP
                  else self.air_transition_policies[kind])
        for _ in range(4):
            command = MotionCommandTick(decision.movement, anchor.physics_state.yaw_radians)
            projected = project_movement_command(
                state, command.movement, movement_yaw_radians=command.required_movement_yaw_radians,
            )
            if projected.status is not ProjectionStatus.READY or projected.tick_input is None:
                break
            calculated = step(state, projected.tick_input, world, JAVA_1_21_RULESET)
            if calculated.status is not CalculationStatus.OK or calculated.next_state is None:
                break
            state = calculated.next_state
            if not state.on_ground or state.horizontal_collision:
                break
            prefix.append(command)
            px, py, pz = state.position
            if (math.hypot(px - sx, pz - sz) <= .20 and abs(py - sy) <= .10
                    and math.hypot(state.velocity_blocks_per_tick[0],
                                   state.velocity_blocks_per_tick[2]) * 20
                        <= policy.maximum_entry_speed_blocks_per_second):
                selected = (tuple(prefix), state.position)
        if selected is not None:
            commands, position = selected
            self._submit_action(action_index, anchor, world, entry_prefix=commands)
            if self._pending_connection is not None:
                # The local controller still checks support, collisions and its
                # release tail each tick. This avoids steering back to the old
                # graph centre while the conditional entry is being prepared.
                from mc2p.motion_nav.fixed_route import GroundHandoffTarget
                self.executor.prepare_ground_handoff(GroundHandoffTarget(
                    position, anchor.movement_tick_id + 1,
                    tuple(command.movement for command in commands),
                    anchor.movement_tick_id + _MOTION_SOLVE_LIMIT_TICKS,
                    movement_yaws_radians=tuple(command.required_movement_yaw_radians
                                                for command in commands),
                    mode=(self.route.action_route.actions[self.executor.action_index].transition.mode
                          if self.route.action_route.actions[self.executor.action_index].transition is not None
                          else MovementMode.WALK),
                ))

    def decide(
            self, frame: NavigationFrame, anchor: StateAnchor,
            ledger: InputApplicationLedger, world: PhysicsWorldView, *,
            changed_cells: tuple[BlockPos, ...],
            current_scope: AsyncComputationScope,
            input_confirmed: bool = True,
            movement_yaw_radians: float | None = None,
            allow_grounded_reprepare: bool = True,
            result_poll_sequence: int | None = None) -> ActionRouteDecision:
        if (type(frame) is not NavigationFrame
                or type(current_scope) is not AsyncComputationScope
                or type(anchor) is not StateAnchor
                or type(ledger) is not InputApplicationLedger
                or type(world) is not PhysicsWorldView
                or type(changed_cells) is not tuple
                or type(allow_grounded_reprepare) is not bool
                or (result_poll_sequence is not None
                    and (type(result_poll_sequence) is not int
                         or result_poll_sequence < 0))):
            raise ContractViolation("motion route decision requires current typed state")
        self._retry_pending_cancellations()
        installed = False
        worker_available = True
        self._sync_local_attempt_chain()
        starting_action_index = self.executor.action_index
        if current_scope != self.computation_scope:
            # Invalidation removes calculation eligibility only. cancel()
            # keeps the executor responsible for in-flight inputs and landing.
            self._retire_work("motion_scope_invalidated")
            self.last_failure_reason = "motion_scope_invalidated"
            self.executor.cancel()
            worker_available = False
        elif not self.worker.is_alive():
            worker_available = False
            self._retire_work("motion_solver_worker_died")
            self.last_failure_reason = "motion_solver_worker_died"
            self.executor.cancel()
        elif self._work_expired(anchor):
            expired_ground = self._ground_solve_basis_job is not None
            expired_connection = self._pending_connection
            expired_action_index = (
                self._pending_action_index
                if self._pending_action_index is not None
                else self.executor.action_index
            )
            expired_revision = self._candidate_revision
            if expired_ground:
                self.executor.retire_ground_terminal()
            self._retire_work("motion_solver_request_expired")
            assert expired_connection is not None
            attempt_id = (
                f"{expired_connection}/candidate-{expired_revision}/"
                "solver-request-expired"
            )
            registration = self._record_local_failure(attempt_id)
            if expired_ground:
                self.last_failure_reason = (
                    "ground_terminal_solver_request_expired"
                    if registration.verdict is LocalAttemptVerdict.RETRY else
                    "ground_terminal_retry_exhausted"
                )
            elif registration.verdict is LocalAttemptVerdict.RETRY:
                self.last_failure_reason = ""
                if (registration.first_seen
                        and expired_action_index == self.executor.action_index):
                    self._submit_action(
                        expired_action_index, anchor, world,
                    )
            else:
                self.last_failure_reason = "motion_solver_retry_exhausted"
                self.executor.cancel()
        elif self._pending_job is not None or self._pending_ground_job is not None:
            self._flush_pending_job()
        available_results = ()
        if self._owns_result_inbox and self._work_identity is None:
            available_results = self.worker.poll_available()
        else:
            discarded = self.result_inbox.drain_once(
                self.worker,
                (frame.body.sequence_id if result_poll_sequence is None
                 else result_poll_sequence),
            )
            for result in discarded:
                if result.work_identity in self._known_work_windows:
                    self._record_admission(
                        AsyncAdmissionDisposition.DISCARDED_LATE,
                        identity_matched=False, facts_valid=None,
                        result_identity=result.work_identity,
                    )
            if self._work_identity is not None:
                result = self.result_inbox.peek(self._work_identity)
                if result is not None:
                    self._sample_solve_delivery(result, anchor, current_scope=current_scope)
                proof = (None if type(result) is not GapMotionSolveResult
                         else result.solve_result.proof)
                ground_sequence = (
                    result.search_result.sequence
                    if type(result) is GroundTerminalSolveResult else None
                )
                ready_to_take = (
                    ground_sequence is None
                    or anchor.movement_tick_id + 1
                        >= ground_sequence.execution_window.earliest_start_tick
                ) if type(result) is GroundTerminalSolveResult else (
                    proof is None
                    or anchor.movement_tick_id + 1
                        >= proof.execution_window.earliest_start_tick
                )
                if ready_to_take:
                    available_results = self.result_inbox.take(self._work_identity)
        for result in available_results:
            installed = self._accept_result(
                result, anchor, world, changed_cells, ledger,
                current_scope=current_scope,
            ) or installed
        if self._reanchor_after_landing_action == self.executor.action_index:
            if frame.body.is_on_ground:
                # Cancel only the obsolete route, after real landing. The
                # existing session recovery path verifies release and plans
                # from this observation instead of the old departure surface.
                self._reanchor_after_landing_action = None
                self.executor.cancel()
                decision = self.executor.decide(frame, state_anchor=anchor,
                                                input_ledger=ledger)
                return replace(decision, state=ActionRouteState.NEEDS_REPLAN,
                               reason_code="landed_entry_requires_reanchor")
            decision = self.executor.decide(frame, state_anchor=anchor,
                                            input_ledger=ledger)
            return replace(decision, movement=MovementV1(), look=None,
                           submit_input=True, input_lease_ticks=1,
                           requires_verified_motion=False,
                           body_phase=BodyControlPhase.ENTRY_RECOVERY,
                           reason_code="airborne_entry_waiting_for_landing")
        entry_ready = self.executor.current_verified_motion_can_start(anchor)
        if entry_ready is False:
            attempt_id = (
                f"{self._connection_id(self.executor.action_index)}:"
                f"entry-reanchor:{self._candidate_revision}"
            )
            registration = self._record_local_failure(attempt_id)
            self.last_failure_reason = "verified_entry_changed_before_submission"
            if registration.verdict is not LocalAttemptVerdict.RETRY:
                self.executor.cancel()
            else:
                self.executor.discard_unstarted_verified_motion()
                self._submit_current(anchor, world)
        decision = self.executor.decide(
            frame, input_confirmed=input_confirmed,
            state_anchor=anchor, input_ledger=ledger,
            movement_yaw_radians=movement_yaw_radians,
        )
        self._sync_local_attempt_chain()
        if self._ground_beam_requested:
            self._ground_beam_requested = False
            safe_prep = verified_ground_rollout(
                frame, anchor.physics_state, MovementV1(),
                control_ticks=1, tail_ticks=30,
                minimum_support=.15,
            )
            if (safe_prep is not None
                    and self._submit_ground_terminal(
                        frame, anchor,
                        preparation_ticks=GROUND_TERMINAL_BEAM_PREPARATION_TICKS,
                        replace_pending_basis=True,
                    )):
                decision = replace(
                    decision,
                    state=ActionRouteState.RUNNING,
                    movement=MovementV1(), look=None,
                    input_lease_ticks=1,
                    reason_code="preparing_ground_terminal_beam",
                    submit_input=True,
                    verified_command_index=None,
                    expected_movement_tick=None,
                    latest_movement_tick=None,
                )
            else:
                self.executor.retire_ground_terminal()
                self.last_failure_reason = "ground_terminal_beam_preparation_unproved"
        elif (worker_available
                and decision.reason_code in _GROUND_TERMINAL_FAILURE_REASONS
                and frame.body.is_on_ground
                and frame.body.pose == "standing"
                and self._local_attempts.failure_count
                    < self._local_attempts.maximum_failures
                and verified_ground_rollout(
                    frame, anchor.physics_state, MovementV1(),
                    control_ticks=1, tail_ticks=30,
                    minimum_support=.15,
                ) is not None
                and self._submit_ground_terminal(frame, anchor)):
            decision = replace(
                decision,
                state=ActionRouteState.RUNNING,
                movement=MovementV1(),
                look=None,
                input_lease_ticks=1,
                reason_code="awaiting_ground_terminal_sequence",
                submit_input=True,
                verified_command_index=None,
                expected_movement_tick=None,
                latest_movement_tick=None,
            )
        if (decision.ground_handoff_disposition is GroundHandoffDisposition.REJECTED
                and self._solve_basis_job is not None
                and self._solve_basis_job.entry_prefix):
            # The controller refused the exact projected prefix. Its result
            # cannot authorize that unrealized entry, even if it arrives later.
            self._retire_work("ground_preparation_input_rejected")
        grounded_recovery = (
            allow_grounded_reprepare
            and decision.state is ActionRouteState.INPUT_LOST
            and frame.body.is_on_ground
        )
        if not grounded_recovery:
            self._end_grounded_recovery_wait()
        if grounded_recovery:
            wait_id = (
                f"grounded-entry-recovery:"
                f"{self._connection_id(self.executor.action_index)}"
            )
            if self._grounded_recovery_wait_id != wait_id:
                self._end_grounded_recovery_wait()
                self.retry_ledger.begin_wait(
                    wait_id, f"motion-route/{self.route.route_id}",
                    _GROUNDED_ENTRY_RECOVERY_POLICY,
                    anchor.movement_tick_id,
                    frame.body.stamp.received_monotonic_ns,
                )
                self._grounded_recovery_wait_id = wait_id
            wait_status = self.retry_ledger.check_wait(
                wait_id, anchor.movement_tick_id,
                frame.body.stamp.received_monotonic_ns,
            )
            if wait_status is not WaitVerdict.WAITING:
                self._end_grounded_recovery_wait()
                self.last_failure_reason = (
                    "grounded_verified_entry_recovery_exhausted"
                )
                return replace(
                    decision,
                    reason_code=self.last_failure_reason,
                )
            attempt_id = (
                f"{self._connection_id(self.executor.action_index)}:"
                f"grounded-input-reanchor:{self._candidate_revision}"
            )
            self.last_failure_attempt_id = attempt_id
            self.last_failure_reason = decision.reason_code
            entry_state = self.executor.active_verified_entry_state()
            horizontal_speed = math.hypot(
                anchor.physics_state.velocity_blocks_per_tick[0],
                anchor.physics_state.velocity_blocks_per_tick[2],
            )
            stopped_on_entry_support = (
                entry_state is not None
                and abs(anchor.physics_state.position[1] - entry_state.position[1]) < 1.0e-7
                and horizontal_speed <= .01
                and verified_ground_rollout(
                    frame, anchor.physics_state, MovementV1(),
                    control_ticks=0, tail_ticks=8,
                    minimum_support=.01,
                ) is not None
            )
            recovery_movement = (
                None if entry_state is None or stopped_on_entry_support else
                verified_ground_target_movement(
                    frame, anchor.physics_state, entry_state.position,
                )
            )
            if recovery_movement is None and entry_state is None:
                recovery_movement = verified_ground_recovery_movement(
                    frame, anchor.physics_state,
                )
            if recovery_movement is not None:
                return replace(
                    decision,
                    state=ActionRouteState.RUNNING,
                    movement=recovery_movement,
                    look=None,
                    input_lease_ticks=1,
                    reason_code="recovering_grounded_verified_entry",
                    body_phase=BodyControlPhase.ENTRY_RECOVERY,
                    submit_input=True,
                    verified_command_index=None,
                    expected_movement_tick=None,
                    latest_movement_tick=None,
                    requires_verified_motion=True,
                )
            neutral_ground = verified_ground_rollout(
                frame, anchor.physics_state, MovementV1(),
                control_ticks=1, tail_ticks=0,
                minimum_support=1.0e-4,
            )
            if (horizontal_speed > .01 and neutral_ground is None
                    and self.executor
                        .retain_landing_after_verified_input_loss(anchor)):
                return replace(
                    decision,
                    state=ActionRouteState.CANCELLING,
                    movement=MovementV1(),
                    look=None,
                    input_lease_ticks=1,
                    reason_code="retain_landing_after_grounded_input_loss",
                    submit_input=True,
                    verified_command_index=None,
                    expected_movement_tick=None,
                    latest_movement_tick=None,
                    requires_verified_motion=True,
                )
            settle_movement = MovementV1(sneak=True)
            if (horizontal_speed > .01
                    and verified_ground_rollout(
                        frame, anchor.physics_state, settle_movement,
                        control_ticks=1, tail_ticks=8,
                        minimum_support=1.0e-4,
                    ) is not None):
                return replace(
                    decision,
                    state=ActionRouteState.RUNNING,
                    movement=settle_movement,
                    look=None,
                    input_lease_ticks=1,
                    reason_code="settling_grounded_verified_entry",
                    body_phase=BodyControlPhase.ENTRY_RECOVERY,
                    submit_input=True,
                    verified_command_index=None,
                    expected_movement_tick=None,
                    latest_movement_tick=None,
                    requires_verified_motion=True,
                )
            if frame.body.is_sneaking or frame.body.pose == "crouching":
                return replace(
                    decision,
                    state=ActionRouteState.RUNNING,
                    movement=MovementV1(),
                    look=None,
                    input_lease_ticks=1,
                    reason_code="releasing_grounded_verified_entry",
                    body_phase=BodyControlPhase.ENTRY_RECOVERY,
                    submit_input=True,
                    verified_command_index=None,
                    expected_movement_tick=None,
                    latest_movement_tick=None,
                    requires_verified_motion=True,
                )
            if self.executor.reprepare_grounded_verified_motion(frame):
                self._submit_current(anchor, world)
                return replace(
                    decision,
                    state=ActionRouteState.RUNNING,
                    movement=MovementV1(),
                    look=None,
                    input_lease_ticks=1,
                    reason_code="repreparing_grounded_verified_motion",
                    body_phase=BodyControlPhase.STRICT_PREPARATION,
                    submit_input=True,
                    verified_command_index=None,
                    expected_movement_tick=None,
                    latest_movement_tick=None,
                    requires_verified_motion=True,
                )
        if (decision.requires_verified_motion
                and self._pending_connection is None and not installed
                and worker_available):
            current = self.route.action_route.actions[
                self.executor.action_index
            ]
            entry = action_spec(current).entry_observation(current, None)
            if (self.executor.action_index != starting_action_index
                    and entry is not None and entry.needs_acquisition_before_solve):
                # The executor crossed the action boundary in this call.
                # Give the session one frame to run the immediate landing
                # evidence check before any proof is solved or installed.
                return decision
            alignment = self._align_current_gap_entry(decision, anchor)
            if alignment is not None:
                return alignment
            self._submit_current(anchor, world)
        elif (self._pending_connection is None and not installed
              and worker_available):
            self._prepare_upcoming_from_applied_state(
                decision, anchor, world,
            )
        if (self.last_failure_reason
                and decision.state is ActionRouteState.CANCELLED):
            # Cancelling is the executor's safe physical shutdown mechanism;
            # it is not the task result when the solver could not construct a
            # legal action.  Preserve the concrete failure for the session and
            # its parent instead of reporting a user cancellation.
            return replace(
                decision,
                state=ActionRouteState.UNSUPPORTED,
                reason_code=f"motion_unsolvable:{self.last_failure_reason}",
            )
        if (self._pending_action_index == self.executor.action_index
                and self._solve_basis_job is not None
                and self._solve_basis_job.entry_prefix
                and decision.state is ActionRouteState.RUNNING
                and decision.requires_verified_motion
                and not decision.submit_input):
            # The existing route owner submits preparation through the normal
            # arbiter/ledger. It is not a strict command or a risk commitment.
            source_tick = self._solve_basis_job.anchor.movement_tick_id
            offset = anchor.movement_tick_id - source_tick
            prefix = self._solve_basis_job.entry_prefix
            if 0 <= offset < len(prefix):
                movement = prefix[offset].movement
                if (decision.look is None
                        and verified_ground_rollout(
                            frame, anchor.physics_state, movement,
                            control_ticks=1, tail_ticks=0,
                            minimum_support=1.0e-4) is not None):
                    return replace(
                        decision, movement=movement, input_lease_ticks=1,
                        submit_input=True,
                        body_phase=BodyControlPhase.STRICT_PREPARATION,
                    )
        return decision
