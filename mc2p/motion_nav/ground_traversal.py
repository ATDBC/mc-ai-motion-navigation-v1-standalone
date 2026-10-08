"""Physics-backed proof for ordinary walking over known small height changes."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections import OrderedDict
import math

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteConfig, RoutePoint
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.geometry import QueryStatus, query_support
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, PhysicsState, TickInput,
)
from mc2p.motion_nav.segment_entry import MotionContinuationRequirement, SegmentEntryWindow
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.world_model import Aabb, BlockPos


class GroundTraversalStatus(StrEnum):
    VERIFIED = "verified"
    NEEDS_WORLD = "needs_world"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class GroundTraversalExitRequirement:
    """The observed state that an ordinary-ground proof must deliver."""

    completion_region: GroundCompletionRegion
    allowed_poses: frozenset[str]
    allowed_modes: frozenset[MovementMode]
    minimum_speed_blocks_per_second: float
    maximum_speed_blocks_per_second: float
    required_direction: tuple[float, float] | None = None
    maximum_direction_error_radians: float | None = None

    def __post_init__(self) -> None:
        if type(self.completion_region) is not GroundCompletionRegion:
            raise ContractViolation("ground exit requires a completion region")
        if (type(self.allowed_poses) is not frozenset or not self.allowed_poses
                or any(type(value) is not str or not value
                       for value in self.allowed_poses)):
            raise ContractViolation("ground exit poses must be explicit")
        if (type(self.allowed_modes) is not frozenset or not self.allowed_modes
                or any(type(value) is not MovementMode
                       for value in self.allowed_modes)):
            raise ContractViolation("ground exit modes must be explicit")
        for value in (self.minimum_speed_blocks_per_second,
                      self.maximum_speed_blocks_per_second):
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ContractViolation("ground exit speed must be finite")
        if (self.minimum_speed_blocks_per_second < 0
                or self.maximum_speed_blocks_per_second
                   < self.minimum_speed_blocks_per_second):
            raise ContractViolation("ground exit speed interval is invalid")
        if (self.required_direction is None) != (
                self.maximum_direction_error_radians is None):
            raise ContractViolation("ground exit direction requirement is incomplete")
        if self.required_direction is not None:
            if (type(self.required_direction) is not tuple
                    or len(self.required_direction) != 2
                    or any(type(value) not in (int, float)
                           or not math.isfinite(float(value))
                           for value in self.required_direction)
                    or abs(math.hypot(*self.required_direction) - 1.0) > 1.0e-6
                    or type(self.maximum_direction_error_radians) not in (int, float)
                    or not 0 <= self.maximum_direction_error_radians <= math.pi):
                raise ContractViolation("ground exit direction is invalid")

    def accepts(self, state: PhysicsState, mode: MovementMode) -> bool:
        if type(state) is not PhysicsState or type(mode) is not MovementMode:
            raise ContractViolation("ground exit check requires typed state and mode")
        x, y, z = state.position
        bounds = self.completion_region.bounds
        speed = math.hypot(
            state.velocity_blocks_per_tick[0] * 20.0,
            state.velocity_blocks_per_tick[2] * 20.0,
        )
        if not (
            bounds.min_x - 1.0e-9 <= x <= bounds.max_x + 1.0e-9
            and bounds.min_y - 1.0e-9 <= y <= bounds.max_y + 1.0e-9
            and bounds.min_z - 1.0e-9 <= z <= bounds.max_z + 1.0e-9
            and state.pose in self.allowed_poses
            and mode in self.allowed_modes
            and self.minimum_speed_blocks_per_second - 1.0e-9
                <= speed <= self.maximum_speed_blocks_per_second + 1.0e-9
            and state.on_ground
        ):
            return False
        if self.required_direction is None or speed <= 0.1:
            return True
        vx = state.velocity_blocks_per_tick[0]
        vz = state.velocity_blocks_per_tick[2]
        length = math.hypot(vx, vz)
        if length <= 1.0e-12:
            return self.minimum_speed_blocks_per_second <= 1.0e-9
        dot = max(-1.0, min(1.0,
            (vx * self.required_direction[0]
             + vz * self.required_direction[1]) / length))
        return math.acos(dot) <= self.maximum_direction_error_radians + 1.0e-9


@dataclass(frozen=True, slots=True)
class GroundTraversalPlan:
    route: FixedRoute
    entry_window: SegmentEntryWindow
    exit_window: SegmentEntryWindow
    trajectory: tuple[PhysicsState, ...]
    inputs: tuple[TickInput, ...]
    events_by_tick: tuple[tuple[str, ...], ...]
    corridor: tuple[Aabb, ...]
    dependencies: tuple[BlockPos, ...]
    estimated_ticks: int
    ruleset_id: str
    input_projection_version: str
    profile_id: str
    maximum_cross_track_blocks: float
    surface_node_path: tuple[SurfaceNodeId, ...] = ()
    continuation: MotionContinuationRequirement | None = None
    neutral_stop_trajectory: tuple[PhysicsState, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.route) is not FixedRoute
                or type(self.entry_window) is not SegmentEntryWindow
                or type(self.exit_window) is not SegmentEntryWindow):
            raise ContractViolation("ground traversal plan requires typed route windows")
        if (type(self.trajectory) is not tuple or len(self.trajectory) < 2
                or type(self.inputs) is not tuple
                or len(self.inputs) + 1 != len(self.trajectory)
                or len(self.events_by_tick) != len(self.inputs)):
            raise ContractViolation("ground traversal trajectory is incomplete")
        if (type(self.corridor) is not tuple or not self.corridor
                or any(type(box) is not Aabb for box in self.corridor)):
            raise ContractViolation("ground traversal corridor must be explicit")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("ground traversal dependencies must be immutable")
        if type(self.estimated_ticks) is not int or self.estimated_ticks != len(self.inputs):
            raise ContractViolation("ground traversal tick estimate must match its proof")
        if (type(self.surface_node_path) is not tuple
                or any(type(node) is not SurfaceNodeId
                       for node in self.surface_node_path)
                or (self.surface_node_path
                    and len(self.surface_node_path) != len(self.route.points))):
            raise ContractViolation(
                "ground traversal surface path must match its canonical route"
            )
        if self.continuation is not None:
            if (type(self.continuation) is not MotionContinuationRequirement
                    or not self.continuation.accepts(self.trajectory[-1])
                    or self.exit_window != self.continuation.entry_window
                    or type(self.neutral_stop_trajectory) is not tuple
                    or not 1 <= len(self.neutral_stop_trajectory) <= FixedRouteConfig().maximum_recovery_ticks + 1
                    or any(type(state) is not PhysicsState for state in self.neutral_stop_trajectory)
                    or self.neutral_stop_trajectory[0] != self.trajectory[-1]
                    or not self.neutral_stop_trajectory[-1].on_ground
                    or math.hypot(*self.neutral_stop_trajectory[-1].velocity_blocks_per_tick[::2]) > 1.0e-9):
                raise ContractViolation("ground continuation must retain its proved exit and complete stop tail")
        elif self.neutral_stop_trajectory:
            raise ContractViolation("ground stop tail must belong to a continuation proof")


@dataclass(frozen=True, slots=True)
class GroundTraversalResult:
    status: GroundTraversalStatus
    plan: GroundTraversalPlan | None = None
    dependencies: tuple[BlockPos, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not GroundTraversalStatus:
            raise ContractViolation("invalid ground traversal status")
        if (self.status is GroundTraversalStatus.VERIFIED) != (
                type(self.plan) is GroundTraversalPlan):
            raise ContractViolation("only verified traversal can carry a plan")


class GroundTraversalProofCache:
    """Small worker-side LRU for proofs tied to exact world dependencies."""

    def __init__(self, maximum_entries: int = 128) -> None:
        if type(maximum_entries) is not int or maximum_entries < 1:
            raise ContractViolation("ground traversal cache bound must be positive")
        self.maximum_entries = maximum_entries
        self._entries: OrderedDict[tuple, GroundTraversalResult] = OrderedDict()

    def get(self, key: tuple) -> GroundTraversalResult | None:
        if type(key) is not tuple:
            raise ContractViolation("ground traversal cache key must be immutable")
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
        return value

    def put(self, key: tuple, value: GroundTraversalResult) -> None:
        if type(key) is not tuple or type(value) is not GroundTraversalResult:
            raise ContractViolation("ground traversal cache requires typed immutable data")
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self.maximum_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


def _direction(first, second) -> tuple[float, float] | None:
    dx, dz = second.x - first.x, second.z - first.z
    length = math.hypot(dx, dz)
    if length <= 1.0e-9:
        return None
    direction = (dx / length, dz / length)
    if min(abs(direction[0]), abs(direction[1])) > 1.0e-6:
        return None
    return direction


def _entry_window(
    route: FixedRoute,
    profile: GroundMotionProfile,
    state: PhysicsState,
    *,
    exit: bool,
):
    first, second = (
        (route.points[-2], route.points[-1])
        if exit else (route.points[0], route.points[1])
    )
    direction = _direction(first, second)
    assert direction is not None
    reference = state.position
    speed = math.hypot(
        state.velocity_blocks_per_tick[0] * 20.0,
        state.velocity_blocks_per_tick[2] * 20.0,
    )
    return SegmentEntryWindow(
        reference_point=reference,
        horizontal_approach_direction=direction,
        minimum_longitudinal_offset_blocks=-0.05,
        maximum_longitudinal_offset_blocks=0.05,
        maximum_lateral_offset_blocks=0.05,
        minimum_feet_y=reference[1] - 0.05,
        maximum_feet_y=reference[1] + 0.05,
        minimum_speed_blocks_per_second=max(0.0, speed - 0.10),
        maximum_speed_blocks_per_second=min(
            profile.maximum_speed_blocks_per_second + 0.10,
            speed + 0.10,
        ),
        maximum_velocity_direction_error_radians=math.radians(35.0),
        allowed_poses=frozenset({"standing"}),
        allowed_modes=frozenset({MovementMode.WALK}),
        required_yaw_radians=None,
        maximum_yaw_error_radians=None,
        profile_id=profile.profile_id,
    )


def _corridor(route: FixedRoute, radius: float) -> tuple[Aabb, ...]:
    boxes = []
    for first, second in zip(route.points, route.points[1:]):
        boxes.append(Aabb(
            min(first.x, second.x) - radius,
            min(first.y, second.y) - 0.65,
            min(first.z, second.z) - radius,
            max(first.x, second.x) + radius,
            max(first.y, second.y) + 2.4,
            max(first.z, second.z) + radius,
        ))
    return tuple(boxes)


def _inside_corridor(state: PhysicsState, corridor: tuple[Aabb, ...]) -> bool:
    x, y, z = state.position
    return any(
        box.min_x <= x <= box.max_x
        and box.min_y <= y <= box.max_y
        and box.min_z <= z <= box.max_z
        for box in corridor
    )


def _continuation_corridor(requirement: MotionContinuationRequirement, radius: float) -> Aabb:
    window = requirement.entry_window
    x, _, z = window.reference_point
    dx, dz = window.horizontal_approach_direction
    lateral = window.maximum_lateral_offset_blocks + radius
    points = tuple((x + dx * longitudinal - dz * offset,
                    z + dz * longitudinal + dx * offset)
                   for longitudinal in (window.minimum_longitudinal_offset_blocks - radius,
                                        window.maximum_longitudinal_offset_blocks + radius)
                   for offset in (-lateral, lateral))
    return Aabb(min(point[0] for point in points), window.minimum_feet_y - .65,
                min(point[1] for point in points), max(point[0] for point in points),
                window.maximum_feet_y + 2.4, max(point[1] for point in points))


def verify_ground_continuation_stop_tail(
    state: PhysicsState, world: PhysicsWorldView, corridor: tuple[Aabb, ...],
    requirement: MotionContinuationRequirement,
) -> tuple[GroundTraversalStatus, tuple[PhysicsState, ...], tuple[BlockPos, ...], tuple[BlockPos, ...]]:
    """Return status, actual stop states, dependencies and any missing cells."""
    trajectory = [state]
    dependencies = set()
    config = FixedRouteConfig()
    for _ in range(config.maximum_recovery_ticks + 1):
        support = query_support(state.body_box, world._world)
        dependencies.update(support.dependencies)
        if support.status is QueryStatus.NEEDS_INFORMATION:
            return GroundTraversalStatus.NEEDS_WORLD, (), tuple(sorted(dependencies)), support.missing_cells
        if support.status is QueryStatus.UNSUPPORTED:
            return GroundTraversalStatus.UNSUPPORTED, (), tuple(sorted(dependencies)), ()
        if (support.status is not QueryStatus.FEASIBLE
                or support.support_fraction < config.minimum_support_fraction
                or not state.on_ground or not _inside_corridor(state, corridor)
                or not requirement.entry_window.minimum_feet_y <= state.position[1]
                           <= requirement.entry_window.maximum_feet_y):
            return GroundTraversalStatus.BLOCKED, (), tuple(sorted(dependencies)), ()
        if math.hypot(*state.velocity_blocks_per_tick[::2]) <= 1.0e-9:
            return GroundTraversalStatus.VERIFIED, tuple(trajectory), tuple(sorted(dependencies)), ()
        if len(trajectory) > config.maximum_recovery_ticks:
            break
        projected = project_movement_command(state, MovementV1())
        if projected.status is not ProjectionStatus.READY:
            return GroundTraversalStatus.UNSUPPORTED, (), tuple(sorted(dependencies)), ()
        calculated = step(state, projected.tick_input, world, JAVA_1_21_RULESET)
        dependencies.update(calculated.dependencies)
        if calculated.status is CalculationStatus.NEEDS_WORLD:
            return GroundTraversalStatus.NEEDS_WORLD, (), tuple(sorted(dependencies)), calculated.missing_cells
        if calculated.status is not CalculationStatus.OK:
            status = (GroundTraversalStatus.UNSUPPORTED if calculated.status is CalculationStatus.UNSUPPORTED
                      else GroundTraversalStatus.BLOCKED)
            return status, (), tuple(sorted(dependencies)), ()
        state = calculated.next_state
        assert state is not None
        trajectory.append(state)
    return GroundTraversalStatus.BUDGET_EXHAUSTED, (), tuple(sorted(dependencies)), ()


def verify_ground_traversal(
    entry_state: PhysicsState,
    route: FixedRoute,
    world: PhysicsWorldView,
    profile: GroundMotionProfile,
    *,
    maximum_ticks: int,
    surface_node_path: tuple[SurfaceNodeId, ...] = (),
    continuation: MotionContinuationRequirement | None = None,
    exit_requirement: GroundTraversalExitRequirement | None = None,
) -> GroundTraversalResult:
    if (type(entry_state) is not PhysicsState or type(route) is not FixedRoute
            or type(world) is not PhysicsWorldView
            or type(profile) is not GroundMotionProfile
            or type(maximum_ticks) is not int or maximum_ticks < 1):
        raise ContractViolation("ground traversal verification requires typed bounded inputs")
    if (type(surface_node_path) is not tuple
            or any(type(node) is not SurfaceNodeId for node in surface_node_path)
            or (surface_node_path and len(surface_node_path) != len(route.points))):
        raise ContractViolation("ground traversal surface path is invalid")
    if entry_state.session != world.session:
        raise ContractViolation("ground traversal world belongs to another session")
    if continuation is not None and type(continuation) is not MotionContinuationRequirement:
        raise ContractViolation("ground continuation requirement must be typed")
    if (exit_requirement is not None
            and type(exit_requirement) is not GroundTraversalExitRequirement):
        raise ContractViolation("ground exit requirement must be typed")
    if continuation is not None and exit_requirement is not None:
        raise ContractViolation("ground traversal cannot have two exit owners")
    if (entry_state.ruleset_id != JAVA_1_21_RULESET.ruleset_id
            or entry_state.pose != "standing" or entry_state.sprinting
            or entry_state.sneaking):
        return GroundTraversalResult(
            GroundTraversalStatus.UNSUPPORTED,
            reasons=("ordinary_walk_entry_required",),
        )
    entry_speed = math.hypot(
        entry_state.velocity_blocks_per_tick[0] * 20.0,
        entry_state.velocity_blocks_per_tick[2] * 20.0,
    )
    if entry_speed > profile.maximum_speed_blocks_per_second + 0.10:
        return GroundTraversalResult(
            GroundTraversalStatus.UNSUPPORTED,
            reasons=("ordinary_walk_entry_speed_outside_profile",),
        )
    if len(route.points) < 2:
        return GroundTraversalResult(
            GroundTraversalStatus.UNSUPPORTED,
            reasons=("ground_traversal_requires_motion",),
        )
    directions = tuple(
        _direction(first, second)
        for first, second in zip(route.points, route.points[1:])
    )
    if any(direction is None for direction in directions):
        return GroundTraversalResult(
            GroundTraversalStatus.UNSUPPORTED,
            reasons=("ground_traversal_requires_nonzero_segments",),
        )
    height_changes = tuple(
        second.y - first.y
        for first, second in zip(route.points, route.points[1:])
    )
    if any(
        delta_y > entry_state.step_height_blocks + 1.0e-9
        or delta_y < -1.0 - 1.0e-9
        for delta_y in height_changes
    ):
        return GroundTraversalResult(
            GroundTraversalStatus.UNSUPPORTED,
            reasons=("support_height_change_exceeds_step_rule",),
        )

    if continuation is not None and (
            continuation.mode is not MovementMode.WALK
            or "standing" not in continuation.entry_window.allowed_poses
            or abs(continuation.entry_window.reference_point[1] - route.points[-1].y) > .10):
        return GroundTraversalResult(GroundTraversalStatus.UNSUPPORTED,
            reasons=("ordinary_same_height_ground_continuation_required",))

    radius = 0.45
    corridor = _corridor(route, radius)
    if continuation is not None:
        corridor += (_continuation_corridor(continuation, radius),)
    trajectory = [entry_state]
    inputs = []
    events = []
    dependencies: set[BlockPos] = set()
    target_index = 1
    state = entry_state
    braking = False
    for _ in range(maximum_ticks):
        target = route.points[target_index]
        dx = target.x - state.position[0]
        dz = target.z - state.position[2]
        distance = math.hypot(dx, dz)
        if (distance <= 0.40
                and abs(state.position[1] - target.y) <= 0.15
                and target_index < len(route.points) - 1):
            target_index += 1
            target = route.points[target_index]
            dx = target.x - state.position[0]
            dz = target.z - state.position[2]
            distance = math.hypot(dx, dz)
        if target_index == len(route.points) - 1 and continuation is not None:
            window = continuation.entry_window
            direction_x, direction_z = window.horizontal_approach_direction
            target = RoutePoint(
                window.reference_point[0] + direction_x * window.maximum_longitudinal_offset_blocks,
                window.reference_point[1],
                window.reference_point[2] + direction_z * window.maximum_longitudinal_offset_blocks,
            )
            dx, dz = target.x - state.position[0], target.z - state.position[2]
            distance = math.hypot(dx, dz)
            if continuation.accepts(state):
                status, stop_tail, stop_dependencies, missing = verify_ground_continuation_stop_tail(
                    state, world, corridor, continuation,
                )
                dependencies.update(stop_dependencies)
                if status is not GroundTraversalStatus.VERIFIED:
                    return GroundTraversalResult(status, dependencies=tuple(sorted(dependencies)),
                        missing_cells=missing, reasons=("ground_continuation_stop_tail_unproven",))
                plan = GroundTraversalPlan(
                    route, _entry_window(route, profile, trajectory[0], exit=False),
                    continuation.entry_window, tuple(trajectory), tuple(inputs), tuple(events), corridor,
                    tuple(sorted(dependencies)), len(inputs), JAVA_1_21_RULESET.ruleset_id,
                    "mc2p.input-projection.v1", profile.profile_id, radius, surface_node_path,
                    continuation, stop_tail,
                )
                return GroundTraversalResult(GroundTraversalStatus.VERIFIED, plan,
                                              dependencies=plan.dependencies)
        reached_terminal = (
            target_index == len(route.points) - 1
                and distance <= 0.35
                and abs(state.position[1] - target.y) <= 0.10
                and state.on_ground
        )
        # A shallow downward edge can keep the feet on the higher shape until
        # the body centre has nearly crossed the terminal point.  Waiting for
        # the lower feet height before releasing forward input then spends the
        # entire verified endpoint margin on momentum.  Begin braking once the
        # terminal is horizontally reached and no upward step is still needed;
        # the calculator still has to prove the landing and complete stop.
        approaching_terminal_descent = (
            target_index == len(route.points) - 1
            and distance <= 0.35
            and target.y <= state.position[1] + 0.15
        )
        horizontal_speed = math.hypot(
            state.velocity_blocks_per_tick[0],
            state.velocity_blocks_per_tick[2],
        ) * 20.0
        if continuation is None and (reached_terminal or approaching_terminal_descent):
            braking = True
        terminal_ready = (
            exit_requirement.accepts(state, MovementMode.WALK)
            if exit_requirement is not None else horizontal_speed <= 0.10
        )
        if braking and terminal_ready:
            plan = GroundTraversalPlan(
                route, _entry_window(route, profile, trajectory[0], exit=False),
                _entry_window(route, profile, trajectory[-1], exit=True),
                tuple(trajectory),
                tuple(inputs), tuple(events), corridor,
                tuple(sorted(dependencies)), len(inputs),
                JAVA_1_21_RULESET.ruleset_id,
                "mc2p.input-projection.v1", profile.profile_id, radius,
                surface_node_path,
            )
            return GroundTraversalResult(
                GroundTraversalStatus.VERIFIED, plan,
                dependencies=plan.dependencies,
            )
        if distance <= 1.0e-9 and not braking:
            return GroundTraversalResult(
                GroundTraversalStatus.BLOCKED,
                dependencies=tuple(sorted(dependencies)),
                reasons=("ground_traversal_did_not_reach_expected_height",),
            )
        movement_yaw = (
            state.yaw_radians if braking else math.atan2(-dx, dz)
        )
        projected = project_movement_command(
            state, MovementV1() if braking else MovementV1(forward=1),
            movement_yaw_radians=movement_yaw,
        )
        if projected.status is not ProjectionStatus.READY:
            return GroundTraversalResult(
                GroundTraversalStatus.UNSUPPORTED,
                dependencies=tuple(sorted(dependencies)),
                reasons=projected.reasons,
            )
        tick_input = projected.tick_input
        assert tick_input is not None
        calculated = step(state, tick_input, world, JAVA_1_21_RULESET)
        dependencies.update(calculated.dependencies)
        if calculated.status is CalculationStatus.NEEDS_WORLD:
            return GroundTraversalResult(
                GroundTraversalStatus.NEEDS_WORLD,
                dependencies=tuple(sorted(dependencies)),
                missing_cells=calculated.missing_cells,
                reasons=("ground_traversal_world_incomplete",),
            )
        if calculated.status is CalculationStatus.UNSUPPORTED:
            return GroundTraversalResult(
                GroundTraversalStatus.UNSUPPORTED,
                dependencies=tuple(sorted(dependencies)),
                reasons=calculated.unsupported_reasons,
            )
        if calculated.status is not CalculationStatus.OK:
            return GroundTraversalResult(
                GroundTraversalStatus.BLOCKED,
                dependencies=tuple(sorted(dependencies)),
                reasons=calculated.invalid_reasons or ("physics_step_failed",),
            )
        assert calculated.next_state is not None
        state = calculated.next_state
        if not _inside_corridor(state, corridor):
            return GroundTraversalResult(
                GroundTraversalStatus.BLOCKED,
                dependencies=tuple(sorted(dependencies)),
                reasons=("ground_traversal_left_corridor",),
            )
        inputs.append(tick_input)
        events.append(calculated.events)
        trajectory.append(state)
    return GroundTraversalResult(
        GroundTraversalStatus.BUDGET_EXHAUSTED,
        dependencies=tuple(sorted(dependencies)),
        reasons=("ground_traversal_tick_budget_exhausted",),
    )
