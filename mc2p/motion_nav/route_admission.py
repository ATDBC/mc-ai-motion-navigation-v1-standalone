"""Control-thread admission boundary for B04 background route candidates."""
from __future__ import annotations

from mc2p.motion_nav.actions.registry import action_spec
from mc2p.motion_nav.landing_evidence import direct_drop_visual_evidence_sufficient

from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
import math

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route import (
    ActionRoute, ControlledDropSegment, JumpGapSegment, JumpUpSegment,
    StepSegment, WalkSegment, canonical_surface_node_path,
)
from mc2p.motion_nav.async_work import AsyncWorkIdentity
from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_modes import observed_ground_mode
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion, GroundRouteExecutionContract
from mc2p.motion_nav.jump_up import JumpUpEdge
from mc2p.motion_nav.landing_edge_probe import (
    LandingEdgeProbe,
)
from mc2p.motion_nav.movement_transition import compose_movement_transitions
from mc2p.motion_nav.movement_transition import (
    GoalState, GoalSupport, MovementMode, ResourceState,
)
from mc2p.motion_nav.movement_transition import MovementTransition
from mc2p.motion_nav.motion_candidate import (
    MotionCandidateAdmission, MotionCandidateAdmitter, MotionCandidateContext,
    MotionCandidateStatus,
    VerifiedMotionCandidate,
)
from mc2p.motion_nav.motion_solver import (
    MotionSolveKind, VerifiedMotionResult,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_risk import MOVEMENT_DAMAGE_BUDGET_RESOURCE
from mc2p.motion_nav.online_motion import InputApplicationLedger, StateAnchor
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.route_validation import (
    ActiveRouteValidation,
    ActiveRouteValidationDisposition,
    ActiveRouteValidationIdentity,
    ActiveRouteValidationPlan,
    ActiveRouteValidationReason,
    DependencyOwner,
    DependencyOwnerKind,
    DependencyProvenance,
    GroundCapabilityIdentity,
    InitialConnectionValidation,
    StandableConnectionQueryArgs,
    StandableRegionQueryArgs,
    SurfaceEdgeQueryArgs,
    WalkActionValidationPlan,
    WalkLegValidationBinding,
    WalkValidationQueryKind,
    WalkValidationRecipe,
    RouteProgressEvidence,
    RouteValidationBudget,
    ground_profile_allows_dependency_blocks,
    replay_walk_validation_recipe,
)
from mc2p.motion_nav.known_map_planner import (
    PlanningRequest, SurfacePlanningRequest,
    PlanningStatus, RouteCandidate, WalkEdge, WalkNode, WalkNodeId,
    SurfacePlanningStatus, SurfaceRouteCandidate, SurfaceWalkEdge,
    SurfaceControlledDropEdge, SurfaceJumpGapEdge, SurfaceJumpUpEdge,
)
from mc2p.motion_nav.step_transition import StepEdge
from mc2p.motion_nav.support_surfaces import (
    SupportSurface,
    SurfaceNodeId,
    query_standable_connection,
    query_support_surfaces,
    standable_point_in_region,
    standable_region_in_goal,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.segment_entry import (
    SegmentEntryWindow, body_fits_segment_entry,
)
from mc2p.motion_nav.world_model import (
    Aabb, BlockPos, CellKnowledge, WorldQueryCache, WorldView,
)


class AdmissionStatus(StrEnum):
    ACCEPTED = "accepted"
    NEEDS_INFORMATION = "needs_information"
    NOT_APPLICABLE = "not_applicable"
    REJECTED = "rejected"


def _action_route_length(route: ActionRoute) -> float:
    total = 0.
    for action in route.actions:
        if type(action) is WalkSegment:
            points = action.fixed_route.points
            total += sum(math.dist((a.x,a.y,a.z), (b.x,b.y,b.z)) for a,b in zip(points, points[1:]))
        elif type(action) is JumpUpSegment:
            total += math.dist(action.edge.start, action.edge.end)
        else:
            total += math.dist(action.start_surface.position, action.end_surface.position)
    return total


class AdmissionReason(StrEnum):
    GOAL_STANDING_POINT_UNAVAILABLE = "goal_standing_point_unavailable"
    GOAL_STANDING_POINT_NEEDS_INFORMATION = "goal_standing_point_needs_information"
    CANDIDATE_NOT_COMPLETE = "candidate_not_complete"
    PLANNING_REQUEST_REPLACED = "planning_request_replaced"
    WORLD_SESSION_CHANGED = "world_session_changed"
    GOAL_REVISION_CHANGED = "goal_revision_changed"
    CANDIDATE_BASIS_MISMATCH = "candidate_basis_mismatch"
    CURRENT_GOAL_NOT_SATISFIED = "current_goal_not_satisfied"
    ROUTE_RISK_POLICY_CHANGED = "route_risk_policy_changed"
    ROUTE_CAPABILITIES_CHANGED = "route_capabilities_changed"
    WORLD_DELTA_MISSING = "world_delta_missing"
    ROUTE_DEPENDENCIES_CHANGED = "route_dependencies_changed"
    CURRENT_BODY_CANNOT_CONNECT = "current_body_cannot_connect"
    CANDIDATE_HAS_NO_ACTIONS = "candidate_has_no_actions"
    ROUTE_RESOURCE_UNOBSERVABLE = "route_resource_unobservable"
    ROUTE_RESOURCES_BELOW_MINIMUM = "route_resources_below_minimum"
    ROUTE_ENTRY_RESOURCES_UNAVAILABLE = "route_entry_resources_unavailable"
    ROUTE_RESOURCES_UNAVAILABLE = "route_resources_unavailable"
    GROUND_TRAVERSAL_PROOF_MISSING = "ground_traversal_proof_missing"
    LANDING_VISUAL_EVIDENCE_MISSING = "landing_visual_evidence_missing"
    CANDIDATE_ADMITTED = "candidate_admitted"


class LocalDirectAdmissionPhase(StrEnum):
    REQUEST = "request"
    BODY_SURFACE = "body_surface"
    SURFACE_IDENTITY = "surface_identity"
    GOAL_SELECTION = "goal_selection"
    EXACT_CONNECTION = "exact_connection"
    MATERIAL_CAPABILITY = "material_capability"
    ROUTE_BUILD = "route_build"


@dataclass(frozen=True, slots=True)
class LocalDirectAdmissionEvidence:
    """Bounded facts from one local-direct admission attempt."""

    request_id: str
    goal_revision: int
    start: SurfaceNodeId
    goal: SurfaceNodeId
    goal_region: Aabb | None
    body_position: tuple[float, float, float]
    phase: LocalDirectAdmissionPhase
    phase_query_status: QueryStatus | None
    final_status: AdmissionStatus
    final_reason: AdmissionReason
    body_surface_node: SurfaceNodeId | None = None
    selected_position: tuple[float, float, float] | None = None
    selector_status: QueryStatus | None = None
    exact_status: QueryStatus | None = None
    missing_count: int = 0
    exact_dependency_count: int = 0

    def __post_init__(self) -> None:
        if type(self.request_id) is not str or not self.request_id:
            raise ContractViolation("local admission evidence request is invalid")
        if type(self.goal_revision) is not int or self.goal_revision < 0:
            raise ContractViolation("local admission evidence revision is invalid")
        if (type(self.start) is not SurfaceNodeId
                or type(self.goal) is not SurfaceNodeId
                or (self.goal_region is not None
                    and type(self.goal_region) is not Aabb)):
            raise ContractViolation("local admission evidence goal is invalid")
        for position, name in (
            (self.body_position, "body position"),
            (self.selected_position, "selected position"),
        ):
            if position is None:
                continue
            if (type(position) is not tuple or len(position) != 3
                    or any(type(value) not in (int, float)
                           or not math.isfinite(float(value))
                           for value in position)):
                raise ContractViolation(
                    f"local admission evidence {name} is invalid"
                )
        if (type(self.phase) is not LocalDirectAdmissionPhase
                or (self.phase_query_status is not None
                    and type(self.phase_query_status) is not QueryStatus)
                or type(self.final_status) is not AdmissionStatus
                or type(self.final_reason) is not AdmissionReason
                or (self.body_surface_node is not None
                    and type(self.body_surface_node) is not SurfaceNodeId)
                or (self.selector_status is not None
                    and type(self.selector_status) is not QueryStatus)
                or (self.exact_status is not None
                    and type(self.exact_status) is not QueryStatus)):
            raise ContractViolation("local admission evidence types are invalid")
        if (type(self.missing_count) is not int or self.missing_count < 0
                or type(self.exact_dependency_count) is not int
                or self.exact_dependency_count < 0):
            raise ContractViolation("local admission evidence counts are invalid")


@dataclass(frozen=True, slots=True)
class ExecutableCorridor:
    node_ids: tuple[WalkNodeId | SurfaceNodeId, ...]
    dependencies: tuple[BlockPos, ...]
    length_blocks: float
    stop_node: WalkNodeId | SurfaceNodeId

    def affected_by(self, changed_cells: tuple[BlockPos, ...]) -> bool:
        return bool(set(self.dependencies).intersection(changed_cells))


def _surface_identity(surface: SupportSurface) -> tuple[int, int, int, int]:
    node = surface.node_id
    return (
        node.column_x,
        node.column_z,
        node.vertical_band,
        node.surface_index,
    )


def _validate_completion_surface(
    action: object,
    args: StandableRegionQueryArgs,
) -> None:
    """Bind a completion proof to the declared endpoint of one action."""
    surface = args.surface
    expected = args.expected_region
    if (expected.surface_identity != _surface_identity(surface)
            or abs(expected.support_height - surface.position[1]) > 1.0e-9):
        raise ContractViolation(
            "completion region differs from its recipe surface"
        )
    traversal_plan = getattr(action, "traversal_plan", None)
    if traversal_plan is not None:
        surface_path = traversal_plan.surface_node_path
        terminal = traversal_plan.route.points[-1]
        if (not surface_path or surface_path[-1] != surface.node_id
                or (terminal.x, terminal.y, terminal.z)
                    != expected.reference_point):
            raise ContractViolation(
                "completion recipe differs from strict traversal endpoint"
            )
        return
    end_surface = getattr(action, "end_surface", None)
    if end_surface is not None:
        if type(end_surface) is not SupportSurface or surface != end_surface:
            raise ContractViolation(
                "completion recipe differs from strict action endpoint"
            )
        return
    edge = getattr(action, "edge", None)
    endpoint = getattr(edge, "end", None)
    if (type(endpoint) is not tuple or len(endpoint) != 3
            or any(type(value) is not int for value in endpoint)):
        raise ContractViolation(
            "strict action does not declare a completion endpoint"
        )
    node = surface.node_id
    if ((node.column_x, node.vertical_band, node.column_z) != endpoint
            or abs(surface.position[1] - float(endpoint[1])) > 1.0e-9):
        raise ContractViolation(
            "completion recipe differs from strict action endpoint"
        )


@dataclass(frozen=True, slots=True)
class ActiveRoute:
    route_id: str
    route_revision: int
    source_request_id: str
    goal_id: str
    goal_revision: int
    world_session: str
    fixed_route: FixedRoute | None
    fixed_route_length_blocks: float
    connection_length_blocks: float
    connection_dependencies: tuple[BlockPos, ...]
    corridor: ExecutableCorridor
    action_route: ActionRoute
    goal_state: GoalState | None = None
    planning_generation: int = 0
    work_identity: AsyncWorkIdentity | None = None
    validation_plan: ActiveRouteValidationPlan | None = None

    def __post_init__(self) -> None:
        if (self.validation_plan is not None
                and type(self.validation_plan) is not ActiveRouteValidationPlan):
            raise ContractViolation("active route validation plan must be typed")
        if self.validation_plan is None:
            return
        action_count = len(self.action_route.actions)
        if any(not 0 <= owner.action_index < action_count
               for owner in self.validation_plan.owners):
            raise ContractViolation("validation owner action is outside active route")
        for action_index, action in enumerate(self.action_route.actions):
            owner_refs = tuple(
                owner.owner_id for owner in self.validation_plan.owners
                if owner.action_index == action_index
            )
            covered = {
                position
                for owner_ref in owner_refs
                for position in self.validation_plan.dependencies_for_owner(
                    owner_ref
                )
            }
            if covered != set(action.dependencies):
                raise ContractViolation(
                    "validation owners do not exactly cover action dependencies"
                )
        has_initial_connection = self.connection_length_blocks > 1.0e-9
        if has_initial_connection != (
                self.validation_plan.initial_connection is not None):
            raise ContractViolation(
                "validation initial connection metadata differs from active route"
            )
        if self.validation_plan.initial_connection is not None:
            initial_owner_ref = (
                self.validation_plan.initial_connection.owner_ref
            )
            if set(self.validation_plan.dependencies_for_owner(
                    initial_owner_ref)) != set(self.connection_dependencies):
                raise ContractViolation(
                    "validation initial owner differs from connection dependencies"
                )
        for action_plan in self.validation_plan.action_plans:
            if not 0 <= action_plan.action_index < action_count:
                raise ContractViolation("validation action plan is outside active route")
            action = self.action_route.actions[action_plan.action_index]
            if (type(action) is not WalkSegment
                    or action.fixed_route.route_id != action_plan.fixed_route_id):
                raise ContractViolation("validation action plan differs from active route")
            points = action.fixed_route.points
            progress = [0.0]
            for first, second in zip(points, points[1:]):
                progress.append(progress[-1] + math.dist(
                    (first.x, first.y, first.z),
                    (second.x, second.y, second.z),
                ))
            for leg in action_plan.legs:
                if leg.end_point_index >= len(points):
                    raise ContractViolation("validation leg is outside its fixed route")
                if (abs(progress[leg.start_point_index]
                        - leg.start_progress_blocks) > 1.0e-9
                        or abs(progress[leg.end_point_index]
                               - leg.end_progress_blocks) > 1.0e-9):
                    raise ContractViolation("validation leg progress differs from fixed route")
                recipe = self.validation_plan.recipe(leg.recipe_ref)
                start = points[leg.start_point_index]
                end = points[leg.end_point_index]
                start_position = start.x, start.y, start.z
                end_position = end.x, end.y, end.z
                if recipe.query_kind is WalkValidationQueryKind.SURFACE_EDGE:
                    assert recipe.surface_edge is not None
                    if (recipe.surface_edge.start_surface.position != start_position
                            or recipe.surface_edge.end_surface.position != end_position):
                        raise ContractViolation(
                            "surface validation recipe differs from fixed route"
                        )
                else:
                    assert recipe.standable_connection is not None
                    if (recipe.standable_connection.connection_from != start_position
                            or recipe.standable_connection.position != end_position):
                        raise ContractViolation(
                            "standable validation recipe differs from fixed route"
                        )
        for owner in self.validation_plan.owners:
            action = self.action_route.actions[owner.action_index]
            if owner.kind is DependencyOwnerKind.COMPLETION_REGION:
                recipe = self.validation_plan.recipe(owner.recipe_ref)
                args = recipe.standable_region
                if (args is None or self.goal_state is None
                        or args.goal_region != self.goal_state.region):
                    raise ContractViolation(
                        "completion recipe differs from active goal"
                    )
                if owner.action_index != action_count - 1:
                    raise ContractViolation(
                        "completion owner must bind the final action"
                    )
                if owner.fixed_route_id is None:
                    _validate_completion_surface(action, args)
                    continue
                if (args.expected_region.surface_identity
                        != _surface_identity(args.surface)
                        or abs(args.expected_region.support_height
                               - args.surface.position[1]) > 1.0e-9):
                    raise ContractViolation(
                        "completion region differs from its recipe surface"
                    )
                fixed_route = getattr(action, "fixed_route", None)
                if (type(fixed_route) is not FixedRoute
                        or fixed_route.execution_contract is None
                        or args.expected_region != fixed_route
                            .execution_contract.completion_region):
                    raise ContractViolation(
                        "completion recipe differs from final route contract"
                    )
            if owner.fixed_route_id is None:
                continue
            if (type(action) is not WalkSegment
                    or action.fixed_route.route_id != owner.fixed_route_id):
                raise ContractViolation("validation owner fixed route differs")
        if (self.validation_plan.initial_connection is not None
                and abs(
                    self.validation_plan.initial_connection
                    .retire_after_progress_blocks
                    - self.connection_length_blocks
                ) > 1.0e-9):
            raise ContractViolation("validation initial connection length differs")
        if self.validation_plan.initial_connection is not None:
            owner = self.validation_plan.owner(
                self.validation_plan.initial_connection.owner_ref
            )
            if owner.recipe_ref is not None:
                recipe = self.validation_plan.recipe(owner.recipe_ref)
                if (recipe.query_kind
                        is not WalkValidationQueryKind.STANDABLE_CONNECTION):
                    raise ContractViolation(
                        "initial connection requires a standable recipe"
                    )
                action = self.action_route.actions[owner.action_index]
                if type(action) is not WalkSegment or len(
                        action.fixed_route.points) < 2:
                    raise ContractViolation(
                        "initial connection lacks fixed route points"
                    )
                first, second = action.fixed_route.points[:2]
                assert recipe.standable_connection is not None
                if (recipe.standable_connection.connection_from
                        != (first.x, first.y, first.z)
                        or recipe.standable_connection.position
                        != (second.x, second.y, second.z)):
                    raise ContractViolation(
                        "initial connection recipe differs from fixed route"
                    )


@dataclass(frozen=True, slots=True)
class _StandableQueryProof:
    args: StandableConnectionQueryArgs
    dependencies: tuple[BlockPos, ...]


@dataclass(frozen=True, slots=True)
class _ExactTerminalExecutionDependencies:
    """Keep terminal proof sources separate from the preceding route."""

    action_index: int
    preterminal_dependencies: tuple[BlockPos, ...]
    exact_dependencies: tuple[BlockPos, ...]
    uses_initial_connection: bool = False
    completion_dependencies: tuple[BlockPos, ...] = ()

    def __post_init__(self) -> None:
        if type(self.action_index) is not int or self.action_index < 0:
            raise ContractViolation("exact terminal action index is invalid")
        if type(self.uses_initial_connection) is not bool:
            raise ContractViolation("exact terminal proof source must be typed")
        for values, name in (
            (self.preterminal_dependencies, "preterminal dependencies"),
            (self.exact_dependencies, "exact terminal dependencies"),
            (self.completion_dependencies, "completion dependencies"),
        ):
            if (type(values) is not tuple
                    or values != tuple(sorted(set(values)))):
                raise ContractViolation(f"{name} must be sorted and unique")

    @property
    def execution_dependencies(self) -> tuple[BlockPos, ...]:
        return tuple(sorted(
            set(self.preterminal_dependencies) | set(self.exact_dependencies)
            | set(self.completion_dependencies)
        ))


@dataclass(frozen=True, slots=True)
class _ForwardGroundEntry:
    length_blocks: float
    proof: _StandableQueryProof


@dataclass(frozen=True, slots=True)
class _DirectWalkProofContext:
    """One admission-stack binding for a proved ordinary Walk leg."""

    world_session: str
    route_id: str
    action_index: int
    fixed_route_id: str
    connection_from: tuple[float, float, float]
    position: tuple[float, float, float]
    ground_profile: GroundMotionProfile
    capability_identity: GroundCapabilityIdentity
    proof: _StandableQueryProof

    def __post_init__(self) -> None:
        if (type(self.world_session) is not str or not self.world_session
                or type(self.route_id) is not str or not self.route_id
                or type(self.fixed_route_id) is not str
                or not self.fixed_route_id
                or type(self.action_index) is not int
                or self.action_index < 0):
            raise ContractViolation("direct Walk proof identity is invalid")
        if (type(self.ground_profile) is not GroundMotionProfile
                or type(self.capability_identity)
                    is not GroundCapabilityIdentity
                or self.capability_identity
                    != GroundCapabilityIdentity.from_profile(
                        self.ground_profile
                    )):
            raise ContractViolation("direct Walk proof capability is invalid")
        if (self.proof.args.connection_from != self.connection_from
                or self.proof.args.position != self.position):
            raise ContractViolation("direct Walk proof differs from its fixed leg")


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    status: AdmissionStatus
    reason: AdmissionReason
    route: ActiveRoute | None = None
    missing_cells: tuple[BlockPos, ...] = ()
    local_direct_evidence: LocalDirectAdmissionEvidence | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not AdmissionStatus \
                or type(self.reason) is not AdmissionReason:
            raise ContractViolation("route admission result must be typed")
        if (type(self.missing_cells) is not tuple
                or self.missing_cells != tuple(sorted(set(self.missing_cells)))):
            raise ContractViolation("route admission missing cells must be sorted and unique")
        if (self.local_direct_evidence is not None
                and (type(self.local_direct_evidence)
                     is not LocalDirectAdmissionEvidence
                     or self.local_direct_evidence.final_status is not self.status
                     or self.local_direct_evidence.final_reason is not self.reason)):
            raise ContractViolation(
                "local admission evidence differs from admission result"
            )


class CorridorStatus(StrEnum):
    READY = "ready"
    BLOCKED_BY_CHANGE = "blocked_by_change"


@dataclass(frozen=True, slots=True)
class CorridorUpdate:
    status: CorridorStatus
    route: ActiveRoute
    reason: str


class RouteAdmitter:
    """Validate a bounded prefix before a background candidate becomes active."""

    def __init__(self, *, maximum_corridor_blocks: float = 8.0) -> None:
        if (type(maximum_corridor_blocks) not in (int,float)
                or not math.isfinite(float(maximum_corridor_blocks))
                or maximum_corridor_blocks<=0):
            raise ContractViolation("corridor length must be positive and finite")
        self.maximum_corridor_blocks=float(maximum_corridor_blocks)
        self._motion_admitter = MotionCandidateAdmitter()

    @staticmethod
    def _direct_walk_recipe(
        context: _DirectWalkProofContext,
        recipe_id: str,
    ) -> WalkValidationRecipe:
        if type(context) is not _DirectWalkProofContext:
            raise ContractViolation("direct Walk recipe requires typed context")
        return WalkValidationRecipe(
            recipe_id,
            WalkValidationQueryKind.STANDABLE_CONNECTION,
            None,
            context.proof.args,
            context.ground_profile,
            context.capability_identity,
            context.proof.dependencies,
        )

    @staticmethod
    def _surface_for_local_body(
        request: SurfacePlanningRequest,
        frame: NavigationFrame,
    ) -> tuple[
        QueryStatus,
        SupportSurface | None,
        tuple[BlockPos, ...],
        tuple[BlockPos, ...],
    ]:
        start = request.start
        y = frame.body.position[1]
        result = query_support_surfaces(
            frame.world,
            start.column_x,
            start.column_z,
            y - 1.0,
            y + 1.0,
        )
        if result.status is not QueryStatus.FEASIBLE:
            return (
                result.status,
                None,
                result.dependencies,
                result.missing_cells,
            )
        body = frame.body.body_box
        surface = next((
            item for item in result.surfaces
            if item.node_id == start
            and abs(item.position[1] - body.min_y) <= .1 + 1.0e-9
            and min(body.max_x, item.region.max_x)
                > max(body.min_x, item.region.min_x) + 1.0e-9
            and min(body.max_z, item.region.max_z)
                > max(body.min_z, item.region.min_z) + 1.0e-9
        ), None)
        return (
            QueryStatus.FEASIBLE if surface is not None
            else QueryStatus.BLOCKED,
            surface,
            result.dependencies,
            (),
        )

    @staticmethod
    def _local_result(
        request: SurfacePlanningRequest,
        frame: NavigationFrame,
        phase: LocalDirectAdmissionPhase,
        status: AdmissionStatus,
        reason: AdmissionReason,
        *,
        phase_query_status: QueryStatus | None = None,
        route: ActiveRoute | None = None,
        missing_cells: tuple[BlockPos, ...] = (),
        body_surface_node: SurfaceNodeId | None = None,
        selected_position: tuple[float, float, float] | None = None,
        selector_status: QueryStatus | None = None,
        exact_status: QueryStatus | None = None,
        exact_dependency_count: int = 0,
    ) -> AdmissionResult:
        goal_state = request.goal_state
        evidence = LocalDirectAdmissionEvidence(
            request.request_id,
            request.goal_revision,
            request.start,
            request.goal,
            None if goal_state is None else goal_state.region,
            frame.body.position,
            phase,
            phase_query_status,
            status,
            reason,
            body_surface_node,
            selected_position,
            selector_status,
            exact_status,
            len(missing_cells),
            exact_dependency_count,
        )
        return AdmissionResult(
            status,
            reason,
            route,
            missing_cells,
            evidence,
        )

    @staticmethod
    def _local_rejection(
        request: SurfacePlanningRequest,
        frame: NavigationFrame,
        phase: LocalDirectAdmissionPhase,
        status: QueryStatus,
        *,
        missing_cells: tuple[BlockPos, ...] = (),
        body_surface_node: SurfaceNodeId | None = None,
        selected_position: tuple[float, float, float] | None = None,
        selector_status: QueryStatus | None = None,
        exact_status: QueryStatus | None = None,
        exact_dependency_count: int = 0,
    ) -> AdmissionResult:
        reason = {
            QueryStatus.NEEDS_INFORMATION:
                AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION,
            QueryStatus.BLOCKED:
                AdmissionReason.CURRENT_BODY_CANNOT_CONNECT,
            QueryStatus.UNSUPPORTED:
                AdmissionReason.ROUTE_CAPABILITIES_CHANGED,
        }.get(status)
        if reason is None:
            raise ContractViolation("local direct rejection requires failed query")
        return RouteAdmitter._local_result(
            request,
            frame,
            phase,
            AdmissionStatus.REJECTED,
            reason,
            phase_query_status=status,
            missing_cells=missing_cells,
            body_surface_node=body_surface_node,
            selected_position=selected_position,
            selector_status=selector_status,
            exact_status=exact_status,
            exact_dependency_count=exact_dependency_count,
        )

    @staticmethod
    def _goal_completion(world: WorldView, surface: SupportSurface, goal: GoalState,
                         profile: GroundMotionProfile | None, incoming):
        selected = standable_region_in_goal(
            world, surface, goal.region, connection_from=incoming,
            allowed_materials=(
                None if profile is None else profile.support_materials
            ),
        )
        completion = selected.completion_region
        if (selected.status is QueryStatus.FEASIBLE
                and completion is not None
                and completion.contains(surface.position)
                and query_standable_connection(
                    world,
                    surface,
                    completion.reference_point,
                    incoming,
                ).status is not QueryStatus.FEASIBLE):
            # Keep the already-proved surface endpoint only when the newly
            # selected terminal would require an unsafe diagonal tail.
            selected = replace(
                selected,
                completion_region=replace(
                    completion,
                    reference_point=surface.position,
                ),
            )
        return selected

    @staticmethod
    def _direct_walk_result(
        request: SurfacePlanningRequest,
        frame: NavigationFrame,
        *,
        endpoint_surface: SupportSurface,
        endpoint: tuple[float, float, float],
        dependencies: tuple[BlockPos, ...],
        ground_profile: GroundMotionProfile,
        capability_identity: GroundCapabilityIdentity,
        route_suffix: str,
        node_ids: tuple[SurfaceNodeId, ...],
        completion_region: GroundCompletionRegion,
    ) -> AdmissionResult:
        route_id = f"{request.request_id}-{route_suffix}"
        contract = GroundRouteExecutionContract.for_completion(completion_region, dependencies,
                                                               ground_profile.profile_id)
        fixed_route = FixedRoute(route_id, (
            RoutePoint(*frame.body.position),
            RoutePoint(*endpoint),
        ), contract)
        length = math.dist(frame.body.position, endpoint)
        if length <= 1.0e-9:
            return AdmissionResult(
                AdmissionStatus.REJECTED,
                AdmissionReason.CANDIDATE_HAS_NO_ACTIONS,
            )
        proof = _StandableQueryProof(
            StandableConnectionQueryArgs(
                endpoint_surface,
                endpoint,
                frame.body.position,
                .6,
                1.8,
            ),
            dependencies,
        )
        context = _DirectWalkProofContext(
            request.world_session,
            route_id,
            0,
            fixed_route.route_id,
            frame.body.position,
            endpoint,
            ground_profile,
            capability_identity,
            proof,
        )
        recipe = RouteAdmitter._direct_walk_recipe(
            context, f"{route_id}/validation/recipe/001",
        )
        owner = DependencyOwner(
            f"{route_id}/validation/owner/001",
            DependencyOwnerKind.WALK_LEG,
            0,
            fixed_route.route_id,
            recipe.recipe_id,
        )
        leg = WalkLegValidationBinding(
            owner.owner_id,
            recipe.recipe_id,
            0,
            1,
            0.0,
            length,
        )
        region_recipe = (WalkValidationRecipe(f'{route_id}/validation/recipe/002',
            WalkValidationQueryKind.STANDABLE_REGION, None, None, ground_profile,
            capability_identity, completion_region.dependencies,
            StandableRegionQueryArgs(endpoint_surface, request.goal_state.region,
                frame.body.position, completion_region)) if completion_region.dependencies else None)
        region_owner = (DependencyOwner(f'{route_id}/validation/owner/002',
            DependencyOwnerKind.COMPLETION_REGION, 0, fixed_route.route_id, region_recipe.recipe_id)
            if region_recipe is not None else None)
        plan = ActiveRouteValidationPlan(
            (WalkActionValidationPlan(
                0, fixed_route.route_id, (leg,),
            ),),
            (recipe,) if region_recipe is None else (recipe, region_recipe),
            (owner,) if region_owner is None else (owner, region_owner),
            None,
            tuple(
                DependencyProvenance(position, tuple(sorted(
                    ((owner.owner_id,) if position in dependencies else ())
                    + ((region_owner.owner_id,) if region_owner is not None
                       and position in completion_region.dependencies else ()))))
                for position in contract.dependencies
            ),
        )
        action_route = ActionRoute(
            route_id,
            (WalkSegment(
                fixed_route,
                node_ids,
                contract.dependencies,
            ),),
            request.goal_state,
            request.initial_resources,
        )
        route = ActiveRoute(
            route_id,
            1,
            request.request_id,
            request.goal_id,
            request.goal_revision,
            request.world_session,
            fixed_route,
            length,
            0.0,
            (),
            ExecutableCorridor(
                node_ids,
                contract.dependencies,
                length,
                request.goal,
            ),
            action_route,
            request.goal_state,
            request.sequence,
            request.work_identity,
            plan,
        )
        return AdmissionResult(
            AdmissionStatus.ACCEPTED,
            AdmissionReason.CANDIDATE_ADMITTED,
            route,
        )

    def admit_ground_direct(
        self,
        request: SurfacePlanningRequest,
        frame: NavigationFrame,
        *,
        ground_profile: GroundMotionProfile,
        capability_identity: GroundCapabilityIdentity,
    ) -> AdmissionResult:
        """Try one bounded, ordinary same-height direct Walk."""
        if (type(request) is not SurfacePlanningRequest
                or type(frame) is not NavigationFrame
                or type(ground_profile) is not GroundMotionProfile
                or type(capability_identity)
                    is not GroundCapabilityIdentity):
            raise ContractViolation(
                "ground direct admission requires typed request, frame, and profile"
            )
        if capability_identity != GroundCapabilityIdentity.from_profile(
                ground_profile):
            return AdmissionResult(
                AdmissionStatus.REJECTED,
                AdmissionReason.ROUTE_CAPABILITIES_CHANGED,
            )
        if request.world_session != frame.session.value:
            return AdmissionResult(
                AdmissionStatus.REJECTED,
                AdmissionReason.WORLD_SESSION_CHANGED,
            )
        goal = request.goal_state
        if (goal is None
                or not frame.body.is_on_ground
                or frame.body.pose != "standing"
                or observed_ground_mode(frame.body) is not MovementMode.WALK
                or MovementMode.WALK not in goal.allowed_modes
                or "standing" not in goal.allowed_poses):
            return AdmissionResult(
                AdmissionStatus.NOT_APPLICABLE,
                AdmissionReason.CANDIDATE_BASIS_MISMATCH,
            )
        start_status, start_surface, _, start_missing = (
            self._surface_for_local_body(request, frame)
        )
        if start_status is QueryStatus.NEEDS_INFORMATION:
            return AdmissionResult(
                AdmissionStatus.NEEDS_INFORMATION,
                AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION,
                missing_cells=start_missing,
            )
        if start_status is not QueryStatus.FEASIBLE:
            return AdmissionResult(
                AdmissionStatus.NOT_APPLICABLE,
                AdmissionReason.CURRENT_BODY_CANNOT_CONNECT,
            )
        assert start_surface is not None
        target_surfaces = query_support_surfaces(
            frame.world,
            request.goal.column_x,
            request.goal.column_z,
            goal.region.min_y,
            goal.region.max_y,
        )
        if target_surfaces.status is QueryStatus.NEEDS_INFORMATION:
            return AdmissionResult(
                AdmissionStatus.NEEDS_INFORMATION,
                AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION,
                missing_cells=target_surfaces.missing_cells,
            )
        if target_surfaces.status is not QueryStatus.FEASIBLE:
            return AdmissionResult(
                AdmissionStatus.NOT_APPLICABLE,
                AdmissionReason.GOAL_STANDING_POINT_UNAVAILABLE,
            )
        target_surface = next((
            surface for surface in target_surfaces.surfaces
            if surface.node_id == request.goal
        ), None)
        if (target_surface is None
                or abs(target_surface.position[1]
                       - start_surface.position[1]) > 1.0e-9):
            return AdmissionResult(
                AdmissionStatus.NOT_APPLICABLE,
                AdmissionReason.CANDIDATE_BASIS_MISMATCH,
            )
        selected = self._goal_completion(frame.world, target_surface, goal, ground_profile,
                                         frame.body.position)
        if selected.status is QueryStatus.NEEDS_INFORMATION:
            return AdmissionResult(
                AdmissionStatus.NEEDS_INFORMATION,
                AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION,
                missing_cells=selected.missing_cells,
            )
        if selected.status is not QueryStatus.FEASIBLE:
            return AdmissionResult(
                AdmissionStatus.NOT_APPLICABLE,
                AdmissionReason.GOAL_STANDING_POINT_UNAVAILABLE,
            )
        assert selected.position is not None
        if math.dist(frame.body.position, selected.position) \
                > self.maximum_corridor_blocks + 1.0e-9:
            return AdmissionResult(
                AdmissionStatus.NOT_APPLICABLE,
                AdmissionReason.CANDIDATE_BASIS_MISMATCH,
            )
        direct = query_standable_connection(
            frame.world,
            target_surface,
            selected.position,
            frame.body.position,
        )
        if direct.status is QueryStatus.NEEDS_INFORMATION:
            return AdmissionResult(
                AdmissionStatus.NEEDS_INFORMATION,
                AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION,
                missing_cells=direct.missing_cells,
            )
        if direct.status is not QueryStatus.FEASIBLE:
            return AdmissionResult(
                AdmissionStatus.NOT_APPLICABLE,
                AdmissionReason.CURRENT_BODY_CANNOT_CONNECT,
            )
        if not ground_profile_allows_dependency_blocks(
                ground_profile, frame.world, direct.dependencies):
            return AdmissionResult(
                AdmissionStatus.NOT_APPLICABLE,
                AdmissionReason.ROUTE_CAPABILITIES_CHANGED,
            )
        return self._direct_walk_result(
            request,
            frame,
            endpoint_surface=target_surface,
            endpoint=selected.position,
            dependencies=direct.dependencies,
            ground_profile=ground_profile,
            capability_identity=capability_identity,
            route_suffix="direct",
            node_ids=(request.start, request.goal),
            completion_region=selected.completion_region,
        )

    def admit_local_direct(
        self,
        request: SurfacePlanningRequest,
        frame: NavigationFrame,
        *,
        ground_profile: GroundMotionProfile,
        capability_identity: GroundCapabilityIdentity,
    ) -> AdmissionResult:
        """Build one proved same-support direct Walk through admission."""
        if (type(request) is not SurfacePlanningRequest
                or type(frame) is not NavigationFrame
                or type(ground_profile) is not GroundMotionProfile
                or type(capability_identity)
                    is not GroundCapabilityIdentity):
            raise ContractViolation(
                "local direct admission requires typed request, frame, and profile"
            )
        if capability_identity != GroundCapabilityIdentity.from_profile(
                ground_profile):
            return self._local_result(
                request, frame, LocalDirectAdmissionPhase.REQUEST,
                AdmissionStatus.REJECTED,
                AdmissionReason.ROUTE_CAPABILITIES_CHANGED,
            )
        if request.world_session != frame.session.value:
            return self._local_result(
                request, frame, LocalDirectAdmissionPhase.REQUEST,
                AdmissionStatus.REJECTED,
                AdmissionReason.WORLD_SESSION_CHANGED,
            )
        if request.start != request.goal or request.goal_state is None:
            return self._local_result(
                request, frame, LocalDirectAdmissionPhase.REQUEST,
                AdmissionStatus.REJECTED,
                AdmissionReason.CANDIDATE_BASIS_MISMATCH,
            )
        status, surface, _, missing = self._surface_for_local_body(
            request, frame,
        )
        if status is not QueryStatus.FEASIBLE:
            return self._local_rejection(
                request,
                frame,
                LocalDirectAdmissionPhase.BODY_SURFACE,
                status,
                missing_cells=missing,
            )
        assert surface is not None
        if surface.node_id != request.goal:
            return self._local_result(
                request,
                frame,
                LocalDirectAdmissionPhase.SURFACE_IDENTITY,
                AdmissionStatus.REJECTED,
                AdmissionReason.CURRENT_BODY_CANNOT_CONNECT,
                body_surface_node=surface.node_id,
            )
        goal = request.goal_state
        selected = self._goal_completion(frame.world, surface, goal, ground_profile,
                                         frame.body.position)
        if selected.status is not QueryStatus.FEASIBLE:
            return self._local_rejection(
                request,
                frame,
                LocalDirectAdmissionPhase.GOAL_SELECTION,
                selected.status,
                missing_cells=selected.missing_cells,
                body_surface_node=surface.node_id,
                selected_position=selected.position,
                selector_status=selected.status,
            )
        assert selected.position is not None
        endpoint = selected.position
        direct = query_standable_connection(
            frame.world,
            surface,
            endpoint,
            frame.body.position,
        )
        if direct.status is not QueryStatus.FEASIBLE:
            return self._local_rejection(
                request,
                frame,
                LocalDirectAdmissionPhase.EXACT_CONNECTION,
                direct.status,
                missing_cells=direct.missing_cells,
                body_surface_node=surface.node_id,
                selected_position=endpoint,
                selector_status=selected.status,
                exact_status=direct.status,
                exact_dependency_count=len(direct.dependencies),
            )
        if not ground_profile_allows_dependency_blocks(
                ground_profile, frame.world, direct.dependencies):
            return self._local_rejection(
                request,
                frame,
                LocalDirectAdmissionPhase.MATERIAL_CAPABILITY,
                QueryStatus.UNSUPPORTED,
                body_surface_node=surface.node_id,
                selected_position=endpoint,
                selector_status=selected.status,
                exact_status=direct.status,
                exact_dependency_count=len(direct.dependencies),
            )

        length = math.dist(frame.body.position, endpoint)
        if length <= 1.0e-9:
            return self._local_result(
                request,
                frame,
                LocalDirectAdmissionPhase.ROUTE_BUILD,
                AdmissionStatus.REJECTED,
                AdmissionReason.CANDIDATE_HAS_NO_ACTIONS,
                body_surface_node=surface.node_id,
                selected_position=endpoint,
                selector_status=selected.status,
                exact_status=direct.status,
                exact_dependency_count=len(direct.dependencies),
            )
        result = self._direct_walk_result(
            request,
            frame,
            endpoint_surface=surface,
            endpoint=endpoint,
            dependencies=direct.dependencies,
            ground_profile=ground_profile,
            capability_identity=capability_identity,
            route_suffix="local",
            node_ids=(request.start,),
            completion_region=selected.completion_region,
        )
        return self._local_result(
            request,
            frame,
            LocalDirectAdmissionPhase.ROUTE_BUILD,
            result.status,
            result.reason,
            route=result.route,
            missing_cells=result.missing_cells,
            body_surface_node=surface.node_id,
            selected_position=endpoint,
            selector_status=selected.status,
            exact_status=direct.status,
            exact_dependency_count=len(direct.dependencies),
        )

    @staticmethod
    def bind_verified_motion(
            route: ActiveRoute, proof: VerifiedMotionResult, *,
            action_index: int, candidate_revision: int,
            damage_budget: TaskDamageBudget = TaskDamageBudget(),
            accepted_resource_incomplete_reasons: tuple[str, ...] = (),
    ) -> VerifiedMotionCandidate:
        if type(route) is not ActiveRoute or type(proof) is not VerifiedMotionResult:
            raise ContractViolation("verified motion binding requires route and proof")
        if type(action_index) is not int or not 0 <= action_index < len(
                route.action_route.actions):
            raise ContractViolation("verified motion action index is outside the route")
        action = route.action_route.actions[action_index]
        expected_kind = action_spec(action).solve_kind
        if expected_kind is None or proof.kind is not expected_kind:
            raise ContractViolation(
                "verified motion kind does not match its route action"
            )
        return VerifiedMotionCandidate(
            proof,
            MotionCandidateContext(
                route.source_request_id, route.planning_generation,
                route.goal_id, route.goal_revision,
                route.route_id, route.route_revision, action_index,
                candidate_revision, damage_budget,
                accepted_resource_incomplete_reasons,
            ),
        )

    def admit_verified_motion(
            self, candidate: VerifiedMotionCandidate, route: ActiveRoute,
            anchor: StateAnchor, *, candidate_revision: int,
            intended_start_tick: int,
            changed_cells: tuple[BlockPos, ...],
            damage_budget: TaskDamageBudget = TaskDamageBudget(),
            world: PhysicsWorldView | None = None,
            input_ledger: InputApplicationLedger | None = None,
    ) -> MotionCandidateAdmission:
        if type(route) is not ActiveRoute:
            raise ContractViolation("verified motion admission requires an active route")
        admission = self._motion_admitter.admit(
            candidate, anchor,
            planning_request_id=route.source_request_id,
            planning_generation=route.planning_generation,
            goal_id=route.goal_id, goal_revision=route.goal_revision,
            route_id=route.route_id, route_revision=route.route_revision,
            action_index=candidate.context.action_index,
            candidate_revision=candidate_revision,
            damage_budget=damage_budget,
            intended_start_tick=intended_start_tick,
            changed_cells=changed_cells,
            input_ledger=input_ledger,
        )
        # A changed anchor is a domain result. The coordinator may submit a
        # bounded worker revalidation; admission never rolls out physics here.
        return admission

    @staticmethod
    def _route_id(candidate: RouteCandidate) -> str:
        identity=dict(world=candidate.world_session,goal=candidate.goal_id,
                      goal_revision=candidate.goal_revision,
                      path=[node.node_id for node in candidate.path],
                      actions=[{
                      "kind": "jump_up" if type(edge) is JumpUpEdge else "walk",
                      "start": edge.start,
                      "end": edge.end,
                      "profile": (edge.profile_id if type(edge) is JumpUpEdge
                                  else (edge.transition.trajectory_profile_id
                                        if edge.transition is not None else None)),
                      "mode": (None if type(edge) is JumpUpEdge or edge.transition is None
                               else edge.transition.mode.value),
                      } for edge in candidate.segments])
        return hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:24]

    @staticmethod
    def _simplified_path(candidate: RouteCandidate):
        if len(candidate.path)<=2:return candidate.path
        kept=[candidate.path[0]]
        previous_direction=None
        for first,second in zip(candidate.path,candidate.path[1:]):
            direction=(second.node_id[0]-first.node_id[0],
                       second.node_id[1]-first.node_id[1],
                       second.node_id[2]-first.node_id[2])
            if previous_direction is not None and direction!=previous_direction:
                kept.append(first)
            previous_direction=direction
        kept.append(candidate.path[-1])
        return tuple(kept)

    @staticmethod
    def _simplified_nodes(nodes: tuple[WalkNode, ...]) -> tuple[WalkNode, ...]:
        if len(nodes) <= 2:
            return nodes
        kept = [nodes[0]]
        previous_direction = None
        for first, second in zip(nodes, nodes[1:]):
            direction = (
                second.node_id[0] - first.node_id[0],
                second.node_id[1] - first.node_id[1],
                second.node_id[2] - first.node_id[2],
            )
            if previous_direction is not None and direction != previous_direction:
                kept.append(first)
            previous_direction = direction
        kept.append(nodes[-1])
        return tuple(kept)

    @classmethod
    def _action_route(cls, candidate: RouteCandidate, frame: NavigationFrame,
                      connection_length: float,
                      connection_dependencies: tuple[BlockPos, ...],
                      route_id: str) -> ActionRoute | None:
        actions = []
        pending_nodes = [candidate.path[0]]
        pending_points = []
        pending_dependencies = set(connection_dependencies)
        pending_transitions = []
        if connection_length > 1e-9:
            pending_points.append(RoutePoint(
                frame.body.position[0], candidate.path[0].position[1],
                frame.body.position[2],
            ))
        pending_points.append(RoutePoint(*candidate.path[0].position))

        def flush_walk() -> None:
            nonlocal pending_nodes, pending_points, pending_dependencies, pending_transitions
            if len(pending_points) >= 2:
                if connection_length > 1e-9 and len(actions) == 0:
                    graph_nodes = tuple(pending_nodes)
                    route_points = tuple(pending_points)
                else:
                    simplified = cls._simplified_nodes(tuple(pending_nodes))
                    graph_nodes = tuple(node.node_id for node in pending_nodes)
                    route_points = tuple(RoutePoint(*node.position) for node in simplified)
                actions.append(WalkSegment(
                    FixedRoute(f"{route_id}-walk-{len(actions)}", route_points),
                    tuple(node.node_id for node in pending_nodes),
                    tuple(sorted(pending_dependencies)),
                    ((pending_transitions[0] if len(pending_transitions) == 1
                      else compose_movement_transitions(
                          f"{route_id}/walk/{len(actions)}", tuple(pending_transitions),
                      )) if pending_transitions
                     and len(pending_transitions) == len(pending_nodes) - 1
                     else None),
                ))
            pending_nodes = []
            pending_points = []
            pending_dependencies = set()
            pending_transitions = []

        for segment_index, (edge, next_node) in enumerate(
                zip(candidate.segments, candidate.path[1:])):
            if type(edge) is WalkEdge:
                if (pending_transitions and edge.transition is not None
                        and edge.transition.mode is not pending_transitions[-1].mode):
                    flush_walk()
                changes_resources = (edge.transition is not None and any(
                    abs(delta) > 1.0e-12
                    for _, delta in edge.transition.resource_change.deltas
                ))
                if changes_resources:
                    flush_walk()
                if not pending_nodes:
                    previous = candidate.path[segment_index]
                    pending_nodes = [previous]
                    pending_points = [RoutePoint(*previous.position)]
                pending_nodes.append(next_node)
                pending_points.append(RoutePoint(*next_node.position))
                pending_dependencies.update(edge.dependencies)
                pending_dependencies.update(next_node.dependencies)
                if edge.transition is not None:
                    pending_transitions.append(edge.transition)
                if changes_resources:
                    flush_walk()
            else:
                flush_walk()
                assert type(edge) is JumpUpEdge
                actions.append(JumpUpSegment(edge, edge.dependencies, edge.transition))
                pending_nodes = [next_node]
                pending_points = [RoutePoint(*next_node.position)]
                pending_dependencies = set(next_node.dependencies)
        flush_walk()
        return (ActionRoute(
            route_id, tuple(actions), candidate.goal_state,
            candidate.final_resources if candidate.final_resources is not None else ResourceState(),
        ) if actions else None)

    @staticmethod
    def _connection(candidate: RouteCandidate, frame: NavigationFrame
                    ) -> tuple[bool, float, tuple[BlockPos, ...]]:
        first=candidate.path[0].position
        dx,dz=first[0]-frame.body.position[0],first[2]-frame.body.position[2]
        if abs(first[1]-frame.body.position[1])>.10 or math.hypot(dx,dz)>.80:
            return False,0.0,()
        movement=sweep(frame.body.body_box,(dx,0.0,dz),frame.world)
        dependencies=set(movement.dependencies)
        if movement.status is not QueryStatus.FEASIBLE:
            return False,0.0,tuple(sorted(dependencies))
        for fraction in (.5,1.0):
            support=query_support(frame.body.body_box.moved(dx*fraction,0.0,dz*fraction),frame.world)
            dependencies.update(support.dependencies)
            if support.status is not QueryStatus.FEASIBLE:
                return False,0.0,tuple(sorted(dependencies))
        return True,math.hypot(dx,dz),tuple(sorted(dependencies))

    def admit(self, candidate: RouteCandidate, frame: NavigationFrame, *,
              expected_request_id: str, goal_id: str,
              goal_revision: int, changed_cells: tuple[BlockPos,...]) -> AdmissionResult:
        if type(candidate) is not RouteCandidate or type(frame) is not NavigationFrame:
            raise ContractViolation("route admission requires a candidate and navigation frame")
        if type(changed_cells) is not tuple:
            raise ContractViolation("route changes must be immutable")
        if candidate.status is not PlanningStatus.COMPLETE or not candidate.path:
            return AdmissionResult(AdmissionStatus.REJECTED,AdmissionReason.CANDIDATE_NOT_COMPLETE)
        if candidate.request_id!=expected_request_id:
            return AdmissionResult(AdmissionStatus.REJECTED,AdmissionReason.PLANNING_REQUEST_REPLACED)
        if candidate.world_session!=frame.session.value:
            return AdmissionResult(AdmissionStatus.REJECTED,AdmissionReason.WORLD_SESSION_CHANGED)
        if candidate.goal_id!=goal_id or candidate.goal_revision!=goal_revision:
            return AdmissionResult(AdmissionStatus.REJECTED,AdmissionReason.GOAL_REVISION_CHANGED)
        if frame.world.geometry_revision!=candidate.geometry_revision and not changed_cells:
            return AdmissionResult(AdmissionStatus.REJECTED,AdmissionReason.WORLD_DELTA_MISSING)
        if set(candidate.dependencies).intersection(changed_cells):
            return AdmissionResult(AdmissionStatus.REJECTED,AdmissionReason.ROUTE_DEPENDENCIES_CHANGED)
        connected,connection_length,connection_dependencies=self._connection(candidate,frame)
        if not connected:
            return AdmissionResult(AdmissionStatus.REJECTED,AdmissionReason.CURRENT_BODY_CANNOT_CONNECT)

        length=0.0;corridor_nodes=[candidate.path[0]];corridor_segments=[]
        for edge,node in zip(candidate.segments,candidate.path[1:]):
            segment_length=math.dist(candidate.path[len(corridor_segments)].position,node.position)
            if (corridor_segments and connection_length+length+segment_length
                    > self.maximum_corridor_blocks):
                break
            corridor_segments.append(edge);corridor_nodes.append(node);length+=segment_length
        dependencies=tuple(sorted(
            {cell for node in corridor_nodes for cell in node.dependencies}
            | {cell for edge in corridor_segments for cell in edge.dependencies}
            | set(connection_dependencies)
        ))
        route_id=self._route_id(candidate)
        action_route=self._action_route(
            candidate,frame,connection_length,connection_dependencies,route_id,
        )
        if action_route is None:
            return AdmissionResult(AdmissionStatus.REJECTED,AdmissionReason.CANDIDATE_HAS_NO_ACTIONS)
        fixed_route=(action_route.actions[0].fixed_route
                     if len(action_route.actions)==1
                     and type(action_route.actions[0]) is WalkSegment else None)
        full_length=connection_length+sum(
            math.dist(first.position,second.position)
            for first,second in zip(candidate.path,candidate.path[1:]))
        corridor=ExecutableCorridor(tuple(node.node_id for node in corridor_nodes),dependencies,
                                    connection_length+length,corridor_nodes[-1].node_id)
        active=ActiveRoute(route_id,1,candidate.request_id,candidate.goal_id,
                           candidate.goal_revision,candidate.world_session,fixed_route,
                           full_length,connection_length,connection_dependencies,corridor,
                           action_route,candidate.goal_state,candidate.request_sequence,
                           candidate.work_identity)
        return AdmissionResult(AdmissionStatus.ACCEPTED,AdmissionReason.CANDIDATE_ADMITTED,active)

    @staticmethod
    def _replay_resources(candidate, initial: ResourceState, minimum: ResourceState,
                          frame: NavigationFrame) -> ResourceState | AdmissionReason:
        names = set(initial.as_dict()) | set(minimum.as_dict())
        for edge in candidate.segments:
            names.update(name for name, _ in edge.resource_change.deltas)
            if edge.transition is not None:
                names.update(name for name, _ in edge.transition.minimum_entry_resources.values)
        observed = []
        for name in sorted(names):
            if name == MOVEMENT_DAMAGE_BUDGET_RESOURCE:
                if name not in initial.as_dict():
                    return AdmissionReason.ROUTE_RESOURCES_UNAVAILABLE
                value = initial.as_dict()[name]
            elif name == "food_points":
                value = float(frame.body.food_points)
            else:
                return AdmissionReason.ROUTE_RESOURCE_UNOBSERVABLE
            observed.append((name, value))
        resources = capacity = ResourceState(tuple(observed))
        if not resources.at_least(minimum):
            return AdmissionReason.ROUTE_RESOURCES_BELOW_MINIMUM
        for edge in candidate.segments:
            if edge.transition is not None and not resources.at_least(edge.transition.minimum_entry_resources):
                return AdmissionReason.ROUTE_ENTRY_RESOURCES_UNAVAILABLE
            updated = resources.apply(edge.resource_change, minimum, capacity)
            if updated is None:
                return AdmissionReason.ROUTE_RESOURCES_UNAVAILABLE
            resources = updated
        return resources

    def admit_current_request(
        self, candidate: RouteCandidate | SurfaceRouteCandidate,
        calculation_request: PlanningRequest | SurfacePlanningRequest,
        current_request: PlanningRequest | SurfacePlanningRequest,
        frame: NavigationFrame, *, remaining_damage_budget: TaskDamageBudget,
        changed_cells: tuple[BlockPos, ...],
        edge_probe: LandingEdgeProbe | None = None,
    ) -> AdmissionResult:
        """Recheck the whole original positive route, then bind its proven exit.

        Identity/time eligibility belongs to the caller's AsyncWorkLifecycle.
        This boundary never cuts a path or rebuilds a tail for the new goal.
        """
        surface = type(candidate) is SurfaceRouteCandidate
        request_type = SurfacePlanningRequest if surface else PlanningRequest
        if (type(candidate) not in (RouteCandidate, SurfaceRouteCandidate)
                or type(calculation_request) is not request_type
                or type(current_request) is not request_type
                or type(frame) is not NavigationFrame
                or type(remaining_damage_budget) is not TaskDamageBudget
                or type(changed_cells) is not tuple):
            raise ContractViolation("current route binding requires typed producer and current facts")
        complete = SurfacePlanningStatus.COMPLETE if surface else PlanningStatus.COMPLETE
        if candidate.status is not complete or not candidate.path:
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.CANDIDATE_NOT_COMPLETE)
        if (candidate.request_id != calculation_request.request_id
                or candidate.request_sequence != calculation_request.sequence
                or candidate.goal_id != calculation_request.goal_id
                or candidate.goal_revision != calculation_request.goal_revision
                or candidate.world_session != calculation_request.world_session
                or candidate.planning_start != calculation_request.start
                or candidate.planning_goal != calculation_request.goal
                or candidate.goal_state != calculation_request.goal_state
                or candidate.work_identity != calculation_request.work_identity
                or (surface and (
                    candidate.initial_resources != calculation_request.initial_resources
                    or candidate.minimum_resources != calculation_request.minimum_resources))):
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.CANDIDATE_BASIS_MISMATCH)
        if current_request.world_session != frame.session.value:
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.WORLD_SESSION_CHANGED)
        original_policy = (calculation_request.damage_budget.risk_policy_id if surface
            else (candidate.goal_state.risk_policy_id if candidate.goal_state else "no_expected_damage"))
        if (original_policy != remaining_damage_budget.risk_policy_id
                or (surface and current_request.damage_budget.risk_policy_id != original_policy)):
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.ROUTE_RISK_POLICY_CHANGED)
        minimum = current_request.minimum_resources
        if current_request.goal_state is not None:
            if current_request.goal_state.risk_policy_id != remaining_damage_budget.risk_policy_id:
                return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.ROUTE_RISK_POLICY_CHANGED)
            values = minimum.as_dict()
            for name, value in current_request.goal_state.minimum_resources.values:
                values[name] = max(values.get(name, 0.), value)
            minimum = ResourceState(tuple(sorted(values.items())))
        initial_values = calculation_request.initial_resources.as_dict()
        initial_values.update(current_request.initial_resources.as_dict())
        if (MOVEMENT_DAMAGE_BUDGET_RESOURCE in initial_values
                or remaining_damage_budget.maximum_expected_damage_points > 0):
            initial_values[MOVEMENT_DAMAGE_BUDGET_RESOURCE] = remaining_damage_budget.maximum_expected_damage_points
        initial = ResourceState(tuple(sorted(initial_values.items())))
        predicted_damage = sum(getattr(edge, "predicted_damage_points", 0.) for edge in candidate.segments)
        # Health/absorption evidence remains the motion proof's responsibility;
        # route admission rechecks the task's remaining damage capacity here.
        if predicted_damage > remaining_damage_budget.maximum_expected_damage_points:
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.ROUTE_RESOURCES_UNAVAILABLE)
        if surface:
            checked = replace(candidate, initial_resources=initial, minimum_resources=minimum)
            admitted = self.admit_surface(checked, frame,
                expected_request_id=calculation_request.request_id,
                goal_id=calculation_request.goal_id, goal_revision=calculation_request.goal_revision,
                changed_cells=changed_cells, edge_probe=edge_probe)
        else:
            resources = self._replay_resources(candidate, initial, minimum, frame)
            if type(resources) is AdmissionReason:
                return AdmissionResult(AdmissionStatus.REJECTED, resources)
            admitted = self.admit(replace(candidate, final_resources=resources), frame,
                expected_request_id=calculation_request.request_id,
                goal_id=calculation_request.goal_id, goal_revision=calculation_request.goal_revision,
                changed_cells=changed_cells)
        if admitted.status is not AdmissionStatus.ACCEPTED:
            return admitted
        route = admitted.route
        assert route is not None
        resources = route.action_route.final_resources
        if current_request.request_id != calculation_request.request_id:
            if not self._body_meets_route_entry(route, frame):
                return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.CURRENT_BODY_CANNOT_CONNECT)
            if not self._terminal_meets_current_goal(route, candidate, current_request, resources,
                    remaining_damage_budget):
                return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.CURRENT_GOAL_NOT_SATISFIED)
        route = replace(route, source_request_id=current_request.request_id,
            goal_id=current_request.goal_id, goal_revision=current_request.goal_revision,
            planning_generation=current_request.sequence, goal_state=current_request.goal_state,
            action_route=replace(route.action_route, goal_state=current_request.goal_state,
                                 final_resources=resources))
        return AdmissionResult(AdmissionStatus.ACCEPTED, AdmissionReason.CANDIDATE_ADMITTED, route)

    @staticmethod
    def _body_meets_route_entry(route: ActiveRoute, frame: NavigationFrame) -> bool:
        if not frame.body.is_on_ground:
            return False
        mode = observed_ground_mode(frame.body)
        if mode is None:
            return False
        first = route.action_route.actions[0]
        window = (first.traversal_plan.entry_window
                  if type(first) is WalkSegment and first.traversal_plan is not None
                  else getattr(first, 'entry_window', None))
        if window is not None:
            return body_fits_segment_entry(window, frame.body, mode)
        if type(first) is not WalkSegment or first.transition is None:
            return False
        entry = first.transition.entry
        speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                           frame.body.velocity_blocks_per_second[2])
        return (mode is entry.mode and frame.body.pose == entry.pose
            and entry.minimum_speed_blocks_per_second <= speed + 1.e-12
            and speed <= entry.maximum_speed_blocks_per_second + 1.e-12)

    @staticmethod
    def _terminal_meets_current_goal(route, candidate, request, resources, damage_budget) -> bool:
        goal = request.goal_state
        if goal is None:
            return candidate.planning_goal == request.goal
        last = route.action_route.actions[-1]
        if type(last) is WalkSegment:
            point = last.fixed_route.points[-1]
            position = (point.x, point.y, point.z)
        elif type(last) is JumpUpSegment:
            position = last.edge.end
        else:
            position = last.end_surface.position
        transition = last.transition
        if transition is None or goal.required_yaw_radians is not None:
            # The current route contract has no yaw exit range. A path's
            # direction alone is not evidence of the player's final yaw.
            return False
        return all(goal.accepts(position=position, support=GoalSupport.SOLID,
            mode=exit_state.mode, pose=exit_state.pose,
            speed_blocks_per_second=exit_state.maximum_speed_blocks_per_second,
            resources=resources, applied_risk_policy_id=damage_budget.risk_policy_id)
            for exit_state in transition.exits)

    @staticmethod
    def _surface_route_id(candidate: SurfaceRouteCandidate) -> str:
        def node_value(node_id):
            return [node_id.column_x, node_id.column_z,
                    node_id.vertical_band, node_id.surface_index]
        identity = {
            "world": candidate.world_session,
            "goal": candidate.goal_id,
            "goal_revision": candidate.goal_revision,
            "path": [node_value(node.node_id) for node in candidate.path],
            "actions": [{
                "kind": ("step" if type(edge) is StepEdge else
                          "jump_up" if type(edge) is SurfaceJumpUpEdge else
                          "jump_gap" if type(edge) is SurfaceJumpGapEdge else
                          "controlled_drop"
                          if type(edge) is SurfaceControlledDropEdge else "walk"),
                "start": node_value(edge.start),
                "end": node_value(edge.end),
                "profile": (edge.profile_id if type(edge) is StepEdge else
                            edge.jump_edge.profile_id
                            if type(edge) is SurfaceJumpUpEdge else
                            edge.air_edge.profile_id
                            if type(edge) in (SurfaceJumpGapEdge,
                                              SurfaceControlledDropEdge) else None),
            } for edge in candidate.segments],
        }
        return hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()[:24]

    @classmethod
    def _surface_action_route(
        cls,
        candidate: SurfaceRouteCandidate,
        frame: NavigationFrame,
        connection_length: float,
        connection_dependencies: tuple[BlockPos, ...],
        route_id: str,
        terminal_target=None,
        skip_first_walk_start: bool = False,
        forward_entry: _ForwardGroundEntry | None = None,
        terminal_proofs: list[_StandableQueryProof] | None = None,
        terminal_execution_dependencies: list[
            _ExactTerminalExecutionDependencies
        ] | None = None,
    ) -> ActionRoute | None:
        def entry_window(previous, next_node, transition) -> SegmentEntryWindow:
            if type(transition) is not MovementTransition:
                raise ContractViolation(
                    "formal height transition requires an entry transition"
                )
            dx = next_node.position[0] - previous.position[0]
            dz = next_node.position[2] - previous.position[2]
            length = math.hypot(dx, dz)
            if length <= 1.0e-9:
                raise ContractViolation(
                    "height transition entry requires a horizontal direction"
                )
            entry = transition.entry
            return SegmentEntryWindow(
                reference_point=previous.position,
                horizontal_approach_direction=(dx / length, dz / length),
                minimum_longitudinal_offset_blocks=-0.25,
                maximum_longitudinal_offset_blocks=0.05,
                maximum_lateral_offset_blocks=0.25,
                minimum_feet_y=previous.position[1] - 0.10,
                maximum_feet_y=previous.position[1] + 0.10,
                minimum_speed_blocks_per_second=(
                    entry.minimum_speed_blocks_per_second
                ),
                maximum_speed_blocks_per_second=(
                    entry.maximum_speed_blocks_per_second
                ),
                maximum_velocity_direction_error_radians=math.radians(30.0),
                allowed_poses=frozenset({entry.pose}),
                allowed_modes=frozenset({entry.mode}),
                required_yaw_radians=None,
                maximum_yaw_error_radians=None,
                profile_id=(
                    transition.trajectory_profile_id
                    or transition.transition_id
                ),
            )

        actions = []
        entry_index = 1 if skip_first_walk_start else 0
        pending_nodes = [candidate.path[entry_index]]
        pending_points = []
        pending_dependencies = set(connection_dependencies)
        pending_transitions = []
        if connection_length > 1.0e-9:
            pending_points.append(RoutePoint(
                frame.body.position[0], candidate.path[entry_index].position[1],
                frame.body.position[2],
            ))
        pending_points.append(RoutePoint(*candidate.path[entry_index].position))

        def flush_walk() -> None:
            nonlocal pending_nodes, pending_points, pending_dependencies, pending_transitions
            if len(pending_points) >= 2:
                fixed_route = FixedRoute(
                    f"{route_id}-walk-{len(actions)}", tuple(pending_points)
                )
                traversal_plan = next((
                    plan for plan in candidate.ground_traversal_plans
                    if canonical_surface_node_path(plan.surface_node_path)
                    == tuple(node.node_id for node in pending_nodes)
                ), None)
                if traversal_plan is not None:
                    # Execute the concrete entry route that the calculator
                    # proved.  Rebuilding it from graph-node centres would
                    # discard an overhanging or offset current position.
                    fixed_route = traversal_plan.route
                actions.append(WalkSegment(
                    fixed_route,
                    tuple(node.node_id for node in pending_nodes),
                    tuple(sorted(pending_dependencies)),
                    ((pending_transitions[0] if len(pending_transitions) == 1
                      else compose_movement_transitions(
                          f"{route_id}/walk/{len(actions)}", tuple(pending_transitions),
                      )) if pending_transitions
                     and len(pending_transitions) == len(pending_nodes) - 1
                     else None),
                    traversal_plan,
                ))
            pending_nodes = []
            pending_points = []
            pending_dependencies = set()
            pending_transitions = []

        for index, (edge, next_node) in enumerate(zip(
                candidate.segments, candidate.path[1:])):
            if skip_first_walk_start and index == 0:
                continue
            if type(edge) is SurfaceWalkEdge:
                if (pending_transitions
                        and edge.transition.mode is not pending_transitions[-1].mode):
                    flush_walk()
                if not pending_nodes:
                    previous = candidate.path[index]
                    pending_nodes = [previous]
                    pending_points = [RoutePoint(*previous.position)]
                pending_nodes.append(next_node)
                pending_points.append(RoutePoint(*next_node.position))
                pending_dependencies.update(edge.dependencies)
                pending_dependencies.update(next_node.dependencies)
                pending_transitions.append(edge.transition)
            else:
                flush_walk()
                previous = candidate.path[index]
                if type(edge) is StepEdge:
                    actions.append(StepSegment(
                        edge, previous.surface, next_node.surface,
                        edge.dependencies, edge.transition,
                        entry_window(previous, next_node, edge.transition),
                    ))
                elif type(edge) is SurfaceJumpUpEdge:
                    actions.append(JumpUpSegment(
                        edge.jump_edge, edge.dependencies, edge.transition,
                        edge.entry_window,
                    ))
                elif type(edge) is SurfaceJumpGapEdge:
                    actions.append(JumpGapSegment(
                        edge.air_edge, previous.surface, next_node.surface,
                        edge.dependencies, edge.transition,
                        edge.entry_window,
                    ))
                else:
                    assert type(edge) is SurfaceControlledDropEdge
                    actions.append(ControlledDropSegment(
                        edge.air_edge, previous.surface, next_node.surface,
                        edge.dependencies, edge.transition,
                        edge.entry_window,
                    ))
                pending_nodes = [next_node]
                pending_points = [RoutePoint(*next_node.position)]
                pending_dependencies = set(next_node.dependencies)
        exact_terminal_execution = None
        terminal_matches_last = False
        if terminal_target is not None:
            terminal = RoutePoint(*terminal_target.position)
            last = pending_points[-1]
            preterminal_dependencies = tuple(sorted(pending_dependencies))
            if last != terminal:
                has_proof = any(canonical_surface_node_path(plan.surface_node_path)
                                == tuple(node.node_id for node in pending_nodes)
                                for plan in candidate.ground_traversal_plans)
                if has_proof:
                    # Preserve the proved trajectory; its final same-height
                    # connection is a separate ordinary closed-loop walk.
                    tail_proof = next(plan for plan in candidate.ground_traversal_plans
                        if canonical_surface_node_path(plan.surface_node_path)
                        == tuple(node.node_id for node in pending_nodes))
                    tail_start = tail_proof.route.points[-1]
                    tail_start_position = (
                        tail_start.x, tail_start.y, tail_start.z,
                    )
                    direct = query_standable_connection(
                        frame.world,
                        candidate.path[-1].surface,
                        terminal_target.position,
                        tail_start_position,
                    )
                    tail_id = (tail_proof.continuation.following_route_id
                               if tail_proof.continuation is not None else f"{route_id}-goal-tail")
                    flush_walk()
                    if direct.status is QueryStatus.FEASIBLE:
                        tail_action_index = len(actions)
                        actions.append(WalkSegment(
                            FixedRoute(tail_id, (tail_start, terminal)),
                            (candidate.path[-1].node_id,),
                            tuple(sorted(set(direct.dependencies))),
                        ))
                        exact_terminal_execution = (
                            _ExactTerminalExecutionDependencies(
                                tail_action_index,
                                (),
                                tuple(sorted(set(direct.dependencies))),
                            )
                        )
                        if terminal_proofs is not None:
                            terminal_proofs.append(_StandableQueryProof(
                                StandableConnectionQueryArgs(
                                    candidate.path[-1].surface,
                                    terminal_target.position,
                                    tail_start_position,
                                    .6,
                                    1.8,
                                ),
                                direct.dependencies,
                            ))
                    else:
                        pending_dependencies.update(
                            terminal_target.dependencies
                        )
                        actions.append(WalkSegment(
                            FixedRoute(tail_id, (last, terminal)),
                            (candidate.path[-1].node_id,),
                            terminal_target.dependencies,
                        ))
                else:
                    direct = None
                    direct_from = None
                    append_terminal = False
                    if len(pending_points) >= 2:
                        before = pending_points[-2]
                        direct_from = (before.x, before.y, before.z)
                        direct = query_standable_connection(frame.world,
                            candidate.path[-1].surface, terminal_target.position,
                            direct_from)
                        if direct.status is not QueryStatus.FEASIBLE:
                            last_point = pending_points[-1]
                            direct_from = (
                                last_point.x, last_point.y, last_point.z,
                            )
                            direct = query_standable_connection(
                                frame.world,
                                candidate.path[-1].surface,
                                terminal_target.position,
                                direct_from,
                            )
                            append_terminal = (
                                direct.status is QueryStatus.FEASIBLE
                            )
                    if direct is not None and direct.status is QueryStatus.FEASIBLE:
                        if append_terminal:
                            pending_points.append(terminal)
                        else:
                            pending_points[-1] = terminal
                        pending_dependencies.update(direct.dependencies)
                        exact_terminal_execution = (
                            _ExactTerminalExecutionDependencies(
                                len(actions),
                                preterminal_dependencies,
                                tuple(sorted(set(direct.dependencies))),
                            )
                        )
                        if terminal_proofs is not None:
                            terminal_proofs.append(_StandableQueryProof(
                                StandableConnectionQueryArgs(
                                    candidate.path[-1].surface,
                                    terminal_target.position,
                                    direct_from,
                                    .6,
                                    1.8,
                                ),
                                direct.dependencies,
                            ))
                    else:
                        pending_dependencies.update(
                            terminal_target.dependencies
                        )
                        pending_points.append(terminal)
            else:
                terminal_matches_last = True
        flush_walk()
        if terminal_target is not None and actions:
            final_action = actions[-1]
            if (terminal_matches_last and exact_terminal_execution is None
                    and type(final_action) is WalkSegment
                    and final_action.traversal_plan is None):
                if (len(final_action.fixed_route.points) == 2
                        and len(final_action.node_ids) == 1
                        and skip_first_walk_start
                        and forward_entry is not None):
                    first, last = final_action.fixed_route.points
                    args = forward_entry.proof.args
                    initial_leg_maps = (
                        args.connection_from == (first.x, first.y, first.z)
                        and args.position == (last.x, last.y, last.z)
                        and args.position == terminal_target.position
                        and abs(forward_entry.length_blocks
                                - connection_length) <= 1.0e-9
                    )
                    if initial_leg_maps:
                        exact_terminal_execution = (
                            _ExactTerminalExecutionDependencies(
                                len(actions) - 1,
                                (),
                                forward_entry.proof.dependencies,
                                True,
                            )
                        )
                if (exact_terminal_execution is None
                        and len(final_action.fixed_route.points) >= 2
                        and len(final_action.node_ids) >= 2):
                    points = final_action.fixed_route.points
                    start_id, end_id = final_action.node_ids[-2:]
                    nodes = {node.node_id: node for node in candidate.path}
                    final_edge = next((
                        edge for edge in candidate.segments
                        if type(edge) is SurfaceWalkEdge
                        and edge.start == start_id
                        and edge.end == end_id
                    ), None)
                    before = points[-2]
                    last = points[-1]
                    final_leg_maps = (
                        final_edge is not None
                        and not final_edge.requires_ground_traversal_proof
                        and final_edge.transition.mode is MovementMode.WALK
                        and start_id in nodes
                        and end_id in nodes
                        and (before.x, before.y, before.z)
                            == nodes[start_id].position
                        and (last.x, last.y, last.z)
                            == nodes[end_id].position
                        and (last.x, last.y, last.z)
                            == terminal_target.position
                    )
                    if final_leg_maps:
                        direct = query_standable_connection(
                            frame.world,
                            candidate.path[-1].surface,
                            terminal_target.position,
                            (before.x, before.y, before.z),
                        )
                        if direct.status is QueryStatus.FEASIBLE:
                            exact_terminal_execution = (
                                _ExactTerminalExecutionDependencies(
                                    len(actions) - 1,
                                    tuple(sorted(set(final_action.dependencies))),
                                    tuple(sorted(set(direct.dependencies))),
                                )
                            )
                            if terminal_proofs is not None:
                                terminal_proofs.append(_StandableQueryProof(
                                    StandableConnectionQueryArgs(
                                        candidate.path[-1].surface,
                                        terminal_target.position,
                                        (before.x, before.y, before.z),
                                        .6,
                                        1.8,
                                    ),
                                    direct.dependencies,
                                ))
                if (exact_terminal_execution is None
                        and len(final_action.fixed_route.points) >= 2):
                    before = final_action.fixed_route.points[-2]
                    direct_from = (before.x, before.y, before.z)
                    direct = query_standable_connection(
                        frame.world,
                        candidate.path[-1].surface,
                        terminal_target.position,
                        direct_from,
                    )
                    if direct.status is QueryStatus.FEASIBLE:
                        exact_terminal_execution = (
                            _ExactTerminalExecutionDependencies(
                                len(actions) - 1,
                                tuple(sorted(set(final_action.dependencies))),
                                tuple(sorted(set(direct.dependencies))),
                            )
                        )
                        if terminal_proofs is not None:
                            terminal_proofs.append(_StandableQueryProof(
                                StandableConnectionQueryArgs(
                                    candidate.path[-1].surface,
                                    terminal_target.position,
                                    direct_from,
                                    .6,
                                    1.8,
                                ),
                                direct.dependencies,
                            ))
            completion = terminal_target.completion_region
            if exact_terminal_execution is not None:
                exact_terminal_execution = replace(exact_terminal_execution,
                    completion_dependencies=completion.dependencies)
            if exact_terminal_execution is None:
                actions[-1] = replace(actions[-1], dependencies=tuple(sorted(
                    set(actions[-1].dependencies)
                    | set(terminal_target.dependencies)
                    | set(completion.dependencies))))
            else:
                if exact_terminal_execution.action_index != len(actions) - 1:
                    raise ContractViolation(
                        "exact terminal proof does not belong to final action"
                    )
                actions[-1] = replace(
                    actions[-1],
                    dependencies=(
                        exact_terminal_execution.execution_dependencies
                    ),
                )
                if terminal_execution_dependencies is not None:
                    terminal_execution_dependencies.append(
                        exact_terminal_execution
                    )
            if (type(actions[-1]) is WalkSegment
                    and actions[-1].traversal_plan is None
                    and candidate.ground_profile is not None):
                final = actions[-1]
                contract = GroundRouteExecutionContract.for_completion(completion, final.dependencies,
                    candidate.ground_profile.profile_id)
                actions[-1] = replace(final, fixed_route=replace(final.fixed_route,
                    execution_contract=contract))
        return (ActionRoute(
            route_id, tuple(actions), candidate.goal_state,
            candidate.final_resources if candidate.final_resources is not None
            else ResourceState(),
        ) if actions else None)

    @staticmethod
    def _forward_ground_entry(
        candidate: SurfaceRouteCandidate,
        frame: NavigationFrame,
    ) -> _ForwardGroundEntry | None:
        if len(candidate.path) < 2 or not candidate.segments:
            return None
        edge = candidate.segments[0]
        if (type(edge) is not SurfaceWalkEdge
                or edge.requires_ground_traversal_proof
                or edge.transition.mode is not MovementMode.WALK
                or not frame.body.is_on_ground
                or observed_ground_mode(frame.body) is not MovementMode.WALK):
            return None
        first, following = candidate.path[:2]
        if abs(first.position[1] - following.position[1]) > 1.0e-9:
            return None
        first_ids = (first.node_id, following.node_id)
        if any(
            canonical_surface_node_path(plan.surface_node_path)[:2] == first_ids
            for plan in candidate.ground_traversal_plans
        ):
            return None
        dx = following.position[0] - first.position[0]
        dz = following.position[2] - first.position[2]
        edge_length = math.hypot(dx, dz)
        if edge_length <= 1.0e-9:
            return None
        along = (
            (frame.body.position[0] - first.position[0]) * dx
            + (frame.body.position[2] - first.position[2]) * dz
        ) / edge_length
        if not 1.0e-9 < along < edge_length - 1.0e-9:
            return None
        direct = query_standable_connection(
            frame.world, following.surface, following.position,
            frame.body.position,
        )
        if direct.status is not QueryStatus.FEASIBLE:
            return None
        return _ForwardGroundEntry(
            math.dist(frame.body.position, following.position),
            _StandableQueryProof(
                StandableConnectionQueryArgs(
                    following.surface,
                    following.position,
                    frame.body.position,
                    .6,
                    1.8,
                ),
                direct.dependencies,
            ),
        )

    @staticmethod
    def _surface_validation_plan(
        candidate: SurfaceRouteCandidate,
        action_route: ActionRoute,
        *,
        connection_length: float,
        connection_dependencies: tuple[BlockPos, ...],
        forward_entry: _ForwardGroundEntry | None,
        terminal_proof: _StandableQueryProof | None,
        terminal_execution: _ExactTerminalExecutionDependencies | None,
        completion_proof: StandableRegionQueryArgs | None = None,
    ) -> ActiveRouteValidationPlan:
        """Describe proofs for the final route without authorizing execution."""
        recipes: list[WalkValidationRecipe] = []
        owners: list[DependencyOwner] = []
        action_plans: list[WalkActionValidationPlan] = []
        owner_dependencies: dict[str, set[BlockPos]] = {}
        recipe_sequence = 0
        owner_sequence = 0
        profile = candidate.ground_profile
        capability = (
            None if profile is None
            else GroundCapabilityIdentity.from_profile(profile)
        )

        def next_recipe_id() -> str:
            nonlocal recipe_sequence
            recipe_sequence += 1
            return (
                f"{action_route.route_id}/validation/recipe/"
                f"{recipe_sequence:03d}"
            )

        def next_owner_id() -> str:
            nonlocal owner_sequence
            owner_sequence += 1
            return (
                f"{action_route.route_id}/validation/owner/"
                f"{owner_sequence:03d}"
            )

        def add_recipe(
            *,
            query_kind: WalkValidationQueryKind,
            surface_edge: SurfaceEdgeQueryArgs | None = None,
            standable: StandableConnectionQueryArgs | None = None,
            dependencies: tuple[BlockPos, ...],
            region: StandableRegionQueryArgs | None = None,
        ) -> WalkValidationRecipe:
            assert profile is not None and capability is not None
            recipe = WalkValidationRecipe(
                next_recipe_id(),
                query_kind,
                surface_edge,
                standable,
                profile,
                capability,
                tuple(sorted(set(dependencies))),
                region,
            )
            recipes.append(recipe)
            return recipe

        def add_owner(
            kind: DependencyOwnerKind,
            action_index: int,
            dependencies: tuple[BlockPos, ...] | set[BlockPos],
            *,
            fixed_route_id: str | None = None,
            recipe: WalkValidationRecipe | None = None,
        ) -> DependencyOwner | None:
            dependency_set = set(dependencies)
            if not dependency_set:
                return None
            owner = DependencyOwner(
                next_owner_id(),
                kind,
                action_index,
                fixed_route_id,
                None if recipe is None else recipe.recipe_id,
            )
            owners.append(owner)
            owner_dependencies[owner.owner_id] = dependency_set
            return owner

        initial_connection = None
        covered_by_action: dict[int, set[BlockPos]] = {
            index: set() for index in range(len(action_route.actions))
        }
        preterminal_covered_by_action: dict[int, set[BlockPos]] = {
            index: set() for index in range(len(action_route.actions))
        }
        if terminal_execution is not None:
            uses_initial = terminal_execution.uses_initial_connection
            if ((terminal_proof is None and not uses_initial)
                    or terminal_execution.action_index
                        >= len(action_route.actions)
                    or set(action_route.actions[
                        terminal_execution.action_index
                    ].dependencies) != set(
                        terminal_execution.execution_dependencies
                    )
                    or (uses_initial and (
                        forward_entry is None
                        or terminal_execution.action_index != 0
                        or set(terminal_execution.exact_dependencies)
                            != set(forward_entry.proof.dependencies)
                    ))):
                raise ContractViolation(
                    "exact terminal dependency sources differ from final route"
                )
        if connection_length > 1.0e-9 and connection_dependencies:
            initial_recipe = None
            initial_dependencies = connection_dependencies
            first_action = action_route.actions[0]
            forward_maps_to_final_route = False
            if (forward_entry is not None
                    and type(first_action) is WalkSegment
                    and len(first_action.fixed_route.points) >= 2):
                first, second = first_action.fixed_route.points[:2]
                args = forward_entry.proof.args
                forward_maps_to_final_route = (
                    args.connection_from == (first.x, first.y, first.z)
                    and args.position == (second.x, second.y, second.z)
                )
            if (forward_entry is not None and profile is not None
                    and forward_maps_to_final_route):
                context = _DirectWalkProofContext(
                    candidate.world_session,
                    action_route.route_id,
                    0,
                    first_action.fixed_route.route_id,
                    forward_entry.proof.args.connection_from,
                    forward_entry.proof.args.position,
                    profile,
                    capability,
                    forward_entry.proof,
                )
                initial_recipe = RouteAdmitter._direct_walk_recipe(
                    context, next_recipe_id(),
                )
                recipes.append(initial_recipe)
                initial_dependencies = forward_entry.proof.dependencies
            initial_owner = add_owner(
                DependencyOwnerKind.INITIAL_CONNECTION,
                0,
                initial_dependencies,
                fixed_route_id=(
                    first_action.fixed_route.route_id
                    if type(first_action) is WalkSegment else None
                ),
                recipe=initial_recipe,
            )
            if initial_owner is not None:
                initial_connection = InitialConnectionValidation(
                    initial_owner.owner_id,
                    connection_length,
                )
                covered_by_action[0].update(initial_dependencies)
                preterminal_covered_by_action[0].update(
                    initial_dependencies
                )

        nodes = {node.node_id: node for node in candidate.path}
        edges = {
            (edge.start, edge.end): edge
            for edge in candidate.segments
            if type(edge) is SurfaceWalkEdge
        }

        def point_value(point: RoutePoint) -> tuple[float, float, float]:
            return point.x, point.y, point.z

        for action_index, action in enumerate(action_route.actions):
            completion_dependencies: set[BlockPos] = set()
            if (completion_proof is not None
                    and terminal_execution is not None
                    and action_index == len(action_route.actions) - 1
                    and completion_proof.expected_region.dependencies):
                completion_dependencies.update(
                    completion_proof.expected_region.dependencies
                )
                region_recipe = add_recipe(
                    query_kind=WalkValidationQueryKind.STANDABLE_REGION,
                    region=completion_proof,
                    dependencies=tuple(sorted(completion_dependencies)),
                )
                fixed_route = getattr(action, "fixed_route", None)
                completion_fixed_route = (
                    fixed_route.route_id
                    if (type(fixed_route) is FixedRoute
                        and fixed_route.execution_contract is not None)
                    else None
                )
                add_owner(
                    DependencyOwnerKind.COMPLETION_REGION,
                    action_index,
                    completion_dependencies,
                    fixed_route_id=completion_fixed_route,
                    recipe=region_recipe,
                )
                covered_by_action[action_index].update(
                    completion_dependencies
                )
            if type(action) is not WalkSegment:
                add_owner(
                    DependencyOwnerKind.STRICT_ACTION,
                    action_index,
                    action.dependencies,
                )
                covered_by_action[action_index].update(action.dependencies)
                continue
            if action.traversal_plan is not None:
                add_owner(
                    DependencyOwnerKind.STRICT_ACTION,
                    action_index,
                    action.dependencies,
                    fixed_route_id=action.fixed_route.route_id,
                )
                covered_by_action[action_index].update(action.dependencies)
                continue

            points = action.fixed_route.points
            progress = [0.0]
            for first, second in zip(points, points[1:]):
                progress.append(progress[-1] + math.dist(
                    point_value(first), point_value(second),
                ))
            point_offset = (
                1 if action_index == 0 and connection_length > 1.0e-9
                else 0
            )
            legs: list[WalkLegValidationBinding] = []
            terminal_proof_mapped = False
            for node_index, (start_id, end_id) in enumerate(zip(
                    action.node_ids, action.node_ids[1:])):
                edge = edges.get((start_id, end_id))
                start_point_index = node_index + point_offset
                end_point_index = start_point_index + 1
                if (edge is None or end_point_index >= len(points)
                        or edge.requires_ground_traversal_proof
                        or edge.transition.mode is not MovementMode.WALK
                        or profile is None):
                    continue
                start_point = point_value(points[start_point_index])
                end_point = point_value(points[end_point_index])
                recipe = None
                maps_terminal_proof = False
                if (terminal_proof is not None
                        and end_point_index == len(points) - 1
                        and start_point == terminal_proof.args.connection_from
                        and end_point == terminal_proof.args.position):
                    recipe = add_recipe(
                        query_kind=WalkValidationQueryKind.STANDABLE_CONNECTION,
                        standable=terminal_proof.args,
                        dependencies=terminal_proof.dependencies,
                    )
                    maps_terminal_proof = True
                    terminal_proof_mapped = True
                elif (start_point == nodes[start_id].position
                        and end_point == nodes[end_id].position
                        and abs(start_point[1] - end_point[1]) <= 1.0e-9):
                    recipe = add_recipe(
                        query_kind=WalkValidationQueryKind.SURFACE_EDGE,
                        surface_edge=SurfaceEdgeQueryArgs(
                            nodes[start_id].surface,
                            nodes[end_id].surface,
                            edge.body_height_blocks,
                        ),
                        dependencies=edge.dependencies,
                    )
                if recipe is None:
                    continue
                owner = add_owner(
                    DependencyOwnerKind.WALK_LEG,
                    action_index,
                    recipe.dependencies,
                    fixed_route_id=action.fixed_route.route_id,
                    recipe=recipe,
                )
                assert owner is not None
                covered_by_action[action_index].update(recipe.dependencies)
                if not maps_terminal_proof:
                    preterminal_covered_by_action[action_index].update(
                        recipe.dependencies
                    )
                legs.append(WalkLegValidationBinding(
                    owner.owner_id,
                    recipe.recipe_id,
                    start_point_index,
                    end_point_index,
                    progress[start_point_index],
                    progress[end_point_index],
                ))
            if (terminal_execution is not None
                    and terminal_execution.action_index == action_index
                    and not terminal_execution.uses_initial_connection
                    and not terminal_proof_mapped):
                if terminal_proof is None or len(points) < 2 or profile is None:
                    raise ContractViolation(
                        "exact terminal execution lacks a replayable proof"
                    )
                start_point_index = len(points) - 2
                end_point_index = len(points) - 1
                start_point = point_value(points[start_point_index])
                end_point = point_value(points[end_point_index])
                if (start_point != terminal_proof.args.connection_from
                        or end_point != terminal_proof.args.position):
                    raise ContractViolation(
                        "terminal proof differs from the final fixed-route leg"
                    )
                recipe = add_recipe(
                    query_kind=WalkValidationQueryKind.STANDABLE_CONNECTION,
                    standable=terminal_proof.args,
                    dependencies=terminal_proof.dependencies,
                )
                owner = add_owner(
                    DependencyOwnerKind.WALK_LEG,
                    action_index,
                    recipe.dependencies,
                    fixed_route_id=action.fixed_route.route_id,
                    recipe=recipe,
                )
                assert owner is not None
                covered_by_action[action_index].update(recipe.dependencies)
                legs.append(WalkLegValidationBinding(
                    owner.owner_id,
                    recipe.recipe_id,
                    start_point_index,
                    end_point_index,
                    progress[start_point_index],
                    progress[end_point_index],
                ))
                terminal_proof_mapped = True
            if legs:
                action_plans.append(WalkActionValidationPlan(
                    action_index,
                    action.fixed_route.route_id,
                    tuple(legs),
                ))
            if (terminal_execution is not None
                    and terminal_execution.action_index == action_index
                    and (terminal_proof_mapped
                         or terminal_execution.uses_initial_connection)):
                remaining = (
                    set(terminal_execution.preterminal_dependencies)
                    - preterminal_covered_by_action[action_index]
                )
            else:
                remaining = (
                    set(action.dependencies) - covered_by_action[action_index]
                )
            add_owner(
                DependencyOwnerKind.NON_RECIPE,
                action_index,
                remaining,
                fixed_route_id=action.fixed_route.route_id,
            )

        provenance: dict[BlockPos, set[str]] = {}
        for owner in owners:
            for position in owner_dependencies[owner.owner_id]:
                provenance.setdefault(position, set()).add(owner.owner_id)
        return ActiveRouteValidationPlan(
            tuple(sorted(action_plans, key=lambda item: item.action_index)),
            tuple(sorted(recipes, key=lambda item: item.recipe_id)),
            tuple(sorted(owners, key=lambda item: item.owner_id)),
            initial_connection,
            tuple(
                DependencyProvenance(position, tuple(sorted(owner_refs)))
                for position, owner_refs in sorted(provenance.items())
            ),
        )

    def admit_surface(
        self,
        candidate: SurfaceRouteCandidate,
        frame: NavigationFrame,
        *,
        expected_request_id: str,
        goal_id: str,
        goal_revision: int,
        changed_cells: tuple[BlockPos, ...],
        edge_probe: LandingEdgeProbe | None = None,
    ) -> AdmissionResult:
        """Recheck a B07 surface route before it enters the control thread."""
        if type(candidate) is not SurfaceRouteCandidate or type(frame) is not NavigationFrame:
            raise ContractViolation("surface route admission requires candidate and frame")
        if type(changed_cells) is not tuple:
            raise ContractViolation("route changes must be immutable")
        if candidate.status is not SurfacePlanningStatus.COMPLETE or not candidate.path:
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.CANDIDATE_NOT_COMPLETE)
        if candidate.request_id != expected_request_id:
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.PLANNING_REQUEST_REPLACED)
        if candidate.world_session != frame.session.value:
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.WORLD_SESSION_CHANGED)
        if candidate.goal_id != goal_id or candidate.goal_revision != goal_revision:
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.GOAL_REVISION_CHANGED)
        if frame.world.geometry_revision != candidate.geometry_revision and not changed_cells:
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.WORLD_DELTA_MISSING)
        if set(candidate.dependencies).intersection(changed_cells):
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.ROUTE_DEPENDENCIES_CHANGED)
        resources = self._replay_resources(candidate, candidate.initial_resources,
                                           candidate.minimum_resources, frame)
        if type(resources) is AdmissionReason:
            return AdmissionResult(AdmissionStatus.REJECTED, resources)
        candidate = replace(candidate, final_resources=resources)
        first_edge = candidate.segments[0] if candidate.segments else None
        body_mode = observed_ground_mode(frame.body)
        starts_at_first_action = (
            type(first_edge) is SurfaceControlledDropEdge
            and first_edge.entry_window is not None
            and body_mode is not None
            and body_fits_segment_entry(
                first_edge.entry_window, frame.body, body_mode,
            )
        )
        skip_first_walk_start = False
        if starts_at_first_action:
            connected, connection_length, connection_dependencies = True, 0.0, ()
        else:
            connected, connection_length, connection_dependencies = self._connection(
                candidate, frame,
            )
        if not connected:
            return AdmissionResult(AdmissionStatus.REJECTED,
                                   AdmissionReason.CURRENT_BODY_CANNOT_CONNECT)
        forward_entry = self._forward_ground_entry(candidate, frame)
        if forward_entry is not None:
            connection_length = forward_entry.length_blocks
            direct_dependencies = forward_entry.proof.dependencies
            connection_dependencies = tuple(sorted(
                set(connection_dependencies) | set(direct_dependencies)
            ))
            skip_first_walk_start = True
        if (any(
                type(edge) is SurfaceWalkEdge
                and edge.requires_ground_traversal_proof
                for edge in candidate.segments)
                and not candidate.ground_traversal_plans):
            return AdmissionResult(
                AdmissionStatus.REJECTED,
                AdmissionReason.GROUND_TRAVERSAL_PROOF_MISSING,
            )
        route_id = self._surface_route_id(candidate)
        terminal_target = None
        if candidate.goal_state is not None:
            incoming = (candidate.path[-2].position if len(candidate.path) >= 2
                        else frame.body.position)
            terminal_target = self._goal_completion(frame.world, candidate.path[-1].surface,
                candidate.goal_state, candidate.ground_profile, incoming)
            if terminal_target.status is not QueryStatus.FEASIBLE:
                return AdmissionResult(AdmissionStatus.REJECTED,
                    AdmissionReason.GOAL_STANDING_POINT_NEEDS_INFORMATION if terminal_target.missing_cells
                    else AdmissionReason.GOAL_STANDING_POINT_UNAVAILABLE,
                    missing_cells=terminal_target.missing_cells)
        terminal_proofs: list[_StandableQueryProof] = []
        terminal_execution_dependencies: list[
            _ExactTerminalExecutionDependencies
        ] = []
        action_route = self._surface_action_route(
            candidate, frame, connection_length, connection_dependencies, route_id, terminal_target,
            skip_first_walk_start=skip_first_walk_start,
            forward_entry=forward_entry,
            terminal_proofs=terminal_proofs,
            terminal_execution_dependencies=terminal_execution_dependencies,
        )
        if action_route is None:
            return AdmissionResult(AdmissionStatus.REJECTED, AdmissionReason.CANDIDATE_HAS_NO_ACTIONS)
        if (forward_entry is not None and terminal_execution_dependencies
                and terminal_execution_dependencies[0].action_index == 0
                and len(action_route.actions) == 1
                and len(action_route.actions[0].fixed_route.points) == 2
                and terminal_proofs):
            first, last = action_route.actions[0].fixed_route.points
            proof = terminal_proofs[0]
            if (proof.args.connection_from == (first.x, first.y, first.z)
                    and proof.args.position == (last.x, last.y, last.z)):
                connection_length = math.dist(proof.args.connection_from, proof.args.position)
                forward_entry = _ForwardGroundEntry(connection_length, proof)
                connection_dependencies = proof.dependencies
                exact = replace(terminal_execution_dependencies[0],
                    preterminal_dependencies=(),
                    uses_initial_connection=True)
                terminal_execution_dependencies[0] = exact
                final = action_route.actions[0]
                contract = replace(final.fixed_route.execution_contract,
                                   dependencies=exact.execution_dependencies)
                final = replace(final, dependencies=exact.execution_dependencies,
                    fixed_route=replace(final.fixed_route, execution_contract=contract))
                action_route = replace(action_route, actions=(final,))
        if any(
            type(action) is WalkSegment
            and len({point.y for point in action.fixed_route.points}) > 1
            and action.traversal_plan is None
            for action in action_route.actions
        ):
            return AdmissionResult(
                AdmissionStatus.REJECTED,
                AdmissionReason.GROUND_TRAVERSAL_PROOF_MISSING,
            )

        corridor_start = 1 if skip_first_walk_start else 0
        length = 0.0
        corridor_nodes = [candidate.path[corridor_start]]
        corridor_segments = []
        for edge, node in zip(
                candidate.segments[corridor_start:],
                candidate.path[corridor_start + 1:]):
            segment_length = math.dist(corridor_nodes[-1].position, node.position)
            if (corridor_segments and connection_length + length + segment_length
                    > self.maximum_corridor_blocks):
                break
            corridor_segments.append(edge)
            corridor_nodes.append(node)
            length += segment_length
        terminal_execution = (
            terminal_execution_dependencies[0]
            if terminal_execution_dependencies else None
        )
        if len(terminal_execution_dependencies) > 1:
            raise ContractViolation(
                "surface route produced multiple exact terminal proofs"
            )
        if (terminal_execution is not None
                and terminal_execution.uses_initial_connection):
            if (forward_entry is None
                    or set(terminal_execution.exact_dependencies)
                        != set(forward_entry.proof.dependencies)):
                raise ContractViolation(
                    "terminal initial proof differs from admitted connection"
                )
            connection_dependencies = (
                terminal_execution.exact_dependencies
            )
        dependencies = tuple(sorted(
            {cell for node in corridor_nodes for cell in node.dependencies}
            | {cell for edge in corridor_segments for cell in edge.dependencies}
            | set(connection_dependencies)
            | ((set(terminal_execution.exact_dependencies)
                | set(terminal_target.completion_region.dependencies)
                if terminal_execution is not None
                else set(terminal_target.completion_region.dependencies))
               if terminal_target is not None
               and corridor_nodes[-1].node_id == candidate.path[-1].node_id
               else set())
        ))
        full_length = connection_length + sum(
            math.dist(first.position, second.position)
            for first, second in zip(
                candidate.path[corridor_start:],
                candidate.path[corridor_start + 1:],
            )
        )
        if terminal_target is not None:
            # Execution length is measured from executable points, rather than
            # adding an obsolete graph-centre detour to the search estimate.
            full_length = _action_route_length(action_route)
        corridor = ExecutableCorridor(
            tuple(node.node_id for node in corridor_nodes), dependencies,
            (full_length if corridor_nodes[-1].node_id == candidate.path[-1].node_id
             else connection_length + length), corridor_nodes[-1].node_id,
        )
        validation_plan = self._surface_validation_plan(
            candidate,
            action_route,
            connection_length=connection_length,
            connection_dependencies=connection_dependencies,
            forward_entry=forward_entry,
            terminal_proof=(terminal_proofs[0] if terminal_proofs else None),
            terminal_execution=terminal_execution,
            completion_proof=(StandableRegionQueryArgs(candidate.path[-1].surface,
                candidate.goal_state.region, incoming, terminal_target.completion_region)
                if terminal_target is not None else None),
        )
        active = ActiveRoute(
            route_id, 1, candidate.request_id, candidate.goal_id,
            candidate.goal_revision, candidate.world_session, None,
            full_length, connection_length, connection_dependencies, corridor,
            action_route, candidate.goal_state, candidate.request_sequence,
            candidate.work_identity, validation_plan,
        )
        return AdmissionResult(AdmissionStatus.ACCEPTED,
                               AdmissionReason.CANDIDATE_ADMITTED, active)


