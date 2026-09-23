"""B10-C bounded local preparation of a planned JumpGap action."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.motion_nav.action_route import JumpGapSegment, WalkSegment
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision, ActionRouteExecutor,
)
from mc2p.motion_nav.motion_candidate import (
    AdmittedMotionCandidate, MotionCandidateStatus,
)
from mc2p.motion_nav.motion_solver import (
    GapSolveRequest, LandingRegion, SolveResult, SolveStatus,
    solve_one_cell_gap,
)
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveJob, GapMotionSolveResult, MotionSolverWorker,
)
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, InputApplicationLedger, StateAnchor,
)
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.route_admission import ActiveRoute, RouteAdmitter
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import BlockPos


_RESOURCE_ASSUMPTIONS = ("server_hunger_clock_not_in_physics_state",)


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


def _cardinal_direction(action: JumpGapSegment) -> tuple[int, int] | None:
    dx = action.end_surface.position[0] - action.start_surface.position[0]
    dz = action.end_surface.position[2] - action.start_surface.position[2]
    if math.isclose(abs(dx), 2.0, abs_tol=1.0e-7) and math.isclose(
            dz, 0.0, abs_tol=1.0e-7):
        return (1 if dx > 0 else -1, 0)
    if math.isclose(abs(dz), 2.0, abs_tol=1.0e-7) and math.isclose(
            dx, 0.0, abs_tol=1.0e-7):
        return (0, 1 if dz > 0 else -1)
    return None


def _following_walk_direction(
        route: ActiveRoute, action_index: int,
        action: JumpGapSegment) -> tuple[int, int] | None:
    next_index = action_index + 1
    if next_index >= len(route.action_route.actions):
        return None
    following = route.action_route.actions[next_index]
    if type(following) is not WalkSegment:
        return None
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
) -> tuple[GapSolveRequest | None, str]:
    if not 0 <= action_index < len(route.action_route.actions):
        return None, "action_index_outside_route"
    action = route.action_route.actions[action_index]
    if type(action) is not JumpGapSegment:
        return None, "route_action_is_not_jump_gap"
    direction = _cardinal_direction(action)
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
    exit_direction = _following_walk_direction(route, action_index, action)
    return GapSolveRequest(
        direction, landing, execution_window,
        max_candidates=12, max_ticks=20,
        exit_direction=exit_direction,
        exit_motion_ticks=1 if exit_direction is not None else 0,
    ), "ready"


def prepare_planned_gap_motion(
        route: ActiveRoute, action_index: int, anchor: StateAnchor,
        world: PhysicsWorldView, *, candidate_revision: int,
        intended_start_tick: int,
        changed_cells: tuple[BlockPos, ...] = (),
        risk_policy_id: str = "no_expected_damage",
        admitter: RouteAdmitter | None = None,
        precomputed: SolveResult | None = None,
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
        risk_policy_id=risk_policy_id,
        accepted_resource_incomplete_reasons=_RESOURCE_ASSUMPTIONS,
    )
    admitted = route_admitter.admit_verified_motion(
        reusable, route, anchor, candidate_revision=candidate_revision,
        intended_start_tick=intended_start_tick,
        changed_cells=changed_cells, risk_policy_id=risk_policy_id,
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


class MotionRouteCoordinator:
    """Connect one active route to bounded background motion solving."""

    def __init__(self, route: ActiveRoute, executor: ActionRouteExecutor,
                 worker: MotionSolverWorker) -> None:
        if (type(route) is not ActiveRoute
                or type(executor) is not ActionRouteExecutor
                or type(worker) is not MotionSolverWorker):
            raise ContractViolation("motion route coordination requires typed owners")
        self.route = route
        self.executor = executor
        self.worker = worker
        self._pending_connection: str | None = None
        self._candidate_revision = 0
        self.last_failure_reason = ""

    def start(self, frame: NavigationFrame) -> None:
        if type(frame) is not NavigationFrame:
            raise ContractViolation("motion route coordinator requires a frame")
        self._pending_connection = None
        self._candidate_revision = 0
        self.last_failure_reason = ""
        self.executor.start(
            self.route.action_route, frame,
            require_verified_gap_motion=True,
        )

    def _connection_id(self, action_index: int) -> str:
        return f"{self.route.route_id}/action-{action_index}"

    def _accept_result(
            self, result: GapMotionSolveResult, anchor: StateAnchor,
            world: PhysicsWorldView, changed_cells: tuple[BlockPos, ...]) -> bool:
        if result.connection_id != self._pending_connection:
            return False
        self._pending_connection = None
        prepared = prepare_planned_gap_motion(
            self.route, self.executor.action_index, anchor, world,
            candidate_revision=result.candidate_revision,
            intended_start_tick=anchor.movement_tick_id + 1,
            changed_cells=changed_cells,
            precomputed=result.solve_result,
        )
        if prepared.status is not GapPreparationStatus.READY:
            self.last_failure_reason = prepared.reason
            if prepared.reason not in {
                    "candidate_revalidation_failed",
                    "world_dependency_changed"}:
                self.executor.cancel()
            return False
        self.executor.install_verified_motion(prepared.candidate)
        self.last_failure_reason = ""
        return True

    def _submit_current(
            self, anchor: StateAnchor, world: PhysicsWorldView) -> None:
        index = self.executor.action_index
        connection = self._connection_id(index)
        window = CandidateExecutionWindow(
            anchor.movement_tick_id + 1,
            anchor.movement_tick_id + 20,
        )
        request, reason = _planned_gap_request(
            self.route, index, anchor, window,
        )
        if request is None:
            self.last_failure_reason = reason
            self.executor.cancel()
            return
        self._candidate_revision += 1
        submitted = self.worker.submit(GapMotionSolveJob(
            connection, self._candidate_revision, anchor, world, request,
        ))
        if submitted:
            self._pending_connection = connection
            self.last_failure_reason = ""
        else:
            self._candidate_revision -= 1
            self.last_failure_reason = "motion_solver_backpressure"

    def decide(
            self, frame: NavigationFrame, anchor: StateAnchor,
            ledger: InputApplicationLedger, world: PhysicsWorldView, *,
            changed_cells: tuple[BlockPos, ...],
            input_confirmed: bool = True) -> ActionRouteDecision:
        if (type(frame) is not NavigationFrame
                or type(anchor) is not StateAnchor
                or type(ledger) is not InputApplicationLedger
                or type(world) is not PhysicsWorldView
                or type(changed_cells) is not tuple):
            raise ContractViolation("motion route decision requires current typed state")
        installed = False
        for result in self.worker.poll_available():
            installed = self._accept_result(
                result, anchor, world, changed_cells,
            ) or installed
        decision = self.executor.decide(
            frame, input_confirmed=input_confirmed,
            state_anchor=anchor, input_ledger=ledger,
        )
        if (decision.reason_code == "awaiting_verified_motion"
                and self._pending_connection is None and not installed):
            self._submit_current(anchor, world)
        return decision
