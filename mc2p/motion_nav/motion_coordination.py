"""B10-C bounded local preparation of a planned JumpGap action."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.motion_nav.action_route import (
    ControlledDropSegment, JumpGapSegment, JumpUpSegment, WalkSegment,
)
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision, ActionRouteExecutor,
)
from mc2p.motion_nav.motion_candidate import (
    AdmittedMotionCandidate, MotionCandidateStatus,
)
from mc2p.motion_nav.motion_solver import (
    DEFAULT_AIR_TRANSITION_POLICIES, DEFAULT_GAP_SOLVER_POLICY,
    AirTransitionSolveRequest, AirTransitionSolverPolicy,
    GapSolveRequest, GapSolverPolicy, LandingRegion, MotionSolveKind,
    SolveResult, SolveStatus,
    gap_entry_heading_delta_radians, gap_entry_heading_is_aligned,
    solve_air_transition, solve_one_cell_gap,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveJob, GapMotionSolveResult, MotionSolverWorker,
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
from mc2p.motion_nav.world_model import BlockPos


_RESOURCE_ASSUMPTIONS = ("server_hunger_clock_not_in_physics_state",)
_MAX_IDENTICAL_REVALIDATION_RETRIES = 2
_MAX_ENTRY_ALIGNMENT_DEGREES_PER_TICK = 36.0


def _gap_physics_snapshot(
        world: PhysicsWorldView, anchor: StateAnchor,
        request: GapSolveRequest | AirTransitionSolveRequest) -> PhysicsWorldView:
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
    bounds = PhysicsWorldBounds(
        math.floor(min(x - half, request.landing.min_x) - horizontal_margin),
        math.ceil(max(x + half, request.landing.max_x) + horizontal_margin) - 1,
        math.floor(min(y, request.landing.surface_y) - vertical_margin),
        math.ceil(max(
            y + state.body_height,
            request.landing.surface_y + state.body_height,
        ) + vertical_margin) - 1,
        math.floor(min(z - half, request.landing.min_z) - horizontal_margin),
        math.ceil(max(z + half, request.landing.max_z) + horizontal_margin) - 1,
    )
    return world.snapshot(bounds)


class GapPreparationStatus(StrEnum):
    READY = "ready"
    UNSUPPORTED_ROUTE_ACTION = "unsupported_route_action"
    SOLVE_FAILED = "solve_failed"
    ADMISSION_REJECTED = "admission_rejected"


@dataclass(frozen=True, slots=True)
class GapPreparationResult:
    status: GapPreparationStatus
    candidate: AdmittedMotionCandidate | None = None
    solve_result: SolveResult | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.status) is not GapPreparationStatus:
            raise ContractViolation("invalid gap preparation status")
        if (self.status is GapPreparationStatus.READY) != (
                type(self.candidate) is AdmittedMotionCandidate):
            raise ContractViolation("ready gap preparation requires admitted motion")


def _air_action_kind(action) -> MotionSolveKind | None:
    return {
        JumpGapSegment: MotionSolveKind.JUMP_GAP,
        JumpUpSegment: MotionSolveKind.JUMP_UP,
        ControlledDropSegment: MotionSolveKind.CONTROLLED_DROP,
    }.get(type(action))


def _air_action_direction(action) -> tuple[int, int] | None:
    if type(action) is JumpUpSegment:
        return action.edge.direction
    if type(action) not in (JumpGapSegment, ControlledDropSegment):
        return None
    dx = action.end_surface.position[0] - action.start_surface.position[0]
    dz = action.end_surface.position[2] - action.start_surface.position[2]
    expected_distance = 2.0 if type(action) is JumpGapSegment else 1.0
    if math.isclose(abs(dx), expected_distance, abs_tol=1.0e-7) and math.isclose(
            dz, 0.0, abs_tol=1.0e-7):
        return (1 if dx > 0 else -1, 0)
    if math.isclose(abs(dz), expected_distance, abs_tol=1.0e-7) and math.isclose(
            dx, 0.0, abs_tol=1.0e-7):
        return (0, 1 if dz > 0 else -1)
    return None


def _air_action_landing(action, anchor: StateAnchor) -> LandingRegion | None:
    half_width = anchor.physics_state.body_width / 2.0
    if type(action) is JumpUpSegment:
        x, y, z = action.edge.end
        return LandingRegion(
            x + half_width, x + 1.0 - half_width,
            z + half_width, z + 1.0 - half_width,
            float(y),
        )
    if type(action) not in (JumpGapSegment, ControlledDropSegment):
        return None
    region = action.end_surface.region
    min_x, max_x = region.min_x + half_width, region.max_x - half_width
    min_z, max_z = region.min_z + half_width, region.max_z - half_width
    if max_x <= min_x or max_z <= min_z:
        return None
    return LandingRegion(
        min_x, max_x, min_z, max_z, action.end_surface.position[1],
    )


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
        exit_direction=exit_direction,
        exit_motion_ticks=1 if exit_direction is not None else 0,
        policy=policy,
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
        max_ticks=(80 if kind is MotionSolveKind.CONTROLLED_DROP else 40),
        exit_direction=exit_direction,
        exit_motion_ticks=1 if exit_direction is not None else 0,
        policy=policy,
    ), "ready"


def prepare_planned_gap_motion(
        route: ActiveRoute, action_index: int, anchor: StateAnchor,
        world: PhysicsWorldView, *, candidate_revision: int,
        intended_start_tick: int,
        changed_cells: tuple[BlockPos, ...] = (),
        damage_budget: TaskDamageBudget = TaskDamageBudget(),
        admitter: RouteAdmitter | None = None,
        precomputed: SolveResult | None = None,
        policy: GapSolverPolicy = DEFAULT_GAP_SOLVER_POLICY,
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
            or solved.proof.landing != request.landing
            or solved.proof.exit_direction != request.exit_direction
            or solved.proof.exit_motion_ticks != request.exit_motion_ticks):
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
    )
    if admitted.status is not MotionCandidateStatus.ACCEPTED:
        return GapPreparationResult(
            GapPreparationStatus.ADMISSION_REJECTED,
            solve_result=solved, reason=admitted.reason,
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
            or proof.landing != request.landing
            or proof.exit_direction != request.exit_direction
            or proof.exit_motion_ticks != request.exit_motion_ticks
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
    )
    if admitted.status is not MotionCandidateStatus.ACCEPTED:
        return GapPreparationResult(
            GapPreparationStatus.ADMISSION_REJECTED,
            solve_result=solved, reason=admitted.reason,
        )
    return GapPreparationResult(
        GapPreparationStatus.READY, admitted.candidate, solved, "ready",
    )


class MotionRouteCoordinator:
    """Connect one active route to bounded background motion solving."""

    def __init__(self, route: ActiveRoute, executor: ActionRouteExecutor,
                 worker: MotionSolverWorker, *,
                 damage_budget: TaskDamageBudget = TaskDamageBudget(),
                 gap_solver_policy: GapSolverPolicy = DEFAULT_GAP_SOLVER_POLICY,
                 air_transition_policies: dict[
                     MotionSolveKind, AirTransitionSolverPolicy
                 ] = DEFAULT_AIR_TRANSITION_POLICIES) -> None:
        if (type(route) is not ActiveRoute
                or type(executor) is not ActionRouteExecutor
                or type(worker) is not MotionSolverWorker
                or type(damage_budget) is not TaskDamageBudget
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
        self._pending_connection: str | None = None
        self._pending_action_index: int | None = None
        self._pending_submitted_tick: int | None = None
        self._pending_preparation_anchor: StateAnchor | None = None
        self._candidate_revision = 0
        self._retry_signature: tuple | None = None
        self._retry_failures = 0
        self.last_failure_reason = ""

    def start(self, frame: NavigationFrame) -> None:
        if type(frame) is not NavigationFrame:
            raise ContractViolation("motion route coordinator requires a frame")
        self._pending_connection = None
        self._pending_action_index = None
        self._pending_submitted_tick = None
        self._pending_preparation_anchor = None
        self._candidate_revision = 0
        self._retry_signature = None
        self._retry_failures = 0
        self.last_failure_reason = ""
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

    def _connection_id(self, action_index: int) -> str:
        return f"{self.route.route_id}/action-{action_index}"

    @staticmethod
    def _revalidation_signature(
            connection: str, reason: str, anchor: StateAnchor,
            world: PhysicsWorldView,
            changed_cells: tuple[BlockPos, ...]) -> tuple:
        state = anchor.physics_state
        return (
            connection, reason, world.geometry_revision, changed_cells,
            state.position, state.velocity_blocks_per_tick,
            state.yaw_radians, state.pitch_radians, state.pose,
            state.on_ground, state.horizontal_collision,
            state.vertical_collision, state.sprinting, state.sneaking,
            state.jumping_cooldown_ticks, state.food_points,
            state.saturation_points, state.is_using_item,
        )

    def _record_retryable_failure(
            self, connection: str, reason: str, anchor: StateAnchor,
            world: PhysicsWorldView,
            changed_cells: tuple[BlockPos, ...]) -> bool:
        signature = self._revalidation_signature(
            connection, reason, anchor, world, changed_cells,
        )
        if signature == self._retry_signature:
            self._retry_failures += 1
        else:
            self._retry_signature = signature
            self._retry_failures = 1
        return self._retry_failures > _MAX_IDENTICAL_REVALIDATION_RETRIES

    def _accept_result(
            self, result: GapMotionSolveResult, anchor: StateAnchor,
            world: PhysicsWorldView, changed_cells: tuple[BlockPos, ...]) -> bool:
        if result.connection_id != self._pending_connection:
            return False
        connection = result.connection_id
        action_index = (
            self._pending_action_index
            if self._pending_action_index is not None
            else self.executor.action_index
        )
        preparation_anchor = self._pending_preparation_anchor or anchor
        self._pending_connection = None
        self._pending_action_index = None
        self._pending_submitted_tick = None
        self._pending_preparation_anchor = None
        action = self.route.action_route.actions[action_index]
        if type(action) is JumpGapSegment:
            prepared = prepare_planned_gap_motion(
                self.route, action_index, preparation_anchor, world,
                candidate_revision=result.candidate_revision,
                intended_start_tick=preparation_anchor.movement_tick_id + 1,
                changed_cells=changed_cells,
                damage_budget=self.damage_budget,
                precomputed=result.solve_result,
                policy=self.gap_solver_policy,
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
            )
        if prepared.status is not GapPreparationStatus.READY:
            self.last_failure_reason = prepared.reason
            if action_index > self.executor.action_index:
                # An anticipated entry can differ from the next observation.
                # Keep the still-valid ground segment and try again from the
                # next applied state instead of cancelling the whole route.
                return False
            retryable = prepared.reason in {
                "candidate_revalidation_failed", "world_dependency_changed",
            }
            if retryable and self._record_retryable_failure(
                    connection, prepared.reason, anchor, world, changed_cells):
                self.last_failure_reason = (
                    f"motion_retry_exhausted:{prepared.reason}"
                )
                self.executor.cancel()
            elif not retryable:
                self._retry_signature = None
                self._retry_failures = 0
                self.executor.cancel()
            return False
        self.executor.install_verified_motion(prepared.candidate)
        self._retry_signature = None
        self._retry_failures = 0
        self.last_failure_reason = ""
        return True

    def _submit_action(
            self, index: int, anchor: StateAnchor,
            world: PhysicsWorldView, *,
            preparation_anchor: StateAnchor | None = None) -> None:
        connection = self._connection_id(index)
        window = CandidateExecutionWindow(
            anchor.movement_tick_id + 1,
            anchor.movement_tick_id + 2,
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
        solve_world = _gap_physics_snapshot(world, anchor, request)
        submitted = self.worker.submit(GapMotionSolveJob(
            connection, self._candidate_revision, anchor, solve_world, request,
        ))
        if submitted:
            self._pending_connection = connection
            self._pending_action_index = index
            self._pending_submitted_tick = anchor.movement_tick_id
            self._pending_preparation_anchor = preparation_anchor
            self.last_failure_reason = ""
        else:
            self._candidate_revision -= 1
            self.last_failure_reason = "motion_solver_backpressure"

    def _submit_current(
            self, anchor: StateAnchor, world: PhysicsWorldView) -> None:
        self._submit_action(self.executor.action_index, anchor, world)

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

    @staticmethod
    def _predict_applied_walk_state(
            decision: ActionRouteDecision, anchor: StateAnchor,
            world: PhysicsWorldView) -> StateAnchor | None:
        if not decision.submit_input:
            return None
        movement_yaw = anchor.physics_state.yaw_radians
        if decision.look is not None:
            movement_yaw += math.radians(decision.look.yaw_delta_degrees)
        projected = project_movement_command(
            anchor.physics_state, decision.movement,
            movement_yaw_radians=movement_yaw,
        )
        if (projected.status is not ProjectionStatus.READY
                or projected.tick_input is None):
            return None
        calculated = step(
            anchor.physics_state, projected.tick_input,
            world, JAVA_1_21_RULESET,
        )
        if (calculated.status is not CalculationStatus.OK
                or calculated.next_state is None):
            return None
        return replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=calculated.next_state.movement_tick_id,
            confirmed_control_sequence=None,
            confirmed_control_tick_range=None,
            physics_state=calculated.next_state,
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

    def _upcoming_air_index(self) -> int | None:
        index = self.executor.action_index
        actions = self.route.action_route.actions
        if (not 0 <= index < len(actions)
                or _air_action_kind(actions[index]) is None
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
            action_index = self._upcoming_air_index()
            if action_index is None:
                return
            exit_state = self.executor.active_verified_exit_state()
            if exit_state is None:
                return
            predicted = replace(
                anchor,
                observation_sequence_id=anchor.observation_sequence_id + 1,
                movement_tick_id=exit_state.movement_tick_id,
                confirmed_control_sequence=None,
                confirmed_control_tick_range=None,
                physics_state=exit_state,
            )
            self._submit_action(
                action_index, predicted, world,
                preparation_anchor=predicted,
            )
            return
        predicted = self._predict_applied_walk_state(decision, anchor, world)
        if predicted is None or not predicted.physics_state.on_ground:
            return
        action = self.route.action_route.actions[action_index]
        assert _air_action_kind(action) is not None
        px, py, pz = predicted.physics_state.position
        if type(action) is JumpUpSegment:
            sx, sy, sz = (
                action.edge.start[0] + .5,
                float(action.edge.start[1]),
                action.edge.start[2] + .5,
            )
        else:
            sx, sy, sz = action.start_surface.position
        if (math.hypot(px - sx, pz - sz) > .20
                or abs(py - sy) > .10):
            return
        self._submit_action(action_index, predicted, world)

    def decide(
            self, frame: NavigationFrame, anchor: StateAnchor,
            ledger: InputApplicationLedger, world: PhysicsWorldView, *,
            changed_cells: tuple[BlockPos, ...],
            input_confirmed: bool = True,
            movement_yaw_radians: float | None = None) -> ActionRouteDecision:
        if (type(frame) is not NavigationFrame
                or type(anchor) is not StateAnchor
                or type(ledger) is not InputApplicationLedger
                or type(world) is not PhysicsWorldView
                or type(changed_cells) is not tuple):
            raise ContractViolation("motion route decision requires current typed state")
        installed = False
        worker_available = True
        if not self.worker.is_alive():
            worker_available = False
            self._pending_connection = None
            self._pending_action_index = None
            self._pending_submitted_tick = None
            self._pending_preparation_anchor = None
            self.last_failure_reason = "motion_solver_worker_died"
            self.executor.cancel()
        elif (self._pending_connection is not None
              and self._pending_submitted_tick is not None
              and anchor.movement_tick_id > self._pending_submitted_tick + 20):
            self._pending_connection = None
            self._pending_action_index = None
            self._pending_submitted_tick = None
            self._pending_preparation_anchor = None
            self.last_failure_reason = "motion_solver_request_expired"
            self.executor.cancel()
        for result in self.worker.poll_available():
            installed = self._accept_result(
                result, anchor, world, changed_cells,
            ) or installed
        decision = self.executor.decide(
            frame, input_confirmed=input_confirmed,
            state_anchor=anchor, input_ledger=ledger,
            movement_yaw_radians=movement_yaw_radians,
        )
        if (decision.reason_code == "awaiting_verified_motion"
                and self._pending_connection is None and not installed
                and worker_available):
            alignment = self._align_current_gap_entry(decision, anchor)
            if alignment is not None:
                return alignment
            self._submit_current(anchor, world)
        elif (self._pending_connection is None and not installed
              and worker_available):
            self._prepare_upcoming_from_applied_state(
                decision, anchor, world,
            )
        return decision
