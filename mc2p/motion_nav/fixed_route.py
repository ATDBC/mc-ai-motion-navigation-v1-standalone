"""B03 fixed-route walking over the shared world and ground-motion contracts."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mc2p.motion_nav.ground_traversal import GroundTraversalPlan

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.block_motion_traits import unsupported_motion_cells
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_motion import (
    GroundControl, GroundMotionProfile, PlanarBodyState, predict_ground,
    world_direction_control,
)
from mc2p.motion_nav.ground_candidate_verifier import (
    CLOSED_GROUND_MOVEMENTS, GroundCandidateFamilyStatus,
    GroundCandidateSafety, GroundCandidateVerificationReport,
    GroundCandidateVerifier, GroundRouteFit,
)
from mc2p.motion_nav.ground_modes import (
    GroundModeProfile, ModeReadiness, evaluate_ground_mode, movement_for_ground_mode,
    observed_ground_mode,
)
from mc2p.motion_nav.ground_route_execution import (
    GroundRouteCapability, GroundRouteExecutionContract, GroundRouteGuardPhase,
)
from mc2p.motion_nav.ground_tracking_policy import (
    GroundTrackingContext, GroundTrackingLimits, GroundTrackingPolicy,
    GroundTrackingRoute,
)
from mc2p.motion_nav.ground_terminal_search import (
    GroundTerminalBranchProof, GroundTerminalSequence,
    GroundTerminalSolveRequest,
)
from mc2p.motion_nav.movement_transition import MovementMode, GoalState
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, InputApplicationStatus, ProjectionStatus, StateAnchor,
    project_movement_command,
)
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, PhysicsState, StateBuildStatus,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.safe_ground_control import (
    VerifiedGroundRouteCandidate, ground_route_state_matches,
    verified_ground_recovery_movement, verified_ground_rollout,
    verified_ground_route_candidate,
)
from mc2p.motion_nav.segment_entry import (
    MotionContinuationRequirement, SegmentEntryWindow, body_fits_segment_entry,
)
from mc2p.motion_nav.world_model import Aabb, BlockPos, WorldQueryCache, WorldView


_EPSILON = 1.0e-9
_MOVEMENTS = tuple(
    MovementV1(forward=forward, strafe=strafe)
    for forward in (-1, 0, 1)
    for strafe in (-1, 0, 1)
)


def _finite(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ContractViolation(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class RoutePoint:
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "z"):
            _finite(getattr(self, name), f"route point {name}")


@dataclass(frozen=True, slots=True)
class FixedRoute:
    route_id: str
    points: tuple[RoutePoint, ...]
    execution_contract: GroundRouteExecutionContract | None = None

    def __post_init__(self) -> None:
        require_identifier(self.route_id, "fixed route id")
        if type(self.points) is not tuple or not self.points or any(
                type(point) is not RoutePoint for point in self.points):
            raise ContractViolation("fixed route requires immutable typed points")
        if self.execution_contract is not None:
            if type(self.execution_contract) is not GroundRouteExecutionContract:
                raise ContractViolation("fixed route execution contract must be typed")
            self.execution_contract.validate_length(sum(math.hypot(b.x-a.x, b.z-a.z)
                                                       for a, b in zip(self.points, self.points[1:])))
            completion = self.execution_contract.completion_region
            if completion is not None and (self.points[-1].x, self.points[-1].y, self.points[-1].z) != completion.reference_point:
                raise ContractViolation("fixed route endpoint differs from completion reference")


@dataclass(frozen=True, slots=True)
class FixedRouteConfig:
    endpoint_tolerance_blocks: float = 0.25
    traversal_endpoint_tolerance_blocks: float = 0.35
    stopped_speed_blocks_per_second: float = 0.1
    minimum_support_fraction: float = 0.15
    preferred_support_fraction: float = 0.80
    wall_soft_margin_blocks: float = 0.15
    motion_prediction_margin_blocks: float = 0.03
    speed_model_tolerance_blocks_per_second: float = 0.10
    maximum_cross_track_blocks: float = 0.45
    lookahead_min_blocks: float = 0.65
    lookahead_max_blocks: float = 1.40
    corner_braking_lookahead_blocks: float = 1.60
    corner_speed_blocks_per_second: float = 1.20
    prediction_ticks: int = 6
    maximum_recovery_ticks: int = 30
    input_lease_ticks: int = 2
    handoff_speed_blocks_per_second: float | None = None
    handoff_entry_window: SegmentEntryWindow | None = None

    def __post_init__(self) -> None:
        for name in (
            "endpoint_tolerance_blocks", "traversal_endpoint_tolerance_blocks",
            "stopped_speed_blocks_per_second",
            "minimum_support_fraction", "preferred_support_fraction",
            "wall_soft_margin_blocks", "maximum_cross_track_blocks",
            "motion_prediction_margin_blocks",
            "speed_model_tolerance_blocks_per_second",
            "lookahead_min_blocks", "lookahead_max_blocks",
            "corner_braking_lookahead_blocks", "corner_speed_blocks_per_second",
        ):
            value = _finite(getattr(self, name), name)
            if value < 0:
                raise ContractViolation(f"{name} must be nonnegative")
        if not 0 < self.minimum_support_fraction <= self.preferred_support_fraction <= 1:
            raise ContractViolation("invalid support fractions")
        if self.lookahead_min_blocks > self.lookahead_max_blocks:
            raise ContractViolation("invalid lookahead range")
        if self.corner_braking_lookahead_blocks < self.lookahead_min_blocks:
            raise ContractViolation("corner braking lookahead is too short")
        if type(self.prediction_ticks) is not int or not 1 <= self.prediction_ticks <= 20:
            raise ContractViolation("prediction ticks must be within 1..20")
        if type(self.maximum_recovery_ticks) is not int or not 1 <= self.maximum_recovery_ticks <= 100:
            raise ContractViolation("recovery ticks must be within 1..100")
        if type(self.input_lease_ticks) is not int or not 1 <= self.input_lease_ticks <= 20:
            raise ContractViolation("input lease ticks must be within 1..20")
        if self.handoff_speed_blocks_per_second is not None:
            value = _finite(
                self.handoff_speed_blocks_per_second, "handoff speed",
            )
            if value < self.stopped_speed_blocks_per_second:
                raise ContractViolation(
                    "handoff speed cannot be below the stopped threshold"
                )
        if (self.handoff_entry_window is not None
                and type(self.handoff_entry_window) is not SegmentEntryWindow):
            raise ContractViolation("handoff entry window must be typed")


def terminal_route_config(config: FixedRouteConfig, profile: GroundMotionProfile,
                          goal: GoalState, endpoint: RoutePoint,
                          execution_contract: GroundRouteExecutionContract | None = None) -> FixedRouteConfig:
    """Use the same final stop domain during screening and actual tracking."""
    if execution_contract is not None and execution_contract.completion_region is not None:
        return replace(config, stopped_speed_blocks_per_second=min(
            config.stopped_speed_blocks_per_second, goal.maximum_terminal_speed_blocks_per_second))
    margin = min(endpoint.x-goal.region.min_x, goal.region.max_x-endpoint.x,
                 endpoint.z-goal.region.min_z, goal.region.max_z-endpoint.z)
    if margin < 0:
        return config
    tolerance = max(1.e-4, margin-config.stopped_speed_blocks_per_second*profile.tick_seconds)
    return replace(config, endpoint_tolerance_blocks=min(config.endpoint_tolerance_blocks,tolerance),
                   traversal_endpoint_tolerance_blocks=min(config.traversal_endpoint_tolerance_blocks,tolerance),
                   stopped_speed_blocks_per_second=min(config.stopped_speed_blocks_per_second,
                                                       goal.maximum_terminal_speed_blocks_per_second))


class FixedRouteState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    BRAKING = "braking"
    NEEDS_INFORMATION = "needs_information"
    BLOCKED = "blocked"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    INPUT_LOST = "input_lost"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    NEEDS_REPLAN = "needs_replan"


class GroundHandoffDisposition(StrEnum):
    NOT_REQUESTED = "not_requested"
    CONSUMED = "consumed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class FixedRouteDecision:
    state: FixedRouteState
    movement: MovementV1
    progress_blocks: float
    cross_track_error_blocks: float
    missing_cells: tuple[BlockPos, ...]
    control_time_ns: int
    input_lease_ticks: int
    reason: str
    handoff_disposition: GroundHandoffDisposition = GroundHandoffDisposition.NOT_REQUESTED
    full_candidates: int = 0
    physics_steps: int = 0
    edge_guard_phase: GroundRouteGuardPhase = GroundRouteGuardPhase.INACTIVE
    submit_input: bool = True
    declared_candidates: int = 0
    classified_candidates: int = 0
    candidate_budget_exhausted: bool = False
    world_dependencies: tuple[BlockPos, ...] = ()
    dependency_valid_until_movement_tick: int | None = None
    route_fit: GroundRouteFit | None = None
    verified_command_index: int | None = None
    expected_movement_tick: int | None = None
    latest_movement_tick: int | None = None


@dataclass(frozen=True, slots=True)
class _GroundTerminalPendingSubmission:
    command_index: int
    control_sequence: int
    requested_tick: int
    latest_tick: int


def _ground_terminal_state_matches(
    observed: PhysicsState,
    predicted: PhysicsState,
) -> bool:
    """Bound execution to the calculator trajectory without exact-float coupling."""
    return (
        observed.session == predicted.session
        and observed.ruleset_id == predicted.ruleset_id
        and observed.state_schema == predicted.state_schema
        and observed.pose == predicted.pose
        and observed.on_ground == predicted.on_ground
        and math.dist(observed.position, predicted.position) <= .035
        and math.dist(
            observed.velocity_blocks_per_tick,
            predicted.velocity_blocks_per_tick,
        ) <= .012
        and abs(math.atan2(
            math.sin(observed.yaw_radians - predicted.yaw_radians),
            math.cos(observed.yaw_radians - predicted.yaw_radians),
        )) <= math.radians(3.0)
    )


@dataclass(frozen=True, slots=True)
class _Projection:
    progress: float
    distance: float
    segment_index: int


class _RouteGeometry:
    def __init__(self, route: FixedRoute) -> None:
        points: list[RoutePoint] = []
        for point in route.points:
            if not points or math.dist((point.x, point.y, point.z),
                                       (points[-1].x, points[-1].y, points[-1].z)) > _EPSILON:
                points.append(point)
        self.points = tuple(points)
        self.lengths: tuple[float, ...] = tuple(
            math.hypot(b.x - a.x, b.z - a.z) for a, b in zip(self.points, self.points[1:])
        )
        cumulative = [0.0]
        for length in self.lengths:
            cumulative.append(cumulative[-1] + length)
        self.cumulative = tuple(cumulative)
        self.total_length = cumulative[-1]

    @property
    def goal(self) -> RoutePoint:
        return self.points[-1]

    def point_at(self, progress: float) -> tuple[float, float]:
        if len(self.points) == 1 or self.total_length <= _EPSILON:
            return self.goal.x, self.goal.z
        progress = min(self.total_length, max(0.0, progress))
        for index, length in enumerate(self.lengths):
            end = self.cumulative[index + 1]
            if progress <= end + _EPSILON:
                if length <= _EPSILON:
                    continue
                ratio = (progress - self.cumulative[index]) / length
                a, b = self.points[index], self.points[index + 1]
                return a.x + (b.x - a.x) * ratio, a.z + (b.z - a.z) * ratio
        return self.goal.x, self.goal.z

    def project(self, x: float, z: float, previous_progress: float,
                segment_index: int, advance_radius: float,
                prefer_next_segment: bool = False) -> _Projection:
        if len(self.points) == 1 or self.total_length <= _EPSILON:
            return _Projection(0.0, math.hypot(x - self.goal.x, z - self.goal.z), 0)
        if type(segment_index) is not int or not 0 <= segment_index < len(self.lengths):
            raise ContractViolation("fixed route segment index is invalid")
        best: _Projection | None = None
        current_end = self.points[segment_index + 1]
        distance_to_end = math.hypot(x - current_end.x, z - current_end.z)
        indices = [segment_index]
        if distance_to_end <= advance_radius and segment_index + 1 < len(self.lengths):
            indices.append(segment_index + 1)
        for index in indices:
            a, b = self.points[index], self.points[index + 1]
            dx, dz = b.x - a.x, b.z - a.z
            length2 = dx * dx + dz * dz
            if length2 <= _EPSILON:
                continue
            ratio = min(1.0, max(0.0, ((x - a.x) * dx + (z - a.z) * dz) / length2))
            px, pz = a.x + dx * ratio, a.z + dz * ratio
            candidate = _Projection(
                self.cumulative[index] + self.lengths[index] * ratio,
                math.hypot(x - px, z - pz), index,
            )
            if candidate.progress + 0.20 < previous_progress:
                continue
            if best is None or (candidate.distance, -candidate.progress) < (best.distance, -best.progress):
                best = candidate
            # A safe lateral approach to the outgoing leg is genuine progress.
            # Use it only after tracking stalls, near the immediate corner;
            # collision, support and release-tail checks still apply normally.
            if (prefer_next_segment and index == segment_index + 1
                    and candidate.distance <= advance_radius
                    and best is not candidate and candidate.progress > best.progress):
                best = candidate
        if best is None:
            px, pz = self.point_at(previous_progress)
            return _Projection(previous_progress, math.hypot(x - px, z - pz), segment_index)
        return best

    def project_current(self, x: float, z: float) -> _Projection:
        """Nearest physical route location, without task-progress hysteresis."""
        if len(self.points) == 1 or self.total_length <= _EPSILON:
            return _Projection(0.0, math.hypot(x-self.goal.x, z-self.goal.z), 0)
        best = None
        for index, length in enumerate(self.lengths):
            if length <= _EPSILON:
                continue
            a, b = self.points[index], self.points[index+1]
            dx, dz = b.x-a.x, b.z-a.z
            ratio = min(1., max(0., ((x-a.x)*dx + (z-a.z)*dz)/(length*length)))
            projection = _Projection(self.cumulative[index] + length*ratio,
                                     math.hypot(x-a.x-dx*ratio, z-a.z-dz*ratio), index)
            if best is None or projection.distance < best.distance - _EPSILON:
                best = projection
        assert best is not None
        return best

    def next_sharp_corner_distance(self, progress: float, segment_index: int) -> float | None:
        if segment_index+1>=len(self.lengths):return None
        first=self.points[segment_index];corner=self.points[segment_index+1]
        third=self.points[segment_index+2]
        incoming=(corner.x-first.x,corner.z-first.z)
        outgoing=(third.x-corner.x,third.z-corner.z)
        first_length=math.hypot(*incoming);second_length=math.hypot(*outgoing)
        if first_length<=_EPSILON or second_length<=_EPSILON:return None
        cosine=(incoming[0]*outgoing[0]+incoming[1]*outgoing[1])/(first_length*second_length)
        if cosine>math.cos(math.radians(30.0)):return None
        return max(0.0,self.cumulative[segment_index+1]-progress)


@dataclass(frozen=True, slots=True)
class _Candidate:
    movement: MovementV1
    score: float
    missing: tuple[BlockPos, ...]
    blocked: bool
    unsupported: bool
    progress_gain: float
    full_replay_eligible: bool = False
    support_boundary: bool = False
    control_ticks: int = 0
    completion_distance_blocks: float = math.inf
    target_distance_blocks: float = math.inf


@dataclass(frozen=True, slots=True)
class _CandidateRollout:
    movement: MovementV1
    states: tuple[PlanarBodyState, ...]
    tracking_end: PlanarBodyState
    base_score: float
    progress_gain: float
    raw_progress_gain: float
    cross_track_blocked: bool
    unsupported: bool


def _horizontal_wall_penalty(
    body: Aabb,
    world: WorldView,
    margin: float,
    query_cache: WorldQueryCache,
) -> float:
    """Return a smooth known-wall proximity cost without changing collision truth."""
    if margin <= 0:
        return 0.0
    penalty = 0.0
    for x in range(math.floor(body.min_x - margin), math.floor(body.max_x + margin) + 1):
        for y in range(math.floor(body.min_y + _EPSILON), math.floor(body.max_y - _EPSILON) + 1):
            for z in range(math.floor(body.min_z - margin), math.floor(body.max_z + margin) + 1):
                position = (x, y, z)
                fact = query_cache.cell(position)
                if fact.block is None or fact.block.fluid or fact.block.collision_kind == "unsupported":
                    continue
                for obstacle in query_cache.collision_boxes(position):
                    if body.max_y <= obstacle.min_y + _EPSILON or body.min_y >= obstacle.max_y - _EPSILON:
                        continue
                    gap_x = max(obstacle.min_x - body.max_x, body.min_x - obstacle.max_x, 0.0)
                    gap_z = max(obstacle.min_z - body.max_z, body.min_z - obstacle.max_z, 0.0)
                    distance = math.hypot(gap_x, gap_z)
                    if distance < margin:
                        penalty += ((margin - distance) / margin) ** 2
    return penalty


def _directional_prediction_box(
    body: Aabb,
    delta: tuple[float, float, float],
    margin: float,
) -> Aabb:
    """Expand prediction uncertainty without inventing a trailing collision.

    The observed body box is authoritative at the start of each predicted
    step.  Position uncertainty still covers the leading and lateral sides,
    where it can turn a nominally clear movement into a collision.  The side
    that is moving away from an obstacle uses the observed boundary; otherwise
    a safely landed body within ``margin`` of the wall would overlap the wall
    only after expansion and could never move away from it.
    """
    delta_x, _, delta_z = delta
    return Aabb(
        body.min_x if delta_x > _EPSILON else body.min_x - margin,
        body.min_y,
        body.min_z if delta_z > _EPSILON else body.min_z - margin,
        body.max_x if delta_x < -_EPSILON else body.max_x + margin,
        body.max_y,
        body.max_z if delta_z < -_EPSILON else body.max_z + margin,
    )


@dataclass(frozen=True, slots=True)
class GroundHandoffTarget:
    position: tuple[float, float, float]
    first_tick: int
    movements: tuple[MovementV1, ...]
    latest_tick: int
    movement_yaws_radians: tuple[float, ...] = ()
    mode: MovementMode | None = None

    def __post_init__(self) -> None:
        if (type(self.position) is not tuple or len(self.position) != 3
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in self.position)
                or type(self.first_tick) is not int or self.first_tick < 0
                or type(self.latest_tick) is not int
                or not self.first_tick <= self.latest_tick < self.first_tick + 20
                or type(self.movements) is not tuple
                or not 1 <= len(self.movements) <= 4
                or any(type(value) is not MovementV1 for value in self.movements)
                or type(self.movement_yaws_radians) is not tuple
                or (self.movement_yaws_radians
                    and len(self.movement_yaws_radians) != len(self.movements))
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in self.movement_yaws_radians)
                or (self.mode is not None and type(self.mode) is not MovementMode)):
            raise ContractViolation("ground handoff target requires a bounded typed prefix")


class FixedRouteController:
    """Owns one fixed route and proposes one safe ordinary-ground input per frame."""

    def __init__(self, profile: GroundMotionProfile,
                 config: FixedRouteConfig = FixedRouteConfig(), *,
                 mode_profile: GroundModeProfile | None = None) -> None:
        if type(profile) is not GroundMotionProfile or type(config) is not FixedRouteConfig:
            raise ContractViolation("fixed route controller requires typed motion configuration")
        if mode_profile is not None and type(mode_profile) is not GroundModeProfile:
            raise ContractViolation("fixed route ground mode profile must be typed")
        if mode_profile is not None and mode_profile.motion != profile:
            raise ContractViolation("fixed route ground mode and motion profiles disagree")
        self.profile = profile
        self.config = config
        self.mode_profile = mode_profile
        self.state = FixedRouteState.IDLE
        self._route: FixedRoute | None = None
        self._geometry: _RouteGeometry | None = None
        self._session = None
        self._last_sequence: int | None = None
        self._progress = 0.0
        self._cross_track = 0.0
        self._cancel_requested = False
        self._previous_movement = MovementV1()
        self._handoff_target_hint: GroundHandoffTarget | None = None
        self._progress_anchor = 0.0
        self._terminal_distance_anchor = math.inf
        self._terminal_stop_distance_anchor = math.inf
        self._terminal_speed_anchor = math.inf
        self._no_progress_frames = 0
        self._stall_detected = False
        self._segment_index = 0
        self._mode_pending_frames = 0
        self._geometric_support_frames = 0
        self._traversal_plan: GroundTraversalPlan | None = None
        self._traversal_tick_index = 0
        self._traversal_replan_pending = False
        self._continuation: MotionContinuationRequirement | None = None
        self._full_candidates = 0
        self._physics_steps = 0
        self._guard_phase = GroundRouteGuardPhase.INACTIVE
        self._ordinary_replays: dict[MovementV1, VerifiedGroundRouteCandidate] = {}
        self._closed_replays: dict[tuple[MovementV1, int], VerifiedGroundRouteCandidate] = {}
        self._closed_route_fits: dict[tuple[MovementV1, int], GroundRouteFit] = {}
        self._declared_candidates = 0
        self._classified_candidates = 0
        self._candidate_budget_exhausted = False
        self._candidate_neutral_safe = False
        self._active_tail_dependencies: tuple[BlockPos, ...] = ()
        self._active_tail_valid_until_tick: int | None = None
        self._active_route_fit: GroundRouteFit | None = None
        self._guard_release_frames = 0
        self._guard_outside_frames = 0
        self._ground_terminal_basis: GroundTerminalSolveRequest | None = None
        self._ground_terminal_sequence: GroundTerminalSequence | None = None
        self._ground_terminal_branch: GroundTerminalBranchProof | None = None
        self._ground_terminal_start_tick: int | None = None
        self._ground_terminal_command_index = 0
        self._ground_terminal_pending: _GroundTerminalPendingSubmission | None = None
        self._ground_terminal_tail_index: int | None = None
        self._ground_terminal_tail_start_tick: int | None = None
        self._ground_terminal_recovering = False
        self._ground_terminal_recovery_deadline_tick: int | None = None
        self._ground_terminal_recovery_terminal = FixedRouteState.NEEDS_REPLAN

    def start(
        self, route: FixedRoute, frame: NavigationFrame, *,
        traversal_plan: GroundTraversalPlan | None = None,
        continuation: MotionContinuationRequirement | None = None,
    ) -> None:
        from mc2p.motion_nav.ground_traversal import GroundTraversalPlan
        if type(route) is not FixedRoute or type(frame) is not NavigationFrame:
            raise ContractViolation("starting fixed route requires a route and navigation frame")
        if route.execution_contract is not None:
            if route.execution_contract.profile_id != self.profile.profile_id:
                raise ContractViolation("fixed route execution profile does not match controller")
            if (traversal_plan is not None or (self.mode_profile is not None
                    and self.mode_profile.mode is not MovementMode.WALK)):
                raise ContractViolation("route edge guard requires ordinary fixed ground execution")
        varying_height = (
            max(point.y for point in route.points)
            - min(point.y for point in route.points) > 0.05
        )
        if varying_height and traversal_plan is None:
            raise ContractViolation(
                "varying-height fixed route requires a traversal proof"
            )
        if (traversal_plan is not None
                and (type(traversal_plan) is not GroundTraversalPlan
                     or (not traversal_plan.surface_node_path
                         and route.points[-len(traversal_plan.route.points):]
                         != traversal_plan.route.points))):
            raise ContractViolation(
                "fixed route traversal proof does not match the route"
            )
        if continuation is not None and type(continuation) is not MotionContinuationRequirement:
            raise ContractViolation("fixed route continuation must be typed")
        if traversal_plan is not None and traversal_plan.continuation != continuation:
            raise ContractViolation("fixed route continuation must match its new traversal proof")
        if self.state not in {
            FixedRouteState.IDLE, FixedRouteState.CANCELLED, FixedRouteState.SUCCEEDED,
            FixedRouteState.INPUT_LOST, FixedRouteState.FAILED, FixedRouteState.UNSUPPORTED,
        }:
            raise ContractViolation("fixed route controller is already active")
        self._route = route
        self._geometry = _RouteGeometry(route)
        self._session = frame.session
        self._last_sequence = None
        self._progress = 0.0
        self._cross_track = 0.0
        self._cancel_requested = False
        self._previous_movement = MovementV1()
        self._handoff_target_hint = None
        self._progress_anchor = 0.0
        self._terminal_distance_anchor = math.inf
        self._terminal_stop_distance_anchor = math.inf
        self._terminal_speed_anchor = math.inf
        self._no_progress_frames = 0
        self._stall_detected = False
        self._segment_index = 0
        self._mode_pending_frames = 0
        self._geometric_support_frames = 0
        self._traversal_plan = traversal_plan
        self._traversal_tick_index = 0
        self._traversal_replan_pending = False
        self._continuation = continuation
        self._guard_phase = GroundRouteGuardPhase.INACTIVE
        self._guard_release_frames = 0
        self._guard_outside_frames = 0
        self._active_tail_dependencies = ()
        self._active_tail_valid_until_tick = None
        self._active_route_fit = None
        self._clear_ground_terminal()
        self.state = FixedRouteState.RUNNING

    @property
    def ground_terminal_pending(self) -> bool:
        return self._ground_terminal_basis is not None

    @property
    def ground_terminal_active(self) -> bool:
        return self._ground_terminal_sequence is not None

    @property
    def ground_terminal_holds_body(self) -> bool:
        return (self._ground_terminal_basis is not None
                or self.ground_terminal_active
                or self._ground_terminal_pending is not None)

    def _clear_ground_terminal(self) -> None:
        self._ground_terminal_basis = None
        self._ground_terminal_sequence = None
        self._ground_terminal_branch = None
        self._ground_terminal_start_tick = None
        self._ground_terminal_command_index = 0
        self._ground_terminal_pending = None
        self._ground_terminal_tail_index = None
        self._ground_terminal_tail_start_tick = None
        self._ground_terminal_recovering = False
        self._ground_terminal_recovery_deadline_tick = None
        self._ground_terminal_recovery_terminal = FixedRouteState.NEEDS_REPLAN

    @staticmethod
    def _ground_terminal_anchor_domain_matches(
        expected: StateAnchor,
        observed: StateAnchor,
    ) -> bool:
        """Keep a proof inside the phase and projection domain it was built for."""
        return (
            observed.session == expected.session
            and observed.phase is expected.phase
            and observed.ruleset_id == expected.ruleset_id
            and observed.state_schema == expected.state_schema
            and observed.input_projection_version
                == expected.input_projection_version
        )

    def begin_ground_terminal_solve(
        self,
        request: GroundTerminalSolveRequest,
    ) -> bool:
        """Hold one typed solve basis while ordinary ground keeps body ownership."""
        if type(request) is not GroundTerminalSolveRequest or self._route is None:
            raise ContractViolation("ground terminal basis requires an active fixed route")
        contract = self._route.execution_contract
        if (contract is None or contract.completion_region != request.completion
                or request.profile.profile_id != self.profile.profile_id):
            return False
        self._ground_terminal_basis = request
        if self.state in {FixedRouteState.BLOCKED, FixedRouteState.BRAKING}:
            self.state = FixedRouteState.RUNNING
        return True

    def retire_ground_terminal(self) -> None:
        """Retire unstarted work; an active sequence first enters its safe tail."""
        if self._ground_terminal_sequence is None:
            self._clear_ground_terminal()
            return
        self._ground_terminal_recovering = True
        self._ground_terminal_tail_index = 0

    def install_ground_terminal_sequence(
        self,
        sequence: GroundTerminalSequence,
        anchor: StateAnchor,
        ledger: InputApplicationLedger | None = None,
    ) -> bool:
        if (type(sequence) is not GroundTerminalSequence
                or type(anchor) is not StateAnchor):
            raise ContractViolation("ground terminal install requires typed proof and anchor")
        basis = self._ground_terminal_basis
        if (basis is None
                or sequence.work_identity != basis.work_identity
                or sequence.anchor != basis.anchor
                or sequence.goal_id != basis.goal_id
                or sequence.goal_revision != basis.goal_revision
                or sequence.route_id != basis.route_id
                or sequence.route_revision != basis.route_revision
                or sequence.action_index != basis.action_index
                or sequence.execution_window != basis.execution_window
                or sequence.completion != basis.completion
                or sequence.ruleset_id != anchor.ruleset_id
                or not self._ground_terminal_anchor_domain_matches(
                    sequence.anchor, anchor,
                )):
            return False
        if sequence.preparation_inputs:
            if ledger is None:
                return False
            first_tick = sequence.anchor.movement_tick_id + 1
            for offset, expected in enumerate(sequence.preparation_inputs):
                sample = ledger.sample(first_tick + offset)
                if (sample is None
                        or abs(float(sample.forward) - expected.forward) > 1.0e-9
                        or abs(float(sample.strafe) - expected.strafe) > 1.0e-9
                        or sample.jump != expected.jump
                        or sample.sneak != expected.sneak
                        or sample.sprint != expected.sprint):
                    return False
        start_tick = anchor.movement_tick_id + 1
        if not sequence.execution_window.allows_start(start_tick):
            return False
        branch = (
            sequence.normal
            if start_tick == sequence.execution_window.earliest_start_tick
            else sequence.late1
            if start_tick == sequence.execution_window.latest_start_tick
            else None
        )
        if branch is None or not _ground_terminal_state_matches(
                anchor.physics_state, branch.entry_state):
            return False
        self._ground_terminal_sequence = sequence
        self._ground_terminal_branch = branch
        self._ground_terminal_start_tick = start_tick
        self._ground_terminal_command_index = 0
        self._ground_terminal_pending = None
        self._ground_terminal_tail_index = None
        self._ground_terminal_tail_start_tick = None
        self._ground_terminal_recovering = False
        self.state = FixedRouteState.RUNNING
        return True

    def register_ground_terminal_submission(
        self,
        command_index: int,
        *,
        control_sequence: int,
        requested_movement_tick: int,
        requested_latest_movement_tick: int | None = None,
    ) -> None:
        sequence = self._ground_terminal_sequence
        if sequence is None or self._ground_terminal_branch is None:
            raise ContractViolation("ground terminal sequence is not active")
        if requested_latest_movement_tick is None:
            requested_latest_movement_tick = requested_movement_tick
        if (command_index != self._ground_terminal_command_index
                or self._ground_terminal_pending is not None
                or requested_movement_tick != self._ground_terminal_expected_tick()
                or requested_latest_movement_tick != requested_movement_tick):
            raise ContractViolation("ground terminal submission differs from proof")
        self._ground_terminal_pending = _GroundTerminalPendingSubmission(
            command_index, control_sequence, requested_movement_tick,
            requested_latest_movement_tick,
        )

    def _ground_terminal_expected_tick(self) -> int:
        assert self._ground_terminal_start_tick is not None
        return self._ground_terminal_start_tick + self._ground_terminal_command_index

    def _ground_terminal_begin_recovery(
        self,
        anchor: StateAnchor | None = None,
        terminal: FixedRouteState = FixedRouteState.NEEDS_REPLAN,
    ) -> None:
        if not self._ground_terminal_recovering:
            self._ground_terminal_recovering = True
            self._ground_terminal_tail_index = 0
            self._ground_terminal_recovery_terminal = terminal
        elif terminal is FixedRouteState.INPUT_LOST:
            self._ground_terminal_recovery_terminal = terminal
        branch = self._ground_terminal_branch
        if branch is not None:
            tail = branch.prefix_tails[min(
                self._ground_terminal_command_index,
                len(branch.prefix_tails) - 1,
            )]
            self._active_tail_dependencies = tail.dependencies
            if (anchor is not None
                    and self._ground_terminal_recovery_deadline_tick is None):
                maximum = min(
                    len(tail.trajectory), self.config.maximum_recovery_ticks,
                )
                self._ground_terminal_recovery_deadline_tick = (
                    anchor.movement_tick_id + max(1, maximum)
                )

    def _decide_ground_terminal_recovery(
        self,
        frame: NavigationFrame,
        anchor: StateAnchor,
        started: int,
    ) -> FixedRouteDecision:
        """Choose recovery from the observed state; an old tail grants no input."""
        self._ground_terminal_begin_recovery(anchor)
        current = anchor.physics_state
        deadline = self._ground_terminal_recovery_deadline_tick
        speed = math.hypot(
            current.velocity_blocks_per_tick[0],
            current.velocity_blocks_per_tick[2],
        ) * 20.0
        if current.on_ground and speed <= self.config.stopped_speed_blocks_per_second:
            terminal = (
                FixedRouteState.CANCELLED
                if self._cancel_requested else
                self._ground_terminal_recovery_terminal
            )
            self._clear_ground_terminal()
            self.state = terminal
            return self._decision(
                started, MovementV1(),
                "ground_terminal_cancelled_after_safe_recovery"
                if terminal is FixedRouteState.CANCELLED else
                "ground_terminal_deviation_reanchored",
                submit_input=False,
            )
        if deadline is not None and anchor.movement_tick_id >= deadline:
            self._clear_ground_terminal()
            self.state = FixedRouteState.INPUT_LOST
            return self._decision(
                started, MovementV1(),
                "ground_terminal_recovery_deadline_exhausted",
                submit_input=False,
            )
        remaining = max(
            1,
            self.config.maximum_recovery_ticks
            if deadline is None else deadline - anchor.movement_tick_id,
        )
        neutral_safe = verified_ground_rollout(
            frame, current, MovementV1(),
            control_ticks=1,
            tail_ticks=max(0, remaining - 1),
            minimum_support=self.config.minimum_support_fraction,
        )
        if neutral_safe is not None:
            self.state = FixedRouteState.CANCELLING
            return self._decision(
                started, MovementV1(),
                "ground_terminal_current_state_neutral_recovery",
                input_lease_ticks=1,
            )
        recovery = verified_ground_recovery_movement(frame, current)
        if recovery is not None:
            self.state = FixedRouteState.CANCELLING
            return self._decision(
                started, recovery,
                "ground_terminal_current_state_verified_recovery",
                input_lease_ticks=1,
            )
        self._clear_ground_terminal()
        self.state = FixedRouteState.INPUT_LOST
        return self._decision(
            started, MovementV1(),
            "ground_terminal_current_state_recovery_unavailable",
            submit_input=False,
        )

    def _consume_ground_terminal_submission(
        self,
        anchor: StateAnchor,
        ledger: InputApplicationLedger,
    ) -> str | None:
        pending = self._ground_terminal_pending
        sequence = self._ground_terminal_sequence
        if pending is None or sequence is None:
            return None
        record = ledger.record(pending.control_sequence)
        if record is None:
            if anchor.movement_tick_id >= pending.latest_tick:
                self._ground_terminal_begin_recovery(
                    anchor, FixedRouteState.INPUT_LOST,
                )
                self._ground_terminal_pending = None
                return "ground_terminal_receipt_missing"
            return "ground_terminal_awaiting_application"
        if record.action.movement != sequence.commands[pending.command_index]:
            self._ground_terminal_begin_recovery(
                anchor, FixedRouteState.INPUT_LOST,
            )
            self._ground_terminal_pending = None
            return "ground_terminal_applied_command_changed"
        if record.status is InputApplicationStatus.APPLIED:
            if (len(record.applied_ticks) != 1
                    or record.applied_ticks[0] != pending.requested_tick):
                self._ground_terminal_begin_recovery(
                    anchor, FixedRouteState.INPUT_LOST,
                )
                self._ground_terminal_pending = None
                return "ground_terminal_input_tick_changed"
            self._ground_terminal_command_index += 1
            self._ground_terminal_pending = None
            return None
        if record.status in {
                InputApplicationStatus.APPLIED_OUTSIDE_WINDOW,
                InputApplicationStatus.REJECTED,
                InputApplicationStatus.AMBIGUOUS,
                InputApplicationStatus.EXPIRED,
                InputApplicationStatus.SUPERSEDED,
        }:
            self._ground_terminal_begin_recovery(
                anchor, FixedRouteState.INPUT_LOST,
            )
            self._ground_terminal_pending = None
            return f"ground_terminal_input_{record.status.value}"
        return "ground_terminal_awaiting_application"

    def _decide_ground_terminal(
        self,
        frame: NavigationFrame,
        anchor: StateAnchor,
        ledger: InputApplicationLedger,
        started: int,
    ) -> FixedRouteDecision:
        sequence = self._ground_terminal_sequence
        branch = self._ground_terminal_branch
        assert sequence is not None and branch is not None
        if not self._ground_terminal_anchor_domain_matches(
                sequence.anchor, anchor):
            self._ground_terminal_begin_recovery(anchor)
        if (sequence.goal_id != self._ground_terminal_basis.goal_id
                or sequence.goal_revision != self._ground_terminal_basis.goal_revision
                or sequence.route_id != self._ground_terminal_basis.route_id
                or sequence.route_revision != self._ground_terminal_basis.route_revision):
            self._ground_terminal_begin_recovery(anchor)
        if set(frame.changed_cells).intersection(sequence.dependencies):
            self._ground_terminal_begin_recovery(anchor)
        if self._cancel_requested:
            self._ground_terminal_begin_recovery(
                anchor, FixedRouteState.CANCELLED,
            )

        pending_reason = self._consume_ground_terminal_submission(anchor, ledger)
        if (self._ground_terminal_recovering
                and self._ground_terminal_pending is not None):
            self._ground_terminal_begin_recovery(anchor)
            deadline = self._ground_terminal_recovery_deadline_tick
            if deadline is not None and anchor.movement_tick_id >= deadline:
                self._clear_ground_terminal()
                self.state = FixedRouteState.INPUT_LOST
                return self._decision(
                    started, MovementV1(),
                    "ground_terminal_recovery_submission_unresolved",
                    submit_input=False,
                )
            self.state = FixedRouteState.CANCELLING
            return self._decision(
                started, MovementV1(),
                "ground_terminal_recovery_waiting_for_previous_submission",
                submit_input=False,
            )
        if pending_reason is not None:
            if not self._ground_terminal_recovering:
                return self._decision(
                    started, MovementV1(), pending_reason,
                    input_lease_ticks=1,
                )

        if self._ground_terminal_recovering:
            return self._decide_ground_terminal_recovery(
                frame, anchor, started,
            )

        command_index = self._ground_terminal_command_index
        if command_index >= len(sequence.commands):
            if self._ground_terminal_tail_start_tick is None:
                self._ground_terminal_tail_start_tick = anchor.movement_tick_id
            tail = branch.prefix_tails[-1].trajectory
            offset = anchor.movement_tick_id - self._ground_terminal_tail_start_tick
            expected = tail[min(max(offset, 0), len(tail) - 1)]
            if not _ground_terminal_state_matches(anchor.physics_state, expected):
                self._ground_terminal_begin_recovery(anchor)
                self.state = FixedRouteState.CANCELLING
                return self._decision(
                    started, MovementV1(), "ground_terminal_tail_deviated",
                    input_lease_ticks=1,
                )
            if offset >= len(tail) - 1:
                completion = sequence.completion
                speed = math.hypot(
                    anchor.physics_state.velocity_blocks_per_tick[0],
                    anchor.physics_state.velocity_blocks_per_tick[2],
                ) * 20.0
                if (completion.contains(anchor.physics_state.position)
                        and anchor.physics_state.on_ground
                        and anchor.physics_state.pose == "standing"
                        and speed <= self.config.stopped_speed_blocks_per_second):
                    self._clear_ground_terminal()
                    self.state = FixedRouteState.SUCCEEDED
                    return self._decision(
                        started, MovementV1(), "ground_terminal_sequence_complete",
                    )
                self._ground_terminal_begin_recovery(anchor)
                self.state = FixedRouteState.CANCELLING
                return self._decision(
                    started, MovementV1(), "ground_terminal_exit_not_observed",
                )
            self.state = FixedRouteState.RUNNING
            return self._decision(
                started, MovementV1(), "ground_terminal_neutral_tail",
                input_lease_ticks=1,
            )

        expected_state = branch.command_trajectory[command_index]
        if not _ground_terminal_state_matches(anchor.physics_state, expected_state):
            self._ground_terminal_begin_recovery(anchor)
            self.state = FixedRouteState.CANCELLING
            return self._decision(
                started, MovementV1(), "ground_terminal_trajectory_deviated",
                input_lease_ticks=1,
            )
        expected_tick = self._ground_terminal_expected_tick()
        if anchor.movement_tick_id + 1 != expected_tick:
            self._ground_terminal_begin_recovery(anchor)
            self.state = FixedRouteState.CANCELLING
            return self._decision(
                started, MovementV1(), "ground_terminal_command_window_expired",
                input_lease_ticks=1,
            )
        self.state = FixedRouteState.RUNNING
        return self._decision(
            started, sequence.commands[command_index],
            "submit_verified_ground_terminal_command",
            input_lease_ticks=1,
            verified_command_index=command_index,
            expected_movement_tick=expected_tick,
            latest_movement_tick=expected_tick,
        )

    def set_handoff_target(self, target: GroundHandoffTarget | None) -> None:
        """Guide tracking toward a conditional entry; never authorize an action."""
        if target is not None and type(target) is not GroundHandoffTarget:
            raise ContractViolation("ground handoff target must be typed")
        self._handoff_target_hint = target

    def cancel(self) -> None:
        if self.state in {FixedRouteState.RUNNING, FixedRouteState.BRAKING,
                          FixedRouteState.NEEDS_INFORMATION, FixedRouteState.BLOCKED}:
            self._cancel_requested = True
            self.state = FixedRouteState.CANCELLING

    @staticmethod
    def _planar(frame: NavigationFrame) -> PlanarBodyState:
        body = frame.body
        return PlanarBodyState(
            body.position[0], body.position[2],
            body.velocity_blocks_per_second[0], body.velocity_blocks_per_second[2],
            body.yaw_radians,
        )

    def _decision(self, started: int, movement: MovementV1, reason: str,
                  missing: tuple[BlockPos, ...] = (), *,
                  handoff_disposition: GroundHandoffDisposition | None = None,
                  input_lease_ticks: int | None = None,
                  submit_input: bool = True,
                  verified_command_index: int | None = None,
                  expected_movement_tick: int | None = None,
                  latest_movement_tick: int | None = None) -> FixedRouteDecision:
        if (self.mode_profile is not None
                and self._guard_phase is GroundRouteGuardPhase.INACTIVE
                and not (self.mode_profile.mode is MovementMode.SPRINT
                         and self.state in {
                             FixedRouteState.BRAKING, FixedRouteState.CANCELLING,
                         })
                and self.state not in {
                    FixedRouteState.CANCELLED, FixedRouteState.SUCCEEDED,
                    FixedRouteState.INPUT_LOST, FixedRouteState.FAILED,
                    FixedRouteState.UNSUPPORTED,
                }):
            movement = movement_for_ground_mode(self.mode_profile, movement)
        self._previous_movement = movement
        if handoff_disposition is None:
            handoff_disposition = (
                GroundHandoffDisposition.REJECTED
                if self._handoff_target_hint is not None
                else GroundHandoffDisposition.NOT_REQUESTED
            )
        if input_lease_ticks is None:
            input_lease_ticks = self.config.input_lease_ticks
        elif type(input_lease_ticks) is not int or input_lease_ticks not in (1, 2):
            raise ContractViolation("ordinary ground decision lease must be one or two ticks")
        return FixedRouteDecision(
            self.state, movement, self._progress, self._cross_track,
            tuple(sorted(set(missing))), time.perf_counter_ns() - started,
            input_lease_ticks, reason, handoff_disposition,
            self._full_candidates, self._physics_steps, self._guard_phase,
            submit_input, self._declared_candidates, self._classified_candidates,
            self._candidate_budget_exhausted,
            self._active_tail_dependencies,
            self._active_tail_valid_until_tick,
            self._active_route_fit,
            verified_command_index,
            expected_movement_tick,
            latest_movement_tick,
        )

    def _prepared_handoff_movement(self, frame: NavigationFrame) -> MovementV1 | None:
        """Accept the original projected input, without changing its yaw or mode."""
        hint = self._handoff_target_hint
        tick = frame.body.movement_tick_id
        if hint is None or tick is None or self._cancel_requested:
            return None
        index = tick + 1 - hint.first_tick
        if index < 0 or tick + 1 > hint.latest_tick:
            return None
        mode = self.mode_profile.mode if self.mode_profile is not None else MovementMode.WALK
        if (observed_ground_mode(frame.body) is not mode
                or (hint.mode is not None and hint.mode is not mode)):
            return None
        if hint.movement_yaws_radians:
            yaw = hint.movement_yaws_radians[min(index, len(hint.movements) - 1)]
            delta = (frame.body.yaw_radians - yaw + math.pi) % (2.0 * math.pi) - math.pi
            if abs(delta) > 1.0e-7:
                return None
        movement = hint.movements[index] if index < len(hint.movements) else MovementV1()
        if movement.jump:
            return None
        if (self.mode_profile is not None
                and movement_for_ground_mode(self.mode_profile, movement) != movement):
            return None
        return movement

    def _continuation_projection(self, projection: _Projection, x: float, z: float) -> _Projection:
        """Keep following the proved successor lane after the old reference end."""
        if self._continuation is None:
            return projection
        assert self._geometry is not None
        last_segment = len(self._geometry.lengths) - 1
        if ((projection.segment_index != last_segment and self._segment_index != last_segment)
                or max(projection.progress, self._progress)
                < self._geometry.total_length - self.config.endpoint_tolerance_blocks):
            return projection
        window = self._continuation.entry_window
        dx, dz = window.horizontal_approach_direction
        offset_x, offset_z = x - window.reference_point[0], z - window.reference_point[2]
        longitudinal = self._continuation.progress((x, window.reference_point[1], z))
        outside = max(window.minimum_longitudinal_offset_blocks - longitudinal,
                      longitudinal - window.maximum_longitudinal_offset_blocks, 0.0)
        distance = min(projection.distance, math.hypot(outside, -offset_x * dz + offset_z * dx))
        goal = self._geometry.goal
        successor_progress = (self._geometry.total_length + longitudinal
                              - self._continuation.progress((goal.x, goal.y, goal.z)))
        return _Projection(max(projection.progress, successor_progress), distance, last_segment)

    @staticmethod
    def _axis(value: float) -> int:
        if abs(value) < 0.35:
            return 0
        return 1 if value > 0 else -1

    def _traversal_movement(self, frame: NavigationFrame, index: int) -> MovementV1:
        assert self._traversal_plan is not None
        sampled = self._traversal_plan.inputs[index]
        sine = math.sin(sampled.movement_yaw_radians)
        cosine = math.cos(sampled.movement_yaw_radians)
        direction_x = sampled.strafe * cosine - sampled.forward * sine
        direction_z = sampled.forward * cosine + sampled.strafe * sine
        control = world_direction_control(
            direction_x, direction_z, frame.body.yaw_radians,
        )
        return MovementV1(
            forward=self._axis(control.forward),
            strafe=self._axis(-control.strafe),
            jump=sampled.jump,
            sneak=sampled.sneak,
            sprint=sampled.sprint,
        )

    def _traversal_physics_state(
        self, frame: NavigationFrame, provided: PhysicsState | None,
    ) -> PhysicsState | None:
        assert self._traversal_plan is not None
        if provided is not None:
            if (type(provided) is not PhysicsState
                    or provided.session != frame.session):
                raise ContractViolation(
                    "ground traversal physics state belongs to another frame"
                )
            return provided
        template = self._traversal_plan.trajectory[0]
        built = build_physics_state(
            frame, JAVA_1_21_RULESET, {
                "jumping_cooldown_ticks": template.jumping_cooldown_ticks,
                "movement_speed_attribute": template.movement_speed_attribute,
                "step_height_blocks": template.step_height_blocks,
                "gravity_attribute": template.gravity_attribute,
                "jump_strength_attribute": template.jump_strength_attribute,
            },
        )
        return built.state if built.status is StateBuildStatus.READY else None

    @staticmethod
    def _inside_traversal_corridor(
        state: PhysicsState, corridor: tuple[Aabb, ...],
    ) -> bool:
        x, y, z = state.position
        return any(
            box.min_x <= x <= box.max_x
            and box.min_y <= y <= box.max_y
            and box.min_z <= z <= box.max_z
            for box in corridor
        )

    def _verified_traversal_movement(
        self,
        frame: NavigationFrame,
        state: PhysicsState,
        target: tuple[float, float],
        *,
        braking: bool,
    ) -> tuple[MovementV1 | None, tuple[BlockPos, ...], bool]:
        """Choose one command from the current state inside the proven corridor."""
        assert self._traversal_plan is not None
        body = self._planar(frame)
        rollouts = sorted(
            (self._prepare_candidate_rollout(
                body, movement, target, braking=braking,
            ) for movement in _MOVEMENTS),
            key=self._rollout_key,
        )
        missing: set[BlockPos] = set()
        saw_unsupported = False
        for rollout in rollouts:
            if rollout.unsupported or rollout.cross_track_blocked:
                continue
            safe, candidate_missing, unsupported = self._traversal_movement_safety(
                frame, state, rollout.movement,
            )
            missing.update(candidate_missing)
            saw_unsupported = saw_unsupported or unsupported
            if safe:
                return rollout.movement, tuple(sorted(missing)), saw_unsupported
        return None, tuple(sorted(missing)), saw_unsupported

    def _traversal_movement_safety(
        self, frame: NavigationFrame, state: PhysicsState, movement: MovementV1,
    ) -> tuple[bool, tuple[BlockPos, ...], bool]:
        """Prove the actual lease and every remaining neutral drift tick."""
        assert self._traversal_plan is not None
        world = PhysicsWorldView(frame.world, JAVA_1_21_RULESET)
        simulated = state
        commands = ((movement,) * self.config.input_lease_ticks
                    + (MovementV1(),) * self.config.maximum_recovery_ticks)
        for command_index, command in enumerate(commands):
            if (command_index == self.config.input_lease_ticks
                    and self._continuation is not None
                    and self._continuation.accepts(state)):
                from mc2p.motion_nav.ground_traversal import (
                    GroundTraversalStatus, verify_ground_continuation_stop_tail,
                )
                status, _, _, missing = verify_ground_continuation_stop_tail(
                    simulated, world, self._traversal_plan.corridor, self._continuation,
                )
                return (status is GroundTraversalStatus.VERIFIED, missing,
                        status is GroundTraversalStatus.UNSUPPORTED)
            if (command_index >= self.config.input_lease_ticks and simulated.on_ground
                    and math.hypot(simulated.velocity_blocks_per_tick[0],
                                   simulated.velocity_blocks_per_tick[2]) <= _EPSILON):
                return True, (), False
            projected = project_movement_command(simulated, command)
            if projected.status is not ProjectionStatus.READY:
                return False, (), True
            assert projected.tick_input is not None
            calculated = physics_step(simulated, projected.tick_input, world, JAVA_1_21_RULESET)
            if calculated.status is CalculationStatus.NEEDS_WORLD:
                return False, calculated.missing_cells, False
            if calculated.status is not CalculationStatus.OK:
                return False, (), calculated.status is CalculationStatus.UNSUPPORTED
            assert calculated.next_state is not None
            simulated = calculated.next_state
            if not self._inside_traversal_corridor(simulated, self._traversal_plan.corridor):
                return False, (), False
        stopped = (simulated.on_ground
                   and math.hypot(simulated.velocity_blocks_per_tick[0],
                                  simulated.velocity_blocks_per_tick[2]) <= _EPSILON)
        return stopped, (), False

    def _decide_traversal(
        self, frame: NavigationFrame, started: int,
        physics_state: PhysicsState | None,
    ) -> FixedRouteDecision:
        assert self._traversal_plan is not None
        assert self._geometry is not None
        plan = self._traversal_plan
        if set(frame.changed_cells).intersection(plan.dependencies):
            if not frame.body.is_on_ground:
                self._traversal_replan_pending = True
                self.state = FixedRouteState.RUNNING
                return self._decision(
                    started, MovementV1(),
                    "ground_traversal_dependency_changed_awaiting_landing",
                )
            self.state = FixedRouteState.NEEDS_REPLAN
            return self._decision(
                started, MovementV1(), "ground_traversal_dependency_changed",
            )
        if self._traversal_replan_pending and frame.body.is_on_ground:
            self.state = FixedRouteState.NEEDS_REPLAN
            return self._decision(
                started, MovementV1(), "ground_traversal_replan_after_landing",
            )
        state = self._traversal_physics_state(frame, physics_state)
        if state is None or frame.body.pose != "standing":
            self.state = FixedRouteState.NEEDS_REPLAN
            return self._decision(
                started, MovementV1(), "ground_traversal_state_unavailable",
            )
        projection = self._geometry.project(
            frame.body.position[0], frame.body.position[2],
            self._progress, self._segment_index,
            self.config.maximum_cross_track_blocks,
        )
        projection = self._continuation_projection(projection, state.position[0], state.position[2])
        self._progress = max(self._progress, projection.progress)
        self._segment_index = max(self._segment_index, projection.segment_index)
        self._cross_track = projection.distance
        if (self._cross_track > plan.maximum_cross_track_blocks
                or not self._inside_traversal_corridor(state, plan.corridor)):
            if frame.body.is_on_ground:
                self.state = FixedRouteState.NEEDS_REPLAN
                return self._decision(
                    started, MovementV1(),
                    "ground_traversal_left_verified_envelope",
                )
            self._traversal_replan_pending = True
            self.state = FixedRouteState.RUNNING
            return self._decision(
                started, MovementV1(),
                "ground_traversal_left_envelope_awaiting_landing",
            )
        if self._progress >= self._progress_anchor + 0.10:
            self._progress_anchor = self._progress
            self._no_progress_frames = 0
        elif self._previous_movement != MovementV1():
            self._no_progress_frames += 1
        if self._no_progress_frames >= 20 and frame.body.is_on_ground:
            self.state = FixedRouteState.NEEDS_REPLAN
            return self._decision(
                started, MovementV1(), "ground_traversal_stalled",
            )
        speed = math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        )
        if self._cancel_requested and frame.body.is_on_ground:
            if speed <= self.config.stopped_speed_blocks_per_second:
                self.state = FixedRouteState.CANCELLED
                return self._decision(
                    started, MovementV1(), "cancelled_after_traversal_stop",
                )
            self.state = FixedRouteState.CANCELLING
            return self._decision(
                started, MovementV1(), "traversal_cancel_release",
            )
        goal = self._geometry.goal
        goal_distance = math.hypot(
            frame.body.position[0] - goal.x,
            frame.body.position[2] - goal.z,
        )
        entry_window = (plan.exit_window if self._continuation is not None
                        else self.config.handoff_entry_window)
        entry_matches = (
            body_fits_segment_entry(
                entry_window, frame.body,
                observed_ground_mode(frame.body),
            )
            if entry_window is not None else None
        )
        completion_speed = (
            entry_window.maximum_speed_blocks_per_second
            if entry_window is not None
            else self.config.handoff_speed_blocks_per_second
            if self.config.handoff_speed_blocks_per_second is not None
            else self.config.stopped_speed_blocks_per_second
        )
        route_complete = (
            self._progress >= self._geometry.total_length
            - self.config.endpoint_tolerance_blocks
        )
        at_goal = (
            frame.body.is_on_ground and route_complete
            and (entry_matches if entry_matches is not None else
                 goal_distance
                 <= self.config.traversal_endpoint_tolerance_blocks)
            and abs(frame.body.position[1] - goal.y) <= 0.10
        )
        if (at_goal and speed <= completion_speed
                and (self._continuation is None or (
                    self._continuation.accepts(state)
                    and self._traversal_movement_safety(frame, state, MovementV1())[0]
                ))):
            self.state = FixedRouteState.SUCCEEDED
            self._progress = self._geometry.total_length
            return self._decision(
                started, MovementV1(), "verified_ground_traversal_complete",
            )
        body = self._planar(frame)
        remaining = max(0.0, self._geometry.total_length - self._progress)
        braking = (
            at_goal
            or (speed > completion_speed
                and remaining <= self._release_distance(body, completion_speed)
                + self.config.endpoint_tolerance_blocks * 0.65)
        )
        lookahead = min(
            self.config.lookahead_max_blocks,
            max(self.config.lookahead_min_blocks,
                self.config.lookahead_min_blocks + speed * 0.18),
        )
        target = (
            (goal.x, goal.z) if braking
            else self._geometry.point_at(self._progress + lookahead)
        )
        if (self._continuation is not None and not braking
                and self._segment_index == len(self._geometry.lengths) - 1
                and remaining <= lookahead + self.config.endpoint_tolerance_blocks):
            window = self._continuation.entry_window
            dx, dz = window.horizontal_approach_direction
            target = (window.reference_point[0] + dx * window.maximum_longitudinal_offset_blocks,
                      window.reference_point[2] + dz * window.maximum_longitudinal_offset_blocks)
        if remaining <= lookahead + self.config.endpoint_tolerance_blocks:
            prepared = self._prepared_handoff_movement(frame)
            if prepared is not None:
                safe, _, _ = self._traversal_movement_safety(frame, state, prepared)
                if safe:
                    self.state = FixedRouteState.RUNNING
                    return self._decision(
                        started, prepared, "tracking_conditional_motion_entry",
                        handoff_disposition=GroundHandoffDisposition.CONSUMED,
                    )
        movement, missing, unsupported = self._verified_traversal_movement(
            frame, state, target, braking=braking,
        )
        if movement is None:
            if missing:
                self.state = FixedRouteState.NEEDS_INFORMATION
                return self._decision(
                    started, MovementV1(),
                    "ground_traversal_requires_information", missing,
                )
            if not frame.body.is_on_ground:
                self._traversal_replan_pending = True
                self.state = FixedRouteState.RUNNING
                return self._decision(
                    started, MovementV1(),
                    "ground_traversal_no_candidate_awaiting_landing",
                )
            self.state = FixedRouteState.NEEDS_REPLAN
            return self._decision(
                started, MovementV1(),
                ("ground_traversal_model_unsupported"
                 if unsupported else "ground_traversal_no_safe_candidate"),
            )
        self.state = (
            FixedRouteState.CANCELLING
            if self._cancel_requested else FixedRouteState.RUNNING
        )
        return self._decision(
            started,
            movement,
            ("braking_verified_ground_traversal"
             if braking else "tracking_verified_ground_traversal"),
        )

    def _verify_ordinary_ground_family(
        self,
        frame: NavigationFrame,
        body: PlanarBodyState,
        physics_state: PhysicsState | None,
        query_cache: WorldQueryCache,
        *,
        deadline_ns: int,
    ) -> GroundCandidateVerificationReport:
        """Produce the one physical ordinary-WALK report for this frame."""
        assert self._route is not None and self._geometry is not None
        goal = self._geometry.goal
        context = GroundTrackingContext(
            body, frame.body.body_box, frame.body.position[1],
            GroundTrackingRoute.from_fixed_route(self._route),
            self._progress, self._segment_index, self._previous_movement,
            (goal.x, goal.z), False, self.profile,
            GroundTrackingLimits.from_fixed_route_config(self.config),
            frame.world, query_cache,
        )
        report = GroundCandidateVerifier().verify(
            frame, physics_state, context,
            tail_ticks=self.config.maximum_recovery_ticks,
            minimum_support=self.config.minimum_support_fraction,
            profile=self.profile,
            query_cache=query_cache,
            deadline_ns=deadline_ns,
        )
        self._declared_candidates = len(report.candidates)
        self._classified_candidates = sum(
            item.status is not GroundCandidateSafety.BUDGET_EXHAUSTED
            for item in report.candidates
        )
        self._candidate_neutral_safe = report.neutral_safe
        self._candidate_budget_exhausted = (
            report.status is GroundCandidateFamilyStatus.BUDGET_EXHAUSTED
        )
        self._physics_steps += report.stats.physics_steps
        return report

    def decide(self, frame: NavigationFrame, *, input_confirmed: bool = True,
               physics_state: PhysicsState | None = None,
               query_cache: WorldQueryCache | None = None,
               state_anchor: StateAnchor | None = None,
               input_ledger: InputApplicationLedger | None = None) -> FixedRouteDecision:
        started = time.perf_counter_ns()
        self._full_candidates = 0
        self._physics_steps = 0
        self._ordinary_replays = {}
        self._closed_replays = {}
        self._closed_route_fits = {}
        self._declared_candidates = 0
        self._classified_candidates = 0
        self._candidate_budget_exhausted = False
        self._candidate_neutral_safe = False
        if type(frame) is not NavigationFrame or type(input_confirmed) is not bool:
            raise ContractViolation("fixed route decision requires a navigation frame and confirmation")
        if ((state_anchor is not None and type(state_anchor) is not StateAnchor)
                or (input_ledger is not None
                    and type(input_ledger) is not InputApplicationLedger)):
            raise ContractViolation("fixed route execution evidence must be typed")
        if self._route is None or self._geometry is None or self._session is None:
            raise ContractViolation("fixed route controller has not started")
        if frame.session != self._session:
            self.state = FixedRouteState.FAILED
            return self._decision(started, MovementV1(), "world_session_changed")
        if self._last_sequence is not None and frame.body.sequence_id <= self._last_sequence:
            raise ContractViolation("fixed route frame did not advance")
        self._last_sequence = frame.body.sequence_id
        movement_tick = frame.body.movement_tick_id
        if (self._active_tail_valid_until_tick is not None
                and movement_tick is not None
                and movement_tick > self._active_tail_valid_until_tick):
            self._active_tail_dependencies = ()
            self._active_tail_valid_until_tick = None
            self._active_route_fit = None
        if self.state in {
            FixedRouteState.CANCELLED, FixedRouteState.SUCCEEDED,
            FixedRouteState.INPUT_LOST, FixedRouteState.FAILED, FixedRouteState.UNSUPPORTED,
        }:
            return self._decision(started, MovementV1(), self.state.value)
        if not input_confirmed and self._ground_terminal_sequence is not None:
            if state_anchor is None:
                self._clear_ground_terminal()
                self.state = FixedRouteState.INPUT_LOST
                return self._decision(
                    started, MovementV1(),
                    "ground_terminal_execution_anchor_unavailable",
                    submit_input=False,
                )
            self._ground_terminal_begin_recovery(
                state_anchor, FixedRouteState.INPUT_LOST,
            )
            self.state = FixedRouteState.CANCELLING
        elif not input_confirmed:
            self.state = FixedRouteState.INPUT_LOST
            return self._decision(started, MovementV1(), "input_application_unconfirmed")
        if (self._ground_terminal_sequence is None
                and set(frame.changed_cells).intersection(
                    self._active_tail_dependencies)):
            self.state = FixedRouteState.NEEDS_REPLAN
            return self._decision(
                started, MovementV1(), "ground_tail_dependencies_changed",
            )
        if self._traversal_plan is not None:
            return self._decide_traversal(frame, started, physics_state)
        if query_cache is None:
            query_cache = WorldQueryCache(frame.world)
        else:
            query_cache.validate_for(frame.world)
        route_level = self._geometry.points[0].y
        current_support = query_support(
            frame.body.body_box, frame.world, query_cache=query_cache,
        )
        if self._guard_phase is not GroundRouteGuardPhase.INACTIVE:
            return self._decide_edge_guard(frame, physics_state, query_cache, started)
        mode_pending = False
        geometric_ground_support = False
        if self.mode_profile is None:
            if frame.body.pose != "standing":
                self.state = FixedRouteState.UNSUPPORTED
                return self._decision(started, MovementV1(), "ordinary_ground_state_lost")
            if not frame.body.is_on_ground:
                geometric_ground_support = (
                    abs(frame.body.position[1] - route_level) <= 0.10
                    and frame.body.velocity_blocks_per_second[1] <= 0.0
                    and current_support.status is QueryStatus.FEASIBLE
                    and current_support.support_fraction
                    >= self.config.preferred_support_fraction
                    and self._geometric_support_frames == 0
                )
                if not geometric_ground_support:
                    self.state = FixedRouteState.UNSUPPORTED
                    return self._decision(
                        started, MovementV1(), "ordinary_ground_state_lost",
                    )
                self._geometric_support_frames = 1
            else:
                self._geometric_support_frames = 0
        else:
            readiness = evaluate_ground_mode(self.mode_profile, frame.body)
            if readiness is ModeReadiness.GROUND_STATE_LOST:
                self.state = FixedRouteState.UNSUPPORTED
                return self._decision(started, MovementV1(), "ground_mode_ground_state_lost")
            if readiness is ModeReadiness.RESOURCE_UNAVAILABLE:
                self.state = FixedRouteState.UNSUPPORTED
                return self._decision(started, MovementV1(), "ground_mode_resource_unavailable")
            if readiness is ModeReadiness.INVALID_ENTRY:
                self.state = FixedRouteState.UNSUPPORTED
                return self._decision(started, MovementV1(), "ground_mode_invalid_entry")
            if readiness is ModeReadiness.PENDING:
                if (self.mode_profile.mode is MovementMode.SPRINT
                        and self.state is FixedRouteState.BRAKING):
                    # Releasing sprint is part of the verified endpoint brake.
                    # Do not accelerate again merely to re-confirm a mode that
                    # this segment is deliberately leaving.
                    pass
                elif self._mode_pending_frames >= self.mode_profile.confirmation_ticks:
                    self.state = FixedRouteState.UNSUPPORTED
                    return self._decision(started, MovementV1(),
                                          "ground_mode_confirmation_timeout")
                else:
                    mode_pending = True
                if mode_pending and not self.mode_profile.request_sprint:
                    if (self.mode_profile.mode is MovementMode.WALK
                            and observed_ground_mode(frame.body) in {
                                MovementMode.CROUCH, MovementMode.CRAWL,
                            }):
                        standing = Aabb(
                            frame.body.body_box.min_x, frame.body.body_box.min_y,
                            frame.body.body_box.min_z, frame.body.body_box.max_x,
                            frame.body.body_box.min_y + 1.8, frame.body.body_box.max_z,
                        )
                        clearance = sweep(
                            standing, (0.0, 0.0, 0.0), frame.world,
                            query_cache=query_cache,
                        )
                        if clearance.status is QueryStatus.NEEDS_INFORMATION:
                            self.state = FixedRouteState.NEEDS_INFORMATION
                            return self._decision(
                                started, MovementV1(), "ground_mode_exit_requires_information",
                                clearance.missing_cells,
                            )
                        if clearance.status is QueryStatus.BLOCKED:
                            self.state = FixedRouteState.BLOCKED
                            return self._decision(
                                started, MovementV1(), "ground_mode_exit_clearance_blocked",
                            )
                        if clearance.status is QueryStatus.UNSUPPORTED:
                            self.state = FixedRouteState.UNSUPPORTED
                            return self._decision(
                                started, MovementV1(), "ground_mode_exit_clearance_unsupported",
                            )
                    self._mode_pending_frames += 1
                    return self._decision(started, MovementV1(),
                                          "ground_mode_confirmation_pending")
            else:
                self._mode_pending_frames = 0
        if abs(frame.body.position[1] - route_level) > 0.10:
            self.state = FixedRouteState.UNSUPPORTED
            return self._decision(started, MovementV1(), "fixed_route_level_mismatch")

        body = self._planar(frame)
        speed = math.hypot(body.velocity_x, body.velocity_z)
        if speed > (self.profile.maximum_speed_blocks_per_second
                    + self.config.speed_model_tolerance_blocks_per_second):
            self.state = FixedRouteState.UNSUPPORTED
            return self._decision(started, MovementV1(), "ordinary_ground_speed_outside_model")
        current_clearance = sweep(
            frame.body.body_box, (0.0, 0.0, 0.0), frame.world,
            query_cache=query_cache,
        )
        if (self.profile.motion_catalog is not None
                and self.profile.ground_model_id is not None
                and unsupported_motion_cells(
                    self.profile.motion_catalog, frame.world,
                    tuple(sorted(set(current_clearance.dependencies
                                     + current_support.dependencies))),
                    self.profile.ground_model_id,
                    query_cache=query_cache,
                )):
            self.state = FixedRouteState.UNSUPPORTED
            return self._decision(started, MovementV1(), "ordinary_ground_motion_trait_unsupported")
        if current_support.status is QueryStatus.UNSUPPORTED:
            self.state = FixedRouteState.UNSUPPORTED
            return self._decision(started, MovementV1(), "ordinary_ground_support_shape_unsupported")
        if (current_support.status is QueryStatus.FEASIBLE
                and (not self.profile.support_materials
                     or not set(current_support.support_materials).issubset(
                         self.profile.support_materials))):
            self.state = FixedRouteState.UNSUPPORTED
            return self._decision(started, MovementV1(), "ordinary_ground_material_unsupported")
        if self._ground_terminal_sequence is not None:
            if state_anchor is None or input_ledger is None:
                self._clear_ground_terminal()
                self.state = FixedRouteState.INPUT_LOST
                return self._decision(
                    started, MovementV1(),
                    "ground_terminal_execution_evidence_unavailable",
                    submit_input=False,
                )
            return self._decide_ground_terminal(
                frame, state_anchor, input_ledger, started,
            )
        if self._ground_terminal_basis is not None and self._cancel_requested:
            self._clear_ground_terminal()
        if self._ground_terminal_basis is not None:
            if (state_anchor is None
                    or physics_state is None
                    or state_anchor.physics_state != physics_state):
                self._clear_ground_terminal()
            else:
                waiting = verified_ground_route_candidate(
                    frame, physics_state, MovementV1(),
                    control_ticks=1,
                    tail_ticks=self.config.maximum_recovery_ticks,
                    minimum_support=self.config.minimum_support_fraction,
                    profile=self.profile,
                    query_cache=query_cache,
                )
                self._physics_steps += waiting.physics_steps
                if waiting.status is QueryStatus.FEASIBLE:
                    self.state = FixedRouteState.RUNNING
                    self._active_tail_dependencies = waiting.dependencies
                    movement_tick = frame.body.movement_tick_id
                    self._active_tail_valid_until_tick = (
                        None if movement_tick is None else
                        movement_tick + max(0, len(waiting.trajectory) - 1)
                    )
                    return self._decision(
                        started, MovementV1(),
                        "awaiting_ground_terminal_sequence",
                        input_lease_ticks=1,
                    )
                self._clear_ground_terminal()
        projection = self._geometry.project(
            body.x, body.z, self._progress, self._segment_index,
            self.config.maximum_cross_track_blocks,
            prefer_next_segment=self._no_progress_frames >= 3,
        )
        self._progress = max(self._progress, projection.progress)
        self._segment_index = max(self._segment_index, projection.segment_index)
        self._cross_track = projection.distance
        if self._cross_track > self.config.maximum_cross_track_blocks:
            self.state = FixedRouteState.NEEDS_REPLAN
            return self._decision(
                started, MovementV1(), "fixed_route_observed_outside_corridor",
            )
        preview_missing = self._route_preview_missing(frame, query_cache)
        if self._progress >= self._progress_anchor + 0.10:
            self._progress_anchor = self._progress
            self._no_progress_frames = 0
        elif self.state is FixedRouteState.RUNNING and self._previous_movement != MovementV1():
            self._no_progress_frames += 1
        if self._no_progress_frames >= 20:
            self._stall_detected = True

        ordinary_report = None
        if ((self.mode_profile is None
                or self.mode_profile.mode is MovementMode.WALK)
                and self.profile.motion_catalog is not None):
            ordinary_report = self._verify_ordinary_ground_family(
                frame, body, physics_state, query_cache,
                deadline_ns=started + 30_000_000,
            )

        if self._cancel_requested:
            if speed <= self.config.stopped_speed_blocks_per_second:
                self.state = FixedRouteState.CANCELLED
                return self._decision(started, MovementV1(), "cancelled_after_stop")
            self.state = FixedRouteState.CANCELLING
            return self._brake(
                frame, body, started, hold_position=True,
                reason="cancel_braking", query_cache=query_cache,
                ordinary_report=ordinary_report,
            )

        goal = self._geometry.goal
        goal_distance = math.hypot(body.x - goal.x, body.z - goal.z)
        goal_support = current_support
        route_complete = (self._geometry.total_length <= _EPSILON
                          or self._progress >= self._geometry.total_length
                             - self.config.endpoint_tolerance_blocks)
        entry_matches = None
        if self.config.handoff_entry_window is not None:
            entry_matches = body_fits_segment_entry(
                self.config.handoff_entry_window,
                frame.body,
                observed_ground_mode(frame.body),
            )
        at_goal = (
            route_complete
            and (entry_matches if entry_matches is not None else (
                goal_distance <= self.config.endpoint_tolerance_blocks
                and abs(frame.body.position[1] - goal.y) <= 0.10
            ))
            and goal_support.status is QueryStatus.FEASIBLE
            and goal_support.support_fraction >= self.config.minimum_support_fraction
        )
        completion = (None if self._route.execution_contract is None else
                      self._route.execution_contract.completion_region)
        completion_speed = (
            self.config.handoff_entry_window.maximum_speed_blocks_per_second
            if self.config.handoff_entry_window is not None
            else self.config.handoff_speed_blocks_per_second
            if self.config.handoff_speed_blocks_per_second is not None
            else self.config.stopped_speed_blocks_per_second
        )
        completion_evaluation = None
        if completion is not None:
            completion_context = GroundTrackingContext(
                body, frame.body.body_box, frame.body.position[1],
                GroundTrackingRoute.from_fixed_route(self._route),
                self._progress, self._segment_index, self._previous_movement,
                (goal.x, goal.z), True, self.profile,
                GroundTrackingLimits.from_fixed_route_config(self.config),
                frame.world, query_cache,
            )
            completion_evaluation = (
                GroundTrackingPolicy.completion_from_report(
                    completion_context, ordinary_report,
                    maximum_speed_blocks_per_second=completion_speed,
                )
                if ordinary_report is not None else
                GroundTrackingPolicy.completion(
                    completion_context,
                    maximum_speed_blocks_per_second=completion_speed,
                )
            )
            at_goal = (
                completion_evaluation.satisfied
                and (entry_matches is not False)
                and goal_support.status is QueryStatus.FEASIBLE
                and goal_support.support_fraction >= self.config.minimum_support_fraction
                and frame.body.is_on_ground
            )
            current_distance = completion_evaluation.current_distance_to_completion_blocks
            stopped_distance = completion_evaluation.predicted_stop_distance_to_completion_blocks
            terminal_improved = (
                current_distance + .005 < self._terminal_distance_anchor
                or stopped_distance + .005 < self._terminal_stop_distance_anchor
                or (speed + .05 < self._terminal_speed_anchor
                    and current_distance <= self._terminal_distance_anchor + .005)
            )
            if terminal_improved:
                self._terminal_distance_anchor = min(
                    self._terminal_distance_anchor, current_distance,
                )
                self._terminal_stop_distance_anchor = min(
                    self._terminal_stop_distance_anchor, stopped_distance,
                )
                self._terminal_speed_anchor = min(self._terminal_speed_anchor, speed)
                self._no_progress_frames = 0
                self._stall_detected = False
        if at_goal and speed <= completion_speed:
            self.state = FixedRouteState.SUCCEEDED
            self._progress = self._geometry.total_length
            return self._decision(
                started, MovementV1(),
                ("goal_reached_for_handoff"
                 if (self.config.handoff_entry_window is not None
                     or self.config.handoff_speed_blocks_per_second is not None)
                 else "goal_reached_and_stopped"),
            )

        if self._stall_detected:
            if speed <= self.config.stopped_speed_blocks_per_second:
                self.state = FixedRouteState.BLOCKED
                return self._decision(started, MovementV1(), "fixed_route_stalled")
            self.state = FixedRouteState.BRAKING
            return self._brake(
                frame, body, started, hold_position=True,
                reason="fixed_route_stall_braking", query_cache=query_cache,
                ordinary_report=ordinary_report,
            )

        stop_distance = self._release_distance(body, completion_speed)
        remaining = max(0.0, self._geometry.total_length - self._progress)
        if (at_goal
                or (completion is None and speed > completion_speed
                    and remaining <= stop_distance
                    + self.config.endpoint_tolerance_blocks * 0.65)):
            self.state = FixedRouteState.BRAKING
            return self._brake(
                frame, body, started, hold_position=False,
                reason="goal_braking", query_cache=query_cache, physics_state=physics_state,
                ordinary_report=ordinary_report,
            )

        corner_distance=self._geometry.next_sharp_corner_distance(
            self._progress,self._segment_index)
        if (corner_distance is not None
                and corner_distance<=self.config.corner_braking_lookahead_blocks
                and speed>self.config.corner_speed_blocks_per_second):
            corner_target=self._geometry.point_at(
                min(self._geometry.total_length,self._progress+self.config.lookahead_min_blocks))
            if ordinary_report is not None:
                corner_context = GroundTrackingContext(
                    body, frame.body.body_box, frame.body.position[1],
                    GroundTrackingRoute.from_fixed_route(self._route),
                    self._progress, self._segment_index, self._previous_movement,
                    corner_target, True, self.profile,
                    GroundTrackingLimits.from_fixed_route_config(self.config),
                    frame.world, query_cache,
                )
                neutral_options = [
                    value for value in GroundTrackingPolicy.verified_candidates(
                        corner_context, ordinary_report,
                    ) if value.movement == MovementV1()
                ]
                quality = min(
                    neutral_options,
                    key=GroundTrackingPolicy._candidate_key,
                )
                neutral = _Candidate(
                    quality.movement, quality.score, quality.missing_cells,
                    quality.blocked, quality.unsupported, quality.progress_gain,
                    control_ticks=quality.control_ticks,
                )
            else:
                neutral=self._evaluate_candidate(
                    frame, body, MovementV1(), corner_target, braking=True,
                    query_cache=query_cache,
                )
            if not neutral.blocked and not neutral.unsupported and not neutral.missing:
                self.state=FixedRouteState.RUNNING
                return self._decision(started,MovementV1(),"corner_speed_control",preview_missing)

        lookahead = min(
            self.config.lookahead_max_blocks,
            max(self.config.lookahead_min_blocks, self.config.lookahead_min_blocks + speed * 0.18),
        )
        target = self._geometry.point_at(self._progress + lookahead)
        if (self._handoff_target_hint is not None
                and remaining <= lookahead + self.config.endpoint_tolerance_blocks):
            hint = self._handoff_target_hint
            movement = self._prepared_handoff_movement(frame)
            if movement is not None:
                target = (hint.position[0], hint.position[2])
                # Admission separately verifies the applied prefix. This proposal
                # must preserve its original projection and full release tail.
                if ordinary_report is not None:
                    handoff_context = GroundTrackingContext(
                        body, frame.body.body_box, frame.body.position[1],
                        GroundTrackingRoute.from_fixed_route(self._route),
                        self._progress, self._segment_index,
                        self._previous_movement, target, False, self.profile,
                        GroundTrackingLimits.from_fixed_route_config(self.config),
                        frame.world, query_cache,
                    )
                    choices = [
                        item for item in GroundTrackingPolicy.verified_candidates(
                            handoff_context, ordinary_report,
                        ) if item.movement == movement
                    ]
                    quality = min(choices, key=GroundTrackingPolicy._candidate_key)
                    preferred = _Candidate(
                        quality.movement, quality.score, quality.missing_cells,
                        quality.blocked, quality.unsupported,
                        quality.progress_gain, control_ticks=quality.control_ticks,
                    )
                else:
                    preferred = self._evaluate_candidate(
                        frame, body, movement, target, braking=False,
                        query_cache=query_cache,
                    )
                if (not preferred.blocked and not preferred.unsupported
                        and not preferred.missing):
                    self.state = FixedRouteState.RUNNING
                    return self._decision(
                        started, movement, "tracking_conditional_motion_entry",
                        handoff_disposition=GroundHandoffDisposition.CONSUMED,
                    )
        if (mode_pending and self.mode_profile is not None
                and self.mode_profile.mode is MovementMode.SPRINT):
            candidates = [self._evaluate_candidate(
                              frame, body, movement, target, braking=False,
                              query_cache=query_cache,
                          )
                          for movement in _MOVEMENTS]
        else:
            if ordinary_report is not None:
                tracking_context = GroundTrackingContext(
                    body, frame.body.body_box, frame.body.position[1],
                    GroundTrackingRoute.from_fixed_route(self._route),
                    self._progress, self._segment_index, self._previous_movement,
                    target, False, self.profile,
                    GroundTrackingLimits.from_fixed_route_config(self.config),
                    frame.world, query_cache,
                )
                candidates = self._verified_ordinary_candidates(
                    ordinary_report, tracking_context,
                )
            else:
                candidates = self._ranked_tracking_candidates(
                    frame, body, target, query_cache,
                )
                candidates = self._legacy_component_replay_candidates(
                    candidates, frame, body, target, physics_state, query_cache,
                    deadline_ns=started + 30_000_000,
                )
            if self._candidate_budget_exhausted:
                if self._candidate_neutral_safe:
                    self.state = FixedRouteState.RUNNING
                    return self._decision(
                        started, MovementV1(),
                        "ground_candidate_budget_exhausted_neutral",
                        (*preview_missing,), input_lease_ticks=1,
                    )
                self.state = FixedRouteState.NEEDS_REPLAN
                return self._decision(
                    started, MovementV1(),
                    "ground_candidate_budget_exhausted_unproven",
                    (*preview_missing,), input_lease_ticks=1,
                    submit_input=False,
                )
        feasible = [candidate for candidate in candidates
                    if not candidate.blocked and not candidate.unsupported and not candidate.missing]
        if feasible:
            if (mode_pending and self.mode_profile is not None
                    and self.mode_profile.mode is MovementMode.SPRINT):
                activation = [candidate for candidate in feasible
                              if candidate.movement.forward > 0
                              and candidate.progress_gain > .005]
                if not activation:
                    self.state = FixedRouteState.UNSUPPORTED
                    return self._decision(
                        started, MovementV1(), "sprint_requires_forward_alignment",
                    )
                selected = min(activation, key=lambda candidate: (
                    candidate.score, candidate.movement.strafe,
                ))
                self._mode_pending_frames += 1
            else:
                selected = min(feasible, key=self._candidate_key)
                # A declared upcoming interval must remain reachable by safe
                # ordinary input. Neutral's support preference may otherwise
                # stop before that entry despite a proved advancing candidate.
                if selected.progress_gain <= .005 and self._route.execution_contract is not None:
                    entering = [c for c in feasible if c.progress_gain > .005
                                and (self._candidate_enters_guard_interval(c, body, target)
                                     or (completion is not None and not at_goal
                                         and self._candidate_approaches_completion(c, body, target)))]
                    if entering:
                        selected = min(entering, key=self._candidate_key)
            deferred = [candidate for candidate in candidates
                        if candidate.missing and candidate.progress_gain > selected.progress_gain + 0.005]
            if selected.progress_gain <= 0.005 and deferred:
                self.state = FixedRouteState.NEEDS_INFORMATION
                missing = tuple(cell for candidate in deferred for cell in candidate.missing)
                return self._decision(started, MovementV1(),
                                      "forward_candidate_requires_information",
                                      (*missing, *preview_missing))
            if (selected.movement == MovementV1()
                    and selected.progress_gain <= 0.005
                    and speed <= self.config.stopped_speed_blocks_per_second):
                guard = self._enter_edge_guard(candidates, frame, body, target,
                                               physics_state, query_cache, started)
                if guard is not None:
                    return guard
                if any(candidate.unsupported for candidate in candidates):
                    self.state = FixedRouteState.UNSUPPORTED
                    return self._decision(
                        started, MovementV1(), "forward_ground_material_unsupported",
                    )
                self.state = FixedRouteState.BLOCKED
                return self._decision(started, MovementV1(), "fixed_route_has_no_forward_control")
            self.state = FixedRouteState.RUNNING
            self._hold_selected_tail_proof(frame, selected)
            return self._decision(started, selected.movement,
                                  ("ground_mode_confirmation_pending" if mode_pending
                                   else ("tracking_fixed_route_geometric_support"
                                         if geometric_ground_support
                                         else "tracking_fixed_route")),
                                  preview_missing,
                                  input_lease_ticks=(
                                      selected.control_ticks
                                      if selected.control_ticks else None
                                  ))
        guard = self._enter_edge_guard(candidates, frame, body, target,
                                       physics_state, query_cache, started)
        if guard is not None:
            return guard
        missing = tuple(cell for candidate in candidates for cell in candidate.missing)
        if missing:
            self.state = FixedRouteState.NEEDS_INFORMATION
            return self._decision(started, MovementV1(), "local_motion_requires_information", missing)
        if any(candidate.unsupported for candidate in candidates):
            self.state = FixedRouteState.UNSUPPORTED
            return self._decision(started, MovementV1(), "local_geometry_unsupported")
        self.state = FixedRouteState.BLOCKED
        return self._decision(started, MovementV1(), "no_safe_ground_candidate")

    def _route_preview_missing(
        self, frame: NavigationFrame, query_cache: WorldQueryCache,
    ) -> tuple[BlockPos, ...]:
        """Ask for near-future headroom while the already-proven prefix keeps moving."""
        assert self._geometry is not None
        target_x, target_z = self._geometry.point_at(self._progress + 2.0)
        result = sweep(
            frame.body.body_box,
            (target_x - frame.body.position[0], 0.0,
             target_z - frame.body.position[2]),
            frame.world,
            query_cache=query_cache,
        )
        return result.missing_cells

    def _release_distance(self, body: PlanarBodyState,
                          target_speed_blocks_per_second: float) -> float:
        state = body
        distance = 0.0
        neutral = GroundControl(0, 0, body.yaw_radians)
        for _ in range(self.config.maximum_recovery_ticks):
            if math.hypot(state.velocity_x, state.velocity_z) <= target_speed_blocks_per_second:
                break
            next_state = predict_ground(state, (neutral,), self.profile)[-1]
            distance += math.hypot(next_state.x - state.x, next_state.z - state.z)
            state = next_state
        return distance

    @staticmethod
    def _candidate_key(candidate: _Candidate) -> tuple:
        if math.isfinite(candidate.completion_distance_blocks):
            return (
                candidate.completion_distance_blocks,
                candidate.target_distance_blocks,
                candidate.score,
                candidate.control_ticks,
                candidate.movement.forward,
                candidate.movement.strafe,
            )
        return (
            candidate.score,
            candidate.movement.forward,
            candidate.movement.strafe,
        )

    def _legacy_component_replay_candidates(
        self, candidates: list[_Candidate], frame: NavigationFrame,
        body: PlanarBodyState, target: tuple[float, float],
        physics_state: PhysicsState | None, query_cache: WorldQueryCache,
        *, deadline_ns: int | None = None,
    ) -> list[_Candidate]:
        if (not ground_route_state_matches(frame, physics_state)
                or (self.mode_profile is not None
                    and self.mode_profile.mode is not MovementMode.WALK)):
            return candidates
        assert self._geometry is not None
        if not any(
                candidate.full_replay_eligible
                and not candidate.missing and not candidate.unsupported
                for candidate in candidates):
            return candidates
        assert self._route is not None
        completion = (None if self._route.execution_contract is None else
                      self._route.execution_contract.completion_region)
        context = GroundTrackingContext(
            body, frame.body.body_box, frame.body.position[1],
            GroundTrackingRoute.from_fixed_route(self._route),
            self._progress, self._segment_index, self._previous_movement,
            target, False, self.profile,
            GroundTrackingLimits.from_fixed_route_config(self.config),
            frame.world, query_cache,
        )
        quality = tuple(
            GroundTrackingPolicy.safe_tail(context, movement, control_ticks)
            for movement in CLOSED_GROUND_MOVEMENTS for control_ticks in (1, 2)
        )
        quality_by_key = {
            (item.movement, item.control_ticks): item for item in quality
        }
        report = GroundCandidateVerifier().verify(
            frame, physics_state, context,
            tail_ticks=self.config.maximum_recovery_ticks,
            minimum_support=self.config.minimum_support_fraction,
            profile=self.profile, query_cache=query_cache,
            deadline_ns=deadline_ns,
        )
        self._declared_candidates = len(report.candidates)
        self._classified_candidates = sum(
            item.status is not GroundCandidateSafety.BUDGET_EXHAUSTED
            for item in report.candidates
        )
        self._candidate_neutral_safe = report.neutral_safe
        self._candidate_budget_exhausted = (
            report.status is GroundCandidateFamilyStatus.BUDGET_EXHAUSTED
        )
        self._physics_steps += report.stats.physics_steps
        if self._candidate_budget_exhausted:
            return candidates

        evaluated = []
        for proof in report.candidates:
            key = (proof.movement, proof.control_ticks)
            policy = quality_by_key[key]
            result = proof.result
            self._closed_route_fits[key] = proof.route_fit
            if result is not None:
                # A support-boundary rejection is still the typed evidence that
                # permits the separately proved sneak edge guard. It never makes
                # the ordinary command eligible.
                self._closed_replays[key] = result
            if not proof.eligible or result is None:
                evaluated.append(_Candidate(
                    proof.movement, math.inf, proof.missing_cells,
                    (proof.status is GroundCandidateSafety.UNSAFE
                     or (proof.status is GroundCandidateSafety.SAFE
                         and not proof.route_fit.control_prefix_inside)),
                    proof.status is GroundCandidateSafety.UNSUPPORTED,
                    -math.inf, False,
                    bool(result is not None and result.support_boundary_rejected),
                    proof.control_ticks,
                    policy.completion_distance_blocks,
                    policy.target_distance_blocks,
                ))
                continue
            self._ordinary_replays[proof.movement] = result
            if result.tracking_end is None:
                evaluated.append(_Candidate(
                    proof.movement, math.inf, (), True, False, -math.inf,
                    control_ticks=proof.control_ticks,
                ))
                continue
            projections = tuple(self._geometry.project(
                state.position[0], state.position[2], self._progress,
                self._segment_index, self.config.maximum_cross_track_blocks,
            ) for state in result.trajectory)
            # Contact must still advance on each leased tick. A head-on wall
            # cannot buy another lease by approaching it on only the first tick.
            if any(state.horizontal_collision and after.progress <= before.progress + _EPSILON
                   and not (completion is not None and completion.contains(state.position))
                   for state, before, after in zip(result.trajectory[1:proof.control_ticks+1],
                                                    projections, projections[1:])):
                evaluated.append(_Candidate(
                    proof.movement, math.inf, (), True, False, -math.inf,
                    control_ticks=proof.control_ticks,
                ))
                continue
            end = result.tracking_end
            projection = projections[proof.control_ticks]
            gain = projection.progress - self._progress
            region_gain = (0. if completion is None else
                           self._completion_distance(body.x, body.z)
                           - self._completion_distance(end.position[0], end.position[2]))
            enters_region = completion is not None and completion.contains(end.position)
            if gain <= .005 and region_gain <= .005 and not enters_region:
                evaluated.append(_Candidate(
                    proof.movement, math.inf, (), True, False, -math.inf,
                    control_ticks=proof.control_ticks,
                ))
                continue
            gain = max(gain, region_gain, .006 if enters_region else 0.)
            input_switch = (proof.movement.forward != self._previous_movement.forward
                            or proof.movement.strafe != self._previous_movement.strafe)
            score = (math.hypot(end.position[0] - target[0], end.position[2] - target[1]) * 2
                     + projection.distance * 5 - gain * 7 + (.04 if input_switch else 0)
                     + max(0., self.config.preferred_support_fraction - result.minimum_support)**2 * 10)
            stopped = result.trajectory[-1]
            completion_distance = (
                math.inf if completion is None else
                self._completion_distance(stopped.position[0], stopped.position[2])
            )
            evaluated.append(_Candidate(
                proof.movement, score, (), False, False, gain,
                control_ticks=proof.control_ticks,
                completion_distance_blocks=completion_distance,
                target_distance_blocks=math.hypot(
                    stopped.position[0]-target[0], stopped.position[2]-target[1],
                ),
            ))
        return evaluated

    def _verified_ordinary_candidates(
        self,
        report: GroundCandidateVerificationReport,
        context: GroundTrackingContext,
    ) -> list[_Candidate]:
        """Adapt the single formal report to the existing typed controller view."""
        if report.status is GroundCandidateFamilyStatus.BUDGET_EXHAUSTED:
            return []
        quality = GroundTrackingPolicy.verified_candidates(context, report)
        evaluated = []
        for item in quality:
            key = (item.movement, item.control_ticks)
            result = item.verification_result
            if result is not None:
                self._closed_replays[key] = result
            self._closed_route_fits[key] = GroundRouteFit(
                item.control_prefix_inside,
                item.neutral_tail_inside,
                item.maximum_control_prefix_deviation_blocks,
                item.maximum_neutral_tail_deviation_blocks,
            )
            evaluated.append(_Candidate(
                item.movement,
                item.score,
                item.missing_cells,
                item.blocked,
                item.unsupported,
                item.progress_gain,
                False,
                item.support_boundary,
                item.control_ticks,
                item.completion_distance_blocks,
                item.target_distance_blocks,
            ))
        return evaluated

    def _hold_selected_tail_proof(
        self,
        frame: NavigationFrame,
        candidate: _Candidate,
    ) -> None:
        """Hold one winning proof until observation or its stop horizon replaces it."""
        key = (candidate.movement, candidate.control_ticks)
        proof = self._closed_replays.get(key)
        route_fit = self._closed_route_fits.get(key)
        movement_tick = frame.body.movement_tick_id
        if (proof is None or route_fit is None or movement_tick is None
                or proof.tracking_end is None):
            return
        self._active_tail_dependencies = proof.dependencies
        self._active_tail_valid_until_tick = (
            movement_tick + max(0, len(proof.trajectory) - 1)
        )
        self._active_route_fit = route_fit

    def _edge_guard_permitted(self, progress: float) -> bool:
        contract = self._route.execution_contract if self._route is not None else None
        return contract is not None and contract.permits(
            GroundRouteCapability.SNEAK_EDGE_GUARD, progress)

    def _candidate_enters_guard_interval(
        self, candidate: _Candidate, body: PlanarBodyState, target: tuple[float, float],
    ) -> bool:
        replay = self._closed_replays.get((candidate.movement, candidate.control_ticks))
        if replay is None:
            replay = self._ordinary_replays.get(candidate.movement)
        if replay is not None and replay.tracking_end is not None:
            end = replay.tracking_end.position
            projection = self._geometry.project_current(end[0], end[2])
        else:
            end = self._prepare_candidate_rollout(body, candidate.movement, target, braking=False).tracking_end
            projection = self._geometry.project_current(end.x, end.z)
        return self._edge_guard_permitted(projection.progress)

    def _candidate_approaches_completion(self, candidate, body, target) -> bool:
        replay = self._closed_replays.get((candidate.movement, candidate.control_ticks))
        if replay is None:
            replay = self._ordinary_replays.get(candidate.movement)
        if replay is not None and replay.tracking_end is not None:
            x, _, z = replay.tracking_end.position
        else:
            end = self._prepare_candidate_rollout(body, candidate.movement, target,
                                                 braking=False).tracking_end
            x, z = end.x, end.z
        return self._completion_distance(x, z)+_EPSILON < self._completion_distance(body.x, body.z)

    def _completion_distance(self, x: float, z: float) -> float:
        b = self._route.execution_contract.completion_region.bounds
        return math.hypot(max(b.min_x-x, x-b.max_x, 0.),
                          max(b.min_z-z, z-b.max_z, 0.))

    def _guard_replay(
        self, frame: NavigationFrame, physics_state: PhysicsState | None,
        movement: MovementV1, query_cache: WorldQueryCache,
    ) -> tuple[VerifiedGroundRouteCandidate, tuple[_Projection, ...]] | None:
        if self._full_candidates >= 3:
            return None
        self._full_candidates += 1
        result = verified_ground_route_candidate(
            frame, physics_state, movement, control_ticks=self.config.input_lease_ticks,
            tail_ticks=self.config.maximum_recovery_ticks,
            minimum_support=self.config.minimum_support_fraction,
            profile=self.profile, query_cache=query_cache, edge_guard=True,
        )
        self._physics_steps += result.physics_steps
        if result.status is not QueryStatus.FEASIBLE or result.tracking_end is None:
            return None
        projections = [self._geometry.project_current(s.position[0], s.position[2])
                       for s in result.trajectory]
        if any(p.distance > self.config.maximum_cross_track_blocks for p in projections):
            return None
        if movement.sneak:
            if (self._guard_phase is GroundRouteGuardPhase.OUTSIDE_STOPPING
                    and movement == MovementV1(sneak=True)):
                # This is a bounded protective stop, never route tracking.
                if any(p.progress > projections[0].progress + _EPSILON for p in projections):
                    return None
            elif any(not self._edge_guard_permitted(p.progress) for p in projections):
                return None
        return result, tuple(projections)

    def _enter_edge_guard(
        self, candidates: list[_Candidate], frame: NavigationFrame, body: PlanarBodyState,
        target: tuple[float, float], physics_state: PhysicsState | None,
        query_cache: WorldQueryCache, started: int,
    ) -> FixedRouteDecision | None:
        if (not self._edge_guard_permitted(self._geometry.project_current(body.x, body.z).progress)
                or not ground_route_state_matches(frame, physics_state)):
            return None
        # Only an actual support-boundary rejection admits the alternate input.
        rejected = sorted((
            (c, self._prepare_candidate_rollout(
                body, c.movement, target, braking=False,
            ))
            for c in candidates if c.support_boundary
            and not c.missing and not c.unsupported
        ), key=lambda item: self._rollout_key(item[1]))
        for candidate, rollout in rejected:
            ordinary = self._closed_replays.get(
                (candidate.movement, candidate.control_ticks),
            )
            if ordinary is None:
                ordinary = self._ordinary_replays.get(rollout.movement)
            if ordinary is None or not ordinary.support_boundary_rejected:
                continue
            movement = replace(rollout.movement, sneak=True)
            reviewed = self._guard_replay(frame, physics_state, movement, query_cache)
            if reviewed is None:
                continue
            result, projections = reviewed
            gain = projections[self.config.input_lease_ticks].progress - self._progress
            if gain <= .005 and not self._guard_goal_progress(frame, result.tracking_end):
                continue
            # The ordinary full lease/tail failed support; safe reduced motion
            # is equivalent protection even before a calculator clip occurs.
            self._guard_phase = GroundRouteGuardPhase.ACTIVE
            self.state = FixedRouteState.RUNNING
            return self._decision(started, movement, "tracking_declared_edge_guard")
        return None

    def _guard_goal_progress(self, frame: NavigationFrame, end: PhysicsState) -> bool:
        goal = self._geometry.goal
        return (self._progress >= self._geometry.total_length - self.config.endpoint_tolerance_blocks
                and math.hypot(end.position[0]-goal.x, end.position[2]-goal.z) + .005
                < math.hypot(frame.body.position[0]-goal.x, frame.body.position[2]-goal.z))

    def _decide_edge_guard(
        self, frame: NavigationFrame, physics_state: PhysicsState | None,
        query_cache: WorldQueryCache, started: int,
    ) -> FixedRouteDecision:
        if not ground_route_state_matches(frame, physics_state, edge_guard=True):
            self.state = FixedRouteState.UNSUPPORTED
            return self._decision(started, MovementV1(), "edge_guard_state_unavailable")
        body = self._planar(frame)
        projection = self._geometry.project(body.x, body.z, self._progress,
                                            self._segment_index, self.config.maximum_cross_track_blocks)
        self._progress = max(self._progress, projection.progress)
        self._segment_index = max(self._segment_index, projection.segment_index)
        actual = self._geometry.project_current(body.x, body.z)
        self._cross_track = actual.distance
        speed = math.hypot(body.velocity_x, body.velocity_z)
        if (self._cross_track > self.config.maximum_cross_track_blocks
                or abs(frame.body.position[1] - self._geometry.points[0].y) > .10
                or speed > self.profile.maximum_speed_blocks_per_second
                    + self.config.speed_model_tolerance_blocks_per_second):
            self.state = FixedRouteState.UNSUPPORTED
            return self._decision(started, MovementV1(), "edge_guard_body_outside_route_model")
        if (self._guard_phase in {GroundRouteGuardPhase.OUTSIDE_STOPPING,
                                  GroundRouteGuardPhase.OUTSIDE_RELEASING}
                or not self._edge_guard_permitted(actual.progress)):
            return self._stop_outside_guard_interval(frame, physics_state, query_cache, started, speed)
        goal = self._geometry.goal
        at_goal = (self._progress >= self._geometry.total_length - self.config.endpoint_tolerance_blocks
                   and math.hypot(body.x-goal.x, body.z-goal.z) <= self.config.endpoint_tolerance_blocks)
        completion = self._route.execution_contract.completion_region
        if completion is not None:
            at_goal = completion.contains(frame.body.position)
        if self._guard_phase is GroundRouteGuardPhase.RELEASING:
            if not frame.body.is_sneaking and frame.body.pose == "standing":
                self._guard_phase = GroundRouteGuardPhase.INACTIVE
                # Resume the existing completion/cancel path on the next frame.
                return self._decision(started, MovementV1(), "edge_guard_release_confirmed")
            self._guard_release_frames += 1
            if self._guard_release_frames >= self.config.maximum_recovery_ticks:
                self.state = FixedRouteState.BLOCKED
                return self._decision(started, MovementV1(), "edge_guard_standing_clearance_blocked")
            return self._decision(started, MovementV1(), "edge_guard_release_pending")
        must_stop = (self._cancel_requested or at_goal
                     or self._guard_phase is GroundRouteGuardPhase.BRAKING
                     or not self._edge_guard_permitted(actual.progress))
        if must_stop:
            self._guard_phase = GroundRouteGuardPhase.BRAKING
            self.state = FixedRouteState.CANCELLING if self._cancel_requested else FixedRouteState.BRAKING
            if speed <= 1.e-9:
                released = self._guard_replay(frame, physics_state, MovementV1(), query_cache)
                if (released is not None and released[0].trajectory[-1].pose == "standing"
                        and not released[0].trajectory[-1].sneaking):
                    self._guard_phase = GroundRouteGuardPhase.RELEASING
                    self._guard_release_frames = 0
                    return self._decision(started, MovementV1(), "edge_guard_release_requested")
                self.state = FixedRouteState.BLOCKED
                return self._decision(started, MovementV1(), "edge_guard_release_unsafe")
            held = self._guard_replay(frame, physics_state, MovementV1(sneak=True), query_cache)
            if held is not None:
                return self._decision(started, MovementV1(sneak=True), "edge_guard_stopping")
            self.state = FixedRouteState.BLOCKED
            return self._decision(started, MovementV1(), "edge_guard_stop_unsafe")
        target = self._geometry.point_at(self._progress + self.config.lookahead_min_blocks)
        held = self._guard_replay(frame, physics_state, MovementV1(sneak=True), query_cache)
        ordered = sorted((self._prepare_candidate_rollout(body, m, target, braking=False)
                          for m in _MOVEMENTS if m != MovementV1()), key=self._rollout_key)
        for rollout in ordered:
            movement = replace(rollout.movement, sneak=True)
            reviewed = self._guard_replay(frame, physics_state, movement, query_cache)
            if reviewed is None:
                continue
            result, projections = reviewed
            if (projections[self.config.input_lease_ticks].progress <= self._progress + .005
                    and not self._guard_goal_progress(frame, result.tracking_end)):
                continue
            self.state = FixedRouteState.RUNNING
            return self._decision(started, movement, "tracking_declared_edge_guard")
        # Settle inside the interval before giving ordinary control another try.
        self._guard_phase = GroundRouteGuardPhase.BRAKING
        self.state = FixedRouteState.BRAKING if held is not None else FixedRouteState.BLOCKED
        return self._decision(started, MovementV1(sneak=held is not None), "edge_guard_interval_stop")

    def _stop_outside_guard_interval(
        self, frame: NavigationFrame, physics_state: PhysicsState,
        query_cache: WorldQueryCache, started: int, speed: float,
    ) -> FixedRouteDecision:
        self._guard_outside_frames += 1
        self.state = FixedRouteState.CANCELLING if self._cancel_requested else FixedRouteState.BRAKING
        if self._guard_phase is GroundRouteGuardPhase.OUTSIDE_RELEASING:
            if not frame.body.is_sneaking and frame.body.pose == "standing" and speed <= 1.e-9:
                self.state = FixedRouteState.CANCELLED if self._cancel_requested else FixedRouteState.NEEDS_REPLAN
                return self._decision(started, MovementV1(), "edge_guard_outside_release_confirmed")
            if self._guard_outside_frames >= self.config.maximum_recovery_ticks:
                self.state = FixedRouteState.NEEDS_REPLAN
            return self._decision(started, MovementV1(), "edge_guard_outside_release_pending")
        released = self._guard_replay(frame, physics_state, MovementV1(), query_cache)
        if (released is not None and released[0].trajectory[-1].pose == "standing"
                and not released[0].trajectory[-1].sneaking):
            self._guard_phase = GroundRouteGuardPhase.OUTSIDE_RELEASING
            return self._decision(started, MovementV1(), "edge_guard_outside_release_requested")
        self._guard_phase = GroundRouteGuardPhase.OUTSIDE_STOPPING
        held = self._guard_replay(frame, physics_state, MovementV1(sneak=True), query_cache)
        if held is None:
            self.state = FixedRouteState.NEEDS_REPLAN
            return self._decision(started, MovementV1(), "edge_guard_outside_stop_unproven")
        if self._guard_outside_frames >= self.config.maximum_recovery_ticks:
            self.state = FixedRouteState.NEEDS_REPLAN
        return self._decision(started, MovementV1(sneak=True), "edge_guard_outside_protective_stop")

    @staticmethod
    def _rollout_key(rollout: _CandidateRollout) -> tuple[float, int, int]:
        return (
            rollout.base_score,
            rollout.movement.forward,
            rollout.movement.strafe,
        )

    def _ranked_tracking_candidates(
        self,
        frame: NavigationFrame,
        body: PlanarBodyState,
        target: tuple[float, float],
        query_cache: WorldQueryCache,
    ) -> list[_Candidate]:
        """Validate only candidates that can still beat the proven safe winner.

        Geometry penalties are nonnegative.  The kinematic score is therefore a
        lower bound on the final score.  Candidates whose lower bound is already
        worse than a feasible result cannot change the selected control.  Low-
        progress and no-feasible cases still evaluate the remaining candidates
        needed for the existing information and failure classifications.
        """
        if self._continuation is None:
            assert self._route is not None
            context = GroundTrackingContext(
                body, frame.body.body_box, frame.body.position[1],
                GroundTrackingRoute.from_fixed_route(self._route),
                self._progress, self._segment_index, self._previous_movement,
                target, False, self.profile,
                GroundTrackingLimits.from_fixed_route_config(self.config),
                frame.world, query_cache,
            )
            return [
                _Candidate(
                    item.movement, item.score, item.missing_cells,
                    item.blocked, item.unsupported, item.progress_gain,
                    item.full_replay_eligible, item.support_boundary,
                    item.control_ticks, item.completion_distance_blocks,
                    item.target_distance_blocks,
                )
                for item in GroundTrackingPolicy.candidates(context)
            ]
        rollouts = tuple(
            self._prepare_candidate_rollout(body, movement, target, braking=False)
            for movement in _MOVEMENTS
        )
        ordered = tuple(sorted(rollouts, key=self._rollout_key))
        evaluated: dict[tuple[int, int], _Candidate] = {}

        def evaluate(rollout: _CandidateRollout) -> _Candidate:
            key = (rollout.movement.forward, rollout.movement.strafe)
            candidate = evaluated.get(key)
            if candidate is None:
                candidate = self._evaluate_candidate(
                    frame, body, rollout.movement, target, braking=False,
                    query_cache=query_cache, rollout=rollout,
                )
                evaluated[key] = candidate
            return candidate

        selected: _Candidate | None = None
        for rollout in ordered:
            if (selected is not None
                    and self._rollout_key(rollout) >= self._candidate_key(selected)):
                break
            candidate = evaluate(rollout)
            if (not candidate.blocked and not candidate.unsupported
                    and not candidate.missing
                    and (selected is None
                         or self._candidate_key(candidate) < self._candidate_key(selected))):
                selected = candidate

        if selected is None:
            for rollout in ordered:
                evaluate(rollout)
        else:
            if selected.progress_gain <= 0.005:
                for rollout in ordered:
                    if rollout.progress_gain > selected.progress_gain + 0.005:
                        evaluate(rollout)
            if (selected.movement == MovementV1()
                    and selected.progress_gain <= 0.005
                    and math.hypot(body.velocity_x, body.velocity_z)
                    <= self.config.stopped_speed_blocks_per_second):
                for rollout in ordered:
                    evaluate(rollout)

        return [
            evaluated[(movement.forward, movement.strafe)]
            for movement in _MOVEMENTS
            if (movement.forward, movement.strafe) in evaluated
        ]

    def _brake(self, frame: NavigationFrame, body: PlanarBodyState, started: int,
               *, hold_position: bool, reason: str,
               query_cache: WorldQueryCache,
               physics_state: PhysicsState | None = None,
               ordinary_report: GroundCandidateVerificationReport | None = None,
               ) -> FixedRouteDecision:
        target = (body.x, body.z) if hold_position else (
            self._geometry.goal.x, self._geometry.goal.z  # type: ignore[union-attr]
        )
        if not hold_position and self.config.handoff_entry_window is not None:
            window = self.config.handoff_entry_window
            dx, dz = window.horizontal_approach_direction
            offset_x = body.x - window.reference_point[0]
            offset_z = body.z - window.reference_point[2]
            lateral = max(
                -window.maximum_lateral_offset_blocks,
                min(window.maximum_lateral_offset_blocks,
                    -offset_x * dz + offset_z * dx),
            )
            longitudinal = min(
                0.0,
                (window.minimum_longitudinal_offset_blocks
                 + window.maximum_longitudinal_offset_blocks) * 0.5,
            )
            # The next action accepts a corridor, not one exact centre point.
            # Preserve an already-valid lateral lane while braking along the
            # approach direction, otherwise tiny centring corrections create
            # sideways velocity and can prevent the handoff forever.
            target = (
                window.reference_point[0] + dx * longitudinal - dz * lateral,
                window.reference_point[2] + dz * longitudinal + dx * lateral,
            )
        if ordinary_report is not None:
            assert self._route is not None
            braking_context = GroundTrackingContext(
                body, frame.body.body_box, frame.body.position[1],
                GroundTrackingRoute.from_fixed_route(self._route),
                self._progress, self._segment_index, self._previous_movement,
                target, True, self.profile,
                GroundTrackingLimits.from_fixed_route_config(self.config),
                frame.world, query_cache,
            )
            candidates = self._verified_ordinary_candidates(
                ordinary_report, braking_context,
            )
        else:
            candidates = self._ranked_braking_candidates(
                frame, body, target, query_cache,
            )
        completion = (None if self._route.execution_contract is None else
                      self._route.execution_contract.completion_region)
        neutral = next((c for c in candidates if c.movement == MovementV1()), None)
        if not hold_position and completion is not None:
            if neutral is None and ordinary_report is None:
                neutral = self._evaluate_candidate(frame, body, MovementV1(), target,
                    braking=True, query_cache=query_cache)
            proof = (None if neutral is None else self._closed_replays.get(
                (neutral.movement, neutral.control_ticks),
            ))
            stopped = (
                tuple(self._prepare_candidate_rollout(
                    body, MovementV1(), target, braking=True,
                ).states)
                if proof is None else
                tuple(GroundTrackingPolicy._planar_physics_state(value)
                      for value in proof.trajectory)
            )
            if (neutral is not None and not neutral.blocked
                    and not neutral.unsupported and not neutral.missing
                    and all(completion.contains((s.x, frame.body.position[1], s.z))
                            for s in stopped)):
                return self._decision(started, MovementV1(), reason)
        if (ordinary_report is None and not hold_position
                and completion is not None and neutral is not None
                and neutral.full_replay_eligible and not neutral.missing and not neutral.unsupported
                and self._full_candidates < 3 and ground_route_state_matches(frame, physics_state)):
            self._full_candidates += 1
            reviewed = verified_ground_route_candidate(frame, physics_state, MovementV1(),
                control_ticks=self.config.input_lease_ticks, tail_ticks=self.config.maximum_recovery_ticks,
                minimum_support=self.config.minimum_support_fraction, profile=self.profile,
                query_cache=query_cache)
            self._physics_steps += reviewed.physics_steps
            if (reviewed.status is QueryStatus.FEASIBLE
                    and all(self._geometry.project_current(s.position[0], s.position[2]).distance
                            <= self.config.maximum_cross_track_blocks for s in reviewed.trajectory)
                    and completion.contains(reviewed.trajectory[-1].position)):
                return self._decision(started, MovementV1(), reason)
        feasible = [candidate for candidate in candidates
                    if not candidate.blocked and not candidate.unsupported and not candidate.missing]
        if feasible:
            selected = min(feasible, key=lambda candidate: (candidate.score, candidate.movement.forward,
                                                              candidate.movement.strafe))
            return self._decision(
                started, selected.movement, reason,
                input_lease_ticks=(selected.control_ticks
                                   if selected.control_ticks else None),
            )
        missing = tuple(cell for candidate in candidates for cell in candidate.missing)
        if missing:
            # Releasing input is the bounded fallback when a more forceful brake cannot be proven.
            return self._decision(started, MovementV1(), "braking_release_requires_information", missing)
        if any(candidate.unsupported for candidate in candidates):
            self.state = FixedRouteState.UNSUPPORTED
            return self._decision(started, MovementV1(), "braking_release_unsupported_geometry")
        return self._decision(started, MovementV1(), "braking_release_no_safe_reverse")

    def _ranked_braking_candidates(
        self,
        frame: NavigationFrame,
        body: PlanarBodyState,
        target: tuple[float, float],
        query_cache: WorldQueryCache,
    ) -> list[_Candidate]:
        """Validate only braking controls that can still win the safe ranking."""
        if self._continuation is None:
            assert self._route is not None
            context = GroundTrackingContext(
                body, frame.body.body_box, frame.body.position[1],
                GroundTrackingRoute.from_fixed_route(self._route),
                self._progress, self._segment_index, self._previous_movement,
                target, True, self.profile,
                GroundTrackingLimits.from_fixed_route_config(self.config),
                frame.world, query_cache,
            )
            return [
                _Candidate(
                    item.movement, item.score, item.missing_cells,
                    item.blocked, item.unsupported, item.progress_gain,
                    item.full_replay_eligible, item.support_boundary,
                    item.control_ticks, item.completion_distance_blocks,
                    item.target_distance_blocks,
                )
                for item in GroundTrackingPolicy.candidates(context)
            ]
        rollouts = tuple(
            self._prepare_candidate_rollout(body, movement, target, braking=True)
            for movement in _MOVEMENTS
        )
        ordered = tuple(sorted(rollouts, key=self._rollout_key))
        evaluated: list[_Candidate] = []
        selected: _Candidate | None = None
        for rollout in ordered:
            if (selected is not None
                    and self._rollout_key(rollout) >= self._candidate_key(selected)):
                break
            candidate = self._evaluate_candidate(
                frame, body, rollout.movement, target, braking=True,
                query_cache=query_cache, rollout=rollout,
            )
            evaluated.append(candidate)
            if (not candidate.blocked and not candidate.unsupported
                    and not candidate.missing
                    and (selected is None
                         or self._candidate_key(candidate)
                         < self._candidate_key(selected))):
                selected = candidate
        if selected is None:
            evaluated_movements = {
                (candidate.movement.forward, candidate.movement.strafe)
                for candidate in evaluated
            }
            for rollout in ordered:
                key = (rollout.movement.forward, rollout.movement.strafe)
                if key not in evaluated_movements:
                    evaluated.append(self._evaluate_candidate(
                        frame, body, rollout.movement, target, braking=True,
                        query_cache=query_cache, rollout=rollout,
                    ))
        return evaluated

    def _prepare_candidate_rollout(
        self,
        body: PlanarBodyState,
        movement: MovementV1,
        target: tuple[float, float],
        *,
        braking: bool,
    ) -> _CandidateRollout:
        control = GroundControl(movement.forward, -movement.strafe, body.yaw_radians)
        # A stream interruption can leave the selected input active for its full
        # lease. Validate every leased tick, then neutral input until the model
        # reaches the same stopped threshold used by completion.
        neutral = GroundControl(0, 0, body.yaw_radians)
        lease_controls = (control,) * self.config.input_lease_ticks
        lease_states = predict_ground(body, lease_controls, self.profile)
        tracking_end = lease_states[-1]
        released = tracking_end
        states = list(lease_states)
        for _ in range(self.config.maximum_recovery_ticks):
            if math.hypot(released.velocity_x, released.velocity_z) <= (
                    self.config.stopped_speed_blocks_per_second):
                break
            released = predict_ground(released, (neutral,), self.profile)[-1]
            states.append(released)
        released_speed = math.hypot(released.velocity_x, released.velocity_z)
        if released_speed > self.config.stopped_speed_blocks_per_second:
            return _CandidateRollout(
                movement, tuple(states), tracking_end, math.inf,
                -math.inf, -math.inf, False, True,
            )
        # Completion may report stopped at 0.1 block/s, but collision safety must
        # still cover all remaining neutral-input drift. The calibrated model has
        # geometric decay, so its infinite tail has a closed-form displacement.
        retention = self.profile.velocity_retention_per_tick
        if released_speed > _EPSILON:
            if retention >= 1.0:
                return _CandidateRollout(
                    movement, tuple(states), tracking_end, math.inf,
                    -math.inf, -math.inf, False, True,
                )
            scale = self.profile.tick_seconds / (1.0 - retention)
            states.append(PlanarBodyState(
                released.x + released.velocity_x * scale,
                released.z + released.velocity_z * scale,
                0.0, 0.0, released.yaw_radians,
            ))
        end = states[-1] if braking else tracking_end
        end_speed = math.hypot(end.velocity_x, end.velocity_z)
        distance_to_target = math.hypot(end.x - target[0], end.z - target[1])
        if (braking and self._route.execution_contract is not None
                and self._route.execution_contract.completion_region is not None
                and target == (self._geometry.goal.x, self._geometry.goal.z)):
            distance_to_target = self._completion_distance(end.x, end.z)
        projection = self._geometry.project(
            end.x, end.z, self._progress, self._segment_index,
            self.config.maximum_cross_track_blocks,
        )  # type: ignore[union-attr]
        projection = self._continuation_projection(projection, end.x, end.z)
        raw_progress_gain = projection.progress - self._progress
        cross_track_blocked = (
            not braking
            and projection.distance > self.config.maximum_cross_track_blocks
        )
        progress_gain = (
            raw_progress_gain if braking else max(-0.25, raw_progress_gain)
        )
        input_switch = (movement.forward != self._previous_movement.forward
                        or movement.strafe != self._previous_movement.strafe)
        if braking:
            base_score = end_speed * 8.0 + distance_to_target * 12.0
        else:
            base_score = (
                distance_to_target * 2.0
                + projection.distance * 5.0
                - progress_gain * 7.0
                + (0.04 if input_switch else 0.0)
                + (0.30 if movement == MovementV1() else 0.0)
            )
        return _CandidateRollout(
            movement, tuple(states), tracking_end, base_score,
            progress_gain, raw_progress_gain, cross_track_blocked, False,
        )

    def _evaluate_candidate(self, frame: NavigationFrame, body: PlanarBodyState,
                            movement: MovementV1, target: tuple[float, float],
                            *, braking: bool,
                            query_cache: WorldQueryCache,
                            rollout: _CandidateRollout | None = None) -> _Candidate:
        if rollout is None:
            rollout = self._prepare_candidate_rollout(
                body, movement, target, braking=braking,
            )
        if rollout.unsupported:
            return _Candidate(movement, math.inf, (), False, True, -math.inf)
        states = rollout.states
        tracking_end = rollout.tracking_end
        missing: set[BlockPos] = set()
        support_penalty = 0.0
        wall_penalty = 0.0
        margin = self.config.motion_prediction_margin_blocks
        support_box = frame.body.body_box
        previous_state = body
        blocked = False
        unsupported = False
        full_replay_eligible = False
        support_boundary = False
        for state in states[1:]:
            delta = (state.x - previous_state.x, 0.0, state.z - previous_state.z)
            collision_box = _directional_prediction_box(
                support_box, delta, margin,
            )
            collision = sweep(
                collision_box, delta, frame.world,
                query_cache=query_cache,
            )
            if (self.profile.motion_catalog is not None
                    and self.profile.ground_model_id is not None
                    and unsupported_motion_cells(
                        self.profile.motion_catalog, frame.world, collision.dependencies,
                        self.profile.ground_model_id,
                        query_cache=query_cache,
                    )):
                unsupported = True
                break
            if collision.status is QueryStatus.UNSUPPORTED:
                unsupported = True
                break
            if collision.status is QueryStatus.BLOCKED:
                blocked = True
                full_replay_eligible = not collision.missing_cells
                break
            if collision.status is QueryStatus.NEEDS_INFORMATION:
                missing.update(collision.missing_cells)
            next_collision_box = collision_box.moved(*delta)
            next_support_box = support_box.moved(*delta)
            support = query_support(
                next_support_box, frame.world,
                query_cache=query_cache,
            )
            support_evidence = [support]
            actual_span = (
                math.floor(next_support_box.min_x + _EPSILON),
                math.floor(next_support_box.max_x - _EPSILON),
                math.floor(next_support_box.min_z + _EPSILON),
                math.floor(next_support_box.max_z - _EPSILON),
            )
            uncertainty_span = (
                math.floor(next_collision_box.min_x + _EPSILON),
                math.floor(next_collision_box.max_x - _EPSILON),
                math.floor(next_collision_box.min_z + _EPSILON),
                math.floor(next_collision_box.max_z - _EPSILON),
            )
            if uncertainty_span != actual_span:
                # The expanded box is only an evidence envelope here. Its area
                # is never counted as real foot contact; it merely discovers a
                # material or unknown cell that position error could enter.
                support_evidence.append(query_support(
                    next_collision_box, frame.world,
                    query_cache=query_cache,
                ))
            for evidence in support_evidence:
                if (self.profile.motion_catalog is not None
                        and self.profile.ground_model_id is not None
                        and unsupported_motion_cells(
                            self.profile.motion_catalog, frame.world, evidence.dependencies,
                            self.profile.ground_model_id,
                            query_cache=query_cache,
                        )):
                    unsupported = True
                    break
                if evidence.status is QueryStatus.UNSUPPORTED:
                    unsupported = True
                    break
                if evidence.status is QueryStatus.BLOCKED:
                    blocked = True
                    full_replay_eligible = not evidence.missing_cells
                    support_boundary = True
                    break
                if evidence.status is QueryStatus.NEEDS_INFORMATION:
                    missing.update(evidence.missing_cells)
                if (evidence.status is QueryStatus.FEASIBLE
                        and (not self.profile.support_materials
                             or not set(evidence.support_materials).issubset(
                                 self.profile.support_materials))):
                    unsupported = True
                    break
            if blocked or unsupported:
                break
            width = next_support_box.max_x - next_support_box.min_x
            depth = next_support_box.max_z - next_support_box.min_z
            shift_x, shift_z = min(margin, width), min(margin, depth)
            maximum_lost_area = width * depth - (width - shift_x) * (depth - shift_z)
            worst_support = max(
                0.0,
                support.support_fraction - maximum_lost_area / (width * depth),
            )
            if worst_support < self.config.minimum_support_fraction:
                blocked = True
                full_replay_eligible = not missing
                support_boundary = True
                break
            shortfall = max(0.0, self.config.preferred_support_fraction - worst_support)
            support_penalty += shortfall * shortfall
            wall_penalty += _horizontal_wall_penalty(
                next_collision_box, frame.world,
                self.config.wall_soft_margin_blocks, query_cache,
            )
            support_box = next_support_box
            previous_state = state
        if blocked or unsupported:
            return _Candidate(
                movement, math.inf, tuple(sorted(missing)), blocked, unsupported, -math.inf,
                full_replay_eligible, support_boundary,
            )
        # The full release tail above answers whether the selected input remains
        # safe if the stream disappears.  It must not also define which movement
        # best follows the route: that would rank every action by its eventual
        # stopped position and can prefer neutral input at a corner.  Tracking
        # utility uses the end of the input lease; braking still uses the stopped
        # state because settling is its purpose.
        if rollout.cross_track_blocked:
            return _Candidate(
                movement, math.inf, tuple(sorted(missing)), True, False,
                rollout.raw_progress_gain,
            )
        if braking:
            # Braking must settle inside the requested region. Weighting only speed
            # creates a limit cycle where release stops just outside the tolerance.
            score = rollout.base_score + support_penalty * 8.0 + wall_penalty * 2.0
        else:
            score = (
                rollout.base_score
                + support_penalty * 10.0
                + wall_penalty * 2.0
            )
        return _Candidate(
            movement, score, tuple(sorted(missing)), False, False,
            rollout.progress_gain,
        )
