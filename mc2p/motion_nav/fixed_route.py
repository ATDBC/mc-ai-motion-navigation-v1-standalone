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
from mc2p.motion_nav.ground_modes import (
    GroundModeProfile, ModeReadiness, evaluate_ground_mode, movement_for_ground_mode,
    observed_ground_mode,
)
from mc2p.motion_nav.movement_transition import MovementMode, GoalState
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, PhysicsState, StateBuildStatus,
)
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.segment_entry import (
    SegmentEntryWindow, body_fits_segment_entry,
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

    def __post_init__(self) -> None:
        require_identifier(self.route_id, "fixed route id")
        if type(self.points) is not tuple or not self.points or any(
                type(point) is not RoutePoint for point in self.points):
            raise ContractViolation("fixed route requires immutable typed points")


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
                          goal: GoalState, endpoint: RoutePoint) -> FixedRouteConfig:
    """Use the same final stop domain during screening and actual tracking."""
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

    def __post_init__(self) -> None:
        if (type(self.position) is not tuple or len(self.position) != 3
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in self.position)
                or type(self.first_tick) is not int or self.first_tick < 0
                or type(self.latest_tick) is not int
                or not self.first_tick <= self.latest_tick < self.first_tick + 20
                or type(self.movements) is not tuple
                or not 1 <= len(self.movements) <= 4
                or any(type(value) is not MovementV1 for value in self.movements)):
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
        self._no_progress_frames = 0
        self._stall_detected = False
        self._segment_index = 0
        self._mode_pending_frames = 0
        self._geometric_support_frames = 0
        self._traversal_plan: GroundTraversalPlan | None = None
        self._traversal_tick_index = 0
        self._traversal_replan_pending = False

    def start(
        self, route: FixedRoute, frame: NavigationFrame, *,
        traversal_plan: GroundTraversalPlan | None = None,
    ) -> None:
        from mc2p.motion_nav.ground_traversal import GroundTraversalPlan
        if type(route) is not FixedRoute or type(frame) is not NavigationFrame:
            raise ContractViolation("starting fixed route requires a route and navigation frame")
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
        self._no_progress_frames = 0
        self._stall_detected = False
        self._segment_index = 0
        self._mode_pending_frames = 0
        self._geometric_support_frames = 0
        self._traversal_plan = traversal_plan
        self._traversal_tick_index = 0
        self._traversal_replan_pending = False
        self.state = FixedRouteState.RUNNING

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
                  missing: tuple[BlockPos, ...] = ()) -> FixedRouteDecision:
        if (self.mode_profile is not None
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
        return FixedRouteDecision(
            self.state, movement, self._progress, self._cross_track,
            tuple(sorted(set(missing))), time.perf_counter_ns() - started,
            self.config.input_lease_ticks, reason,
        )

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
        world = PhysicsWorldView(frame.world, JAVA_1_21_RULESET)
        for rollout in rollouts:
            if rollout.unsupported or rollout.cross_track_blocked:
                continue
            simulated = state
            safe = True
            commands = (
                (rollout.movement,) * self.config.input_lease_ticks
                + (MovementV1(),) * self.config.maximum_recovery_ticks
            )
            for command_index, command in enumerate(commands):
                speed = math.hypot(
                    simulated.velocity_blocks_per_tick[0],
                    simulated.velocity_blocks_per_tick[2],
                ) * 20.0
                if (command_index >= self.config.input_lease_ticks
                        and speed <= self.config.stopped_speed_blocks_per_second):
                    break
                projected = project_movement_command(simulated, command)
                if projected.status is not ProjectionStatus.READY:
                    saw_unsupported = True
                    safe = False
                    break
                assert projected.tick_input is not None
                calculated = physics_step(
                    simulated, projected.tick_input, world, JAVA_1_21_RULESET,
                )
                if calculated.status is CalculationStatus.NEEDS_WORLD:
                    missing.update(calculated.missing_cells)
                    safe = False
                    break
                if calculated.status is not CalculationStatus.OK:
                    saw_unsupported = (
                        saw_unsupported
                        or calculated.status is CalculationStatus.UNSUPPORTED
                    )
                    safe = False
                    break
                assert calculated.next_state is not None
                simulated = calculated.next_state
                if not self._inside_traversal_corridor(
                        simulated, self._traversal_plan.corridor):
                    safe = False
                    break
            if safe:
                return rollout.movement, tuple(sorted(missing)), saw_unsupported
        return None, tuple(sorted(missing)), saw_unsupported

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
        self._progress = max(self._progress, projection.progress)
        self._segment_index = max(self._segment_index, projection.segment_index)
        self._cross_track = projection.distance
        if (projection.distance > plan.maximum_cross_track_blocks
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
        entry_matches = (
            body_fits_segment_entry(
                self.config.handoff_entry_window, frame.body,
                observed_ground_mode(frame.body),
            )
            if self.config.handoff_entry_window is not None else None
        )
        completion_speed = (
            self.config.handoff_entry_window.maximum_speed_blocks_per_second
            if self.config.handoff_entry_window is not None
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
        if at_goal and speed <= completion_speed:
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

    def decide(self, frame: NavigationFrame, *, input_confirmed: bool = True,
               physics_state: PhysicsState | None = None,
               query_cache: WorldQueryCache | None = None) -> FixedRouteDecision:
        started = time.perf_counter_ns()
        if type(frame) is not NavigationFrame or type(input_confirmed) is not bool:
            raise ContractViolation("fixed route decision requires a navigation frame and confirmation")
        if self._route is None or self._geometry is None or self._session is None:
            raise ContractViolation("fixed route controller has not started")
        if frame.session != self._session:
            self.state = FixedRouteState.FAILED
            return self._decision(started, MovementV1(), "world_session_changed")
        if self._last_sequence is not None and frame.body.sequence_id <= self._last_sequence:
            raise ContractViolation("fixed route frame did not advance")
        self._last_sequence = frame.body.sequence_id
        if self.state in {
            FixedRouteState.CANCELLED, FixedRouteState.SUCCEEDED,
            FixedRouteState.INPUT_LOST, FixedRouteState.FAILED, FixedRouteState.UNSUPPORTED,
        }:
            return self._decision(started, MovementV1(), self.state.value)
        if not input_confirmed:
            self.state = FixedRouteState.INPUT_LOST
            return self._decision(started, MovementV1(), "input_application_unconfirmed")
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
        projection = self._geometry.project(
            body.x, body.z, self._progress, self._segment_index,
            self.config.maximum_cross_track_blocks,
            prefer_next_segment=self._no_progress_frames >= 3,
        )
        self._progress = max(self._progress, projection.progress)
        self._segment_index = max(self._segment_index, projection.segment_index)
        self._cross_track = projection.distance
        preview_missing = self._route_preview_missing(frame, query_cache)
        if self._progress >= self._progress_anchor + 0.10:
            self._progress_anchor = self._progress
            self._no_progress_frames = 0
        elif self.state is FixedRouteState.RUNNING and self._previous_movement != MovementV1():
            self._no_progress_frames += 1
        if self._no_progress_frames >= 20:
            self._stall_detected = True

        if self._cancel_requested:
            if speed <= self.config.stopped_speed_blocks_per_second:
                self.state = FixedRouteState.CANCELLED
                return self._decision(started, MovementV1(), "cancelled_after_stop")
            self.state = FixedRouteState.CANCELLING
            return self._brake(
                frame, body, started, hold_position=True,
                reason="cancel_braking", query_cache=query_cache,
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
        completion_speed = (
            self.config.handoff_entry_window.maximum_speed_blocks_per_second
            if self.config.handoff_entry_window is not None
            else self.config.handoff_speed_blocks_per_second
            if self.config.handoff_speed_blocks_per_second is not None
            else self.config.stopped_speed_blocks_per_second
        )
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
            )

        stop_distance = self._release_distance(body, completion_speed)
        remaining = max(0.0, self._geometry.total_length - self._progress)
        if (at_goal
                or (speed > completion_speed
                    and remaining <= stop_distance
                    + self.config.endpoint_tolerance_blocks * 0.65)):
            self.state = FixedRouteState.BRAKING
            return self._brake(
                frame, body, started, hold_position=False,
                reason="goal_braking", query_cache=query_cache,
            )

        corner_distance=self._geometry.next_sharp_corner_distance(
            self._progress,self._segment_index)
        if (corner_distance is not None
                and corner_distance<=self.config.corner_braking_lookahead_blocks
                and speed>self.config.corner_speed_blocks_per_second):
            corner_target=self._geometry.point_at(
                min(self._geometry.total_length,self._progress+self.config.lookahead_min_blocks))
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
            tick = frame.body.movement_tick_id
            if tick is not None:
                index = tick + 1 - hint.first_tick
                if 0 <= index and tick + 1 <= hint.latest_tick:
                    target = (hint.position[0], hint.position[2])
                    # This is a ground proposal, checked against the current
                    # world and its full release tail. Admission separately
                    # verifies the actually applied prefix before any jump.
                    movement = (hint.movements[index] if index < len(hint.movements)
                                else MovementV1())
                    preferred = self._evaluate_candidate(
                        frame, body, movement, target, braking=False,
                        query_cache=query_cache,
                    )
                    if (not preferred.blocked and not preferred.unsupported
                            and not preferred.missing):
                        self.state = FixedRouteState.RUNNING
                        return self._decision(started, movement,
                                              "tracking_conditional_motion_entry")
                elif tick + 1 > hint.latest_tick:
                    self._handoff_target_hint = None
        if (mode_pending and self.mode_profile is not None
                and self.mode_profile.mode is MovementMode.SPRINT):
            candidates = [self._evaluate_candidate(
                              frame, body, movement, target, braking=False,
                              query_cache=query_cache,
                          )
                          for movement in _MOVEMENTS]
        else:
            candidates = self._ranked_tracking_candidates(
                frame, body, target, query_cache,
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
                selected = min(feasible, key=lambda candidate: (
                    candidate.score, candidate.movement.forward, candidate.movement.strafe,
                ))
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
                if any(candidate.unsupported for candidate in candidates):
                    self.state = FixedRouteState.UNSUPPORTED
                    return self._decision(
                        started, MovementV1(), "forward_ground_material_unsupported",
                    )
                self.state = FixedRouteState.BLOCKED
                return self._decision(started, MovementV1(), "fixed_route_has_no_forward_control")
            self.state = FixedRouteState.RUNNING
            return self._decision(started, selected.movement,
                                  ("ground_mode_confirmation_pending" if mode_pending
                                   else ("tracking_fixed_route_geometric_support"
                                         if geometric_ground_support
                                         else "tracking_fixed_route")),
                                  preview_missing)
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
    def _candidate_key(candidate: _Candidate) -> tuple[float, int, int]:
        return (
            candidate.score,
            candidate.movement.forward,
            candidate.movement.strafe,
        )

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
               query_cache: WorldQueryCache) -> FixedRouteDecision:
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
        candidates = self._ranked_braking_candidates(
            frame, body, target, query_cache,
        )
        feasible = [candidate for candidate in candidates
                    if not candidate.blocked and not candidate.unsupported and not candidate.missing]
        if feasible:
            selected = min(feasible, key=lambda candidate: (candidate.score, candidate.movement.forward,
                                                              candidate.movement.strafe))
            return self._decision(started, selected.movement, reason)
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
        projection = self._geometry.project(
            end.x, end.z, self._progress, self._segment_index,
            self.config.maximum_cross_track_blocks,
        )  # type: ignore[union-attr]
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