class ActiveRouteTracker:
    """Own runtime proof progress and legacy corridor facts for one route."""

    def __init__(self, route: ActiveRoute,
                 candidate: RouteCandidate | SurfaceRouteCandidate | None = None,
                 *, maximum_corridor_blocks: float = 8.0) -> None:
        if (type(route) is not ActiveRoute
                or (candidate is not None and type(candidate) not in (
                    RouteCandidate, SurfaceRouteCandidate,
                ))):
            raise ContractViolation("route tracker requires an active route")
        if candidate is not None and route.source_request_id!=candidate.request_id:
            raise ContractViolation("active route and candidate identities differ")
        if (type(maximum_corridor_blocks) not in (int,float)
                or not math.isfinite(float(maximum_corridor_blocks))
                or maximum_corridor_blocks<=0):
            raise ContractViolation("corridor length must be positive and finite")
        self.route=route;self.candidate=candidate
        self.maximum_corridor_blocks=float(maximum_corridor_blocks)
        self._route_start_index = None
        if candidate is not None:
            first_corridor_node = route.corridor.node_ids[0]
            self._route_start_index = next(
                index for index, node in enumerate(candidate.path)
                if node.node_id == first_corridor_node
            )
        self._invalidated:set[BlockPos]=set()
        self._plan = route.validation_plan
        self._current_action_index = 0
        self._last_progress: dict[int, float] = {}
        self._progress_failure: ActiveRouteValidationReason | None = None
        if self._plan is None:
            self._active_owner_dependencies: dict[str, set[BlockPos]] = {}
            self._fallback_dependencies = set(route.connection_dependencies) | set(
                route.action_route.dependencies
            )
        else:
            self._active_owner_dependencies = {
                owner.owner_id: set(
                    self._plan.dependencies_for_owner(owner.owner_id)
                )
                for owner in self._plan.owners
            }
            self._fallback_dependencies = set()

    @property
    def identity(self) -> ActiveRouteValidationIdentity:
        return ActiveRouteValidationIdentity(
            self.route.world_session,
            self.route.route_id,
            self.route.route_revision,
            self.route.source_request_id,
            self.route.goal_id,
            self.route.goal_revision,
            self.route.planning_generation,
            self.route.work_identity,
            self._current_action_index,
        )

    @property
    def effective_dependencies(self) -> tuple[BlockPos, ...]:
        if self._plan is None:
            return tuple(sorted(self._fallback_dependencies))
        return tuple(sorted({
            position
            for positions in self._active_owner_dependencies.values()
            for position in positions
        }))

    def unaffected(self) -> ActiveRouteValidation:
        return self._validation(
            ActiveRouteValidationDisposition.UNAFFECTED,
            ActiveRouteValidationReason.NO_INTERSECTION,
        )

    def _validation(
        self,
        disposition: ActiveRouteValidationDisposition,
        reason: ActiveRouteValidationReason,
        *,
        affected: tuple[BlockPos, ...] = (),
        refreshed: tuple[BlockPos, ...] = (),
        missing: tuple[BlockPos, ...] = (),
        queries_used: int = 0,
    ) -> ActiveRouteValidation:
        return ActiveRouteValidation(
            disposition, reason, self.identity,
            tuple(sorted(set(affected))),
            tuple(sorted(set(refreshed))),
            tuple(sorted(set(missing))),
            queries_used,
        )

    def _fail_progress(
        self, reason: ActiveRouteValidationReason,
    ) -> ActiveRouteValidation:
        self._progress_failure = reason
        return self._validation(ActiveRouteValidationDisposition.STOP, reason)

    def record_progress(
        self,
        action_index: int,
        evidence: RouteProgressEvidence | None,
        *,
        observation_sequence_id: int,
    ) -> ActiveRouteValidation:
        """Retire proof owners only from this frame's typed controller fact."""
        if (type(action_index) is not int or action_index < 0
                or type(observation_sequence_id) is not int
                or observation_sequence_id < 0):
            raise ContractViolation("route progress update requires typed indices")
        if self._progress_failure is not None:
            return self._validation(
                ActiveRouteValidationDisposition.STOP,
                self._progress_failure,
            )
        action_count = len(self.route.action_route.actions)
        if (action_index >= action_count
                or action_index > self._current_action_index + 1):
            return self._fail_progress(
                ActiveRouteValidationReason.PROGRESS_ACTION_INDEX_INVALID,
            )
        if action_index < self._current_action_index:
            return self._fail_progress(
                ActiveRouteValidationReason.PROGRESS_REWOUND,
            )
        if self._plan is None:
            self._current_action_index = action_index
            return self._validation(
                ActiveRouteValidationDisposition.CONTINUE,
                ActiveRouteValidationReason.PROGRESS_RECORDED,
            )
        action_plans = {
            plan.action_index: plan for plan in self._plan.action_plans
        }
        fixed_routes_by_action = {
            index: {
                owner.fixed_route_id
                for owner in self._plan.owners
                if owner.action_index == index
                and owner.fixed_route_id is not None
            }
            for index in range(len(self.route.action_route.actions))
        }
        leaving_current_action = action_index > self._current_action_index
        current_fixed_routes = fixed_routes_by_action.get(
            self._current_action_index, set()
        )
        if evidence is None:
            if current_fixed_routes:
                return self._fail_progress(
                    ActiveRouteValidationReason.PROGRESS_EVIDENCE_MISSING,
                )
        else:
            if type(evidence) is not RouteProgressEvidence:
                raise ContractViolation("route progress evidence must be typed")
            if evidence.observation_sequence_id != observation_sequence_id:
                return self._fail_progress(
                    ActiveRouteValidationReason.PROGRESS_EVIDENCE_STALE,
                )
            expected_routes = fixed_routes_by_action.get(
                evidence.action_index, set()
            )
            if (expected_routes != {evidence.fixed_route_id}
                    or evidence.action_index not in {
                        self._current_action_index, action_index,
                    }
                    or (leaving_current_action and current_fixed_routes
                        and evidence.action_index
                            != self._current_action_index)):
                return self._fail_progress(
                    ActiveRouteValidationReason.PROGRESS_IDENTITY_MISMATCH,
                )
            prior = self._last_progress.get(evidence.action_index, 0.0)
            if evidence.progress_blocks + 1.0e-9 < prior:
                return self._fail_progress(
                    ActiveRouteValidationReason.PROGRESS_REWOUND,
                )
            self._last_progress[evidence.action_index] = float(
                evidence.progress_blocks
            )

        for owner in self._plan.owners:
            if owner.action_index < action_index:
                self._active_owner_dependencies.pop(owner.owner_id, None)
        if evidence is not None:
            action_plan = action_plans.get(evidence.action_index)
            if action_plan is not None:
                for leg in action_plan.legs:
                    if (evidence.progress_blocks + 1.0e-9
                            >= leg.end_progress_blocks):
                        self._active_owner_dependencies.pop(leg.owner_ref, None)
            initial = self._plan.initial_connection
            if (evidence.action_index == 0 and initial is not None
                    and evidence.progress_blocks + 1.0e-9
                        >= initial.retire_after_progress_blocks):
                self._active_owner_dependencies.pop(initial.owner_ref, None)
        self._current_action_index = action_index
        return self._validation(
            ActiveRouteValidationDisposition.CONTINUE,
            ActiveRouteValidationReason.PROGRESS_RECORDED,
        )

    def validate(
        self,
        world: WorldView,
        changed_cells: tuple[BlockPos, ...],
        *,
        ground_profile: GroundMotionProfile | None = None,
        action_index: int | None = None,
        expected_identity: ActiveRouteValidationIdentity | None = None,
        query_cache: WorldQueryCache | None = None,
        budget: RouteValidationBudget | None = None,
    ) -> ActiveRouteValidation:
        """Replay every proof owner for an affected cell, atomically."""
        if type(world) is not WorldView or type(changed_cells) is not tuple:
            raise ContractViolation("route validation requires world and changes")
        if (action_index is not None
                and (type(action_index) is not int or action_index < 0)):
            raise ContractViolation("route validation action index must be typed")
        if (expected_identity is not None
                and type(expected_identity)
                    is not ActiveRouteValidationIdentity):
            raise ContractViolation(
                "route validation expected identity must be typed"
            )
        if (expected_identity is not None
                and expected_identity != self.identity):
            return self._validation(
                ActiveRouteValidationDisposition.STOP,
                ActiveRouteValidationReason.ROUTE_IDENTITY_CHANGED,
            )
        if world.session.value != self.route.world_session:
            return self._validation(
                ActiveRouteValidationDisposition.STOP,
                ActiveRouteValidationReason.ROUTE_IDENTITY_CHANGED,
            )
        if self._progress_failure is not None:
            return self._validation(
                ActiveRouteValidationDisposition.STOP,
                self._progress_failure,
            )
        if (not 0 <= self._current_action_index
                    < len(self.route.action_route.actions)
                or (action_index is not None
                    and action_index != self._current_action_index)):
            return self._validation(
                ActiveRouteValidationDisposition.STOP,
                ActiveRouteValidationReason.PROGRESS_IDENTITY_MISMATCH,
            )
        if not changed_cells:
            return self._validation(
                ActiveRouteValidationDisposition.UNAFFECTED,
                ActiveRouteValidationReason.NO_INTERSECTION,
            )
        effective_dependencies = self.effective_dependencies
        affected = tuple(sorted(
            set(changed_cells).intersection(effective_dependencies)
        ))
        if not affected:
            return self._validation(
                ActiveRouteValidationDisposition.UNAFFECTED,
                ActiveRouteValidationReason.NO_INTERSECTION,
            )
        if self._plan is None:
            return self._validation(
                ActiveRouteValidationDisposition.STOP,
                ActiveRouteValidationReason.PLAN_UNAVAILABLE,
                affected=affected,
            )
        owner_refs = tuple(sorted({
            owner_ref
            for owner_ref, dependencies in self._active_owner_dependencies.items()
            if set(affected).intersection(dependencies)
        }))
        owners = {owner.owner_id: owner for owner in self._plan.owners}
        if not owner_refs or any(owner_ref not in owners for owner_ref in owner_refs):
            return self._validation(
                ActiveRouteValidationDisposition.STOP,
                ActiveRouteValidationReason.OWNER_REFERENCE_INVALID,
                affected=affected,
            )
        for owner_ref in owner_refs:
            owner = owners[owner_ref]
            if owner.kind is DependencyOwnerKind.STRICT_ACTION:
                return self._validation(
                    ActiveRouteValidationDisposition.STOP,
                    ActiveRouteValidationReason.STRICT_OWNER_CHANGED,
                    affected=affected,
                )
            if owner.kind is DependencyOwnerKind.NON_RECIPE:
                return self._validation(
                    ActiveRouteValidationDisposition.STOP,
                    ActiveRouteValidationReason.NON_RECIPE_OWNER_CHANGED,
                    affected=affected,
                )
            if owner.recipe_ref is None:
                return self._validation(
                    ActiveRouteValidationDisposition.STOP,
                    ActiveRouteValidationReason.OWNER_REFERENCE_INVALID,
                    affected=affected,
                )
        recipe_refs = tuple(sorted({owners[ref].recipe_ref for ref in owner_refs}))
        region_queries = sum(self._plan.recipe(ref).query_kind is WalkValidationQueryKind.STANDABLE_REGION
                             for ref in recipe_refs)
        if len(recipe_refs) > 2 + min(1, region_queries):
            return self._validation(
                ActiveRouteValidationDisposition.STOP,
                ActiveRouteValidationReason.QUERY_LIMIT_EXCEEDED,
                affected=affected,
            )
        if budget is None:
            budget = RouteValidationBudget()
        if type(budget) is not RouteValidationBudget:
            raise ContractViolation("route validation budget must be typed")
        if query_cache is None:
            query_cache = WorldQueryCache(world)
        if (type(query_cache) is not WorldQueryCache
                or query_cache.world is not world):
            raise ContractViolation("route validation cache belongs to another world")
        updates: dict[str, tuple[BlockPos, ...]] = {}
        before_queries = budget.queries_used
        status_reason = {
            QueryStatus.NEEDS_INFORMATION:
                ActiveRouteValidationReason.NEEDS_INFORMATION,
            QueryStatus.BLOCKED: ActiveRouteValidationReason.BLOCKED,
            QueryStatus.UNSUPPORTED: ActiveRouteValidationReason.UNSUPPORTED,
        }
        for recipe_ref in recipe_refs:
            recipe = self._plan.recipe(recipe_ref)
            if (ground_profile is None
                    or ground_profile != recipe.ground_profile
                    or GroundCapabilityIdentity.from_profile(ground_profile)
                        != recipe.capability):
                return self._validation(
                    ActiveRouteValidationDisposition.STOP,
                    ActiveRouteValidationReason.CAPABILITY_IDENTITY_CHANGED,
                    affected=affected,
                    queries_used=budget.queries_used - before_queries,
                )
            if not budget.consume():
                return self._validation(
                    ActiveRouteValidationDisposition.STOP,
                    ActiveRouteValidationReason.QUERY_LIMIT_EXCEEDED,
                    affected=affected,
                    queries_used=budget.queries_used - before_queries,
                )
            status, dependencies = replay_walk_validation_recipe(
                recipe, world, query_cache=query_cache,
            )
            if status is not QueryStatus.FEASIBLE:
                reason = status_reason[status]
                return self._validation(
                    ActiveRouteValidationDisposition.STOP, reason,
                    affected=affected,
                    missing=(dependencies if status is QueryStatus.NEEDS_INFORMATION
                             else ()),
                    queries_used=budget.queries_used - before_queries,
                )
            for owner_ref in owner_refs:
                if owners[owner_ref].recipe_ref == recipe_ref:
                    updates[owner_ref] = dependencies
        refreshed: set[BlockPos] = set()
        for owner_ref, dependencies in updates.items():
            prior = self._active_owner_dependencies[owner_ref]
            current = set(dependencies)
            refreshed.update(prior.symmetric_difference(current))
            self._active_owner_dependencies[owner_ref] = current
        return self._validation(
            ActiveRouteValidationDisposition.CONTINUE,
            ActiveRouteValidationReason.REVALIDATED,
            affected=affected,
            refreshed=tuple(sorted(refreshed)),
            queries_used=budget.queries_used - before_queries,
        )

    def apply_changes(self, changed_cells: tuple[BlockPos,...]) -> None:
        if type(changed_cells) is not tuple:
            raise ContractViolation("route tracker changes must be immutable")
        if self.candidate is None:
            raise ContractViolation("legacy corridor changes require a source candidate")
        dependencies=(set(self.candidate.dependencies)
                      | set(self.route.connection_dependencies)
                      | set(self.route.action_route.dependencies))
        self._invalidated.update(cell for cell in changed_cells if cell in dependencies)

    def update(self, progress_blocks: float) -> CorridorUpdate:
        if (type(progress_blocks) not in (int,float)
                or not math.isfinite(float(progress_blocks)) or progress_blocks<0):
            raise ContractViolation("route progress must be finite and nonnegative")
        if self.candidate is None or self._route_start_index is None:
            raise ContractViolation("legacy corridor update requires a source candidate")
        remaining_connection=max(
            0.0,self.route.connection_length_blocks-progress_blocks,
        )
        graph_progress=max(0.0,progress_blocks-self.route.connection_length_blocks)
        path = self.candidate.path[self._route_start_index:]
        segments = self.candidate.segments[self._route_start_index:]
        positions = [node.position for node in path]
        lengths=[0.0]
        for first,second in zip(positions,positions[1:]):
            lengths.append(lengths[-1]+math.dist(first,second))
        final = self.route.action_route.actions[-1] if self.route.action_route is not None else None
        if (type(final) is WalkSegment and final.traversal_plan is None
                and len(lengths) >= 2):
            # The final graph interval includes the actual terminal tail.
            # A retained centre-then-target connection is longer than a direct
            # chord; substituting only the endpoint loses that distinction.
            lengths[-1] = max(lengths[-2],
                self.route.fixed_route_length_blocks - self.route.connection_length_blocks)
        start=0
        while start+1<len(lengths) and lengths[start+1]<=graph_progress+1e-9:
            start+=1
        nodes=[path[start]];corridor_segments=[];length=0.0
        for index,(edge,node) in enumerate(zip(segments[start:],path[start+1:]),start):
            segment_length=lengths[index+1]-lengths[index]
            if (corridor_segments and remaining_connection+length+segment_length
                    > self.maximum_corridor_blocks):break
            corridor_segments.append(edge);nodes.append(node);length+=segment_length
        dependencies=tuple(sorted(
            {cell for node in nodes for cell in node.dependencies}
            | {cell for edge in corridor_segments for cell in edge.dependencies}
            | (set(self.route.connection_dependencies)
               if remaining_connection>1e-9 else set())
            | (set(self.route.action_route.actions[-1].dependencies)
               if nodes[-1].node_id == self.candidate.path[-1].node_id else set())
        ))
        corridor=ExecutableCorridor(tuple(node.node_id for node in nodes),dependencies,
                                    remaining_connection+length,nodes[-1].node_id)
        route=ActiveRoute(self.route.route_id,self.route.route_revision,
                          self.route.source_request_id,self.route.goal_id,
                          self.route.goal_revision,self.route.world_session,
                          self.route.fixed_route,self.route.fixed_route_length_blocks,
                          self.route.connection_length_blocks,
                          self.route.connection_dependencies,corridor,
                          self.route.action_route,self.route.goal_state,
                          self.route.planning_generation,
                          self.route.work_identity,
                          self.route.validation_plan)
        self.route=route
        if self._invalidated.intersection(dependencies):
            return CorridorUpdate(CorridorStatus.BLOCKED_BY_CHANGE,route,
                                  "active_corridor_dependency_changed")
        return CorridorUpdate(CorridorStatus.READY,route,"active_corridor_ready")
