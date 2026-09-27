"""Physics-backed proof for ordinary walking over known small height changes."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections import OrderedDict
import math

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.fixed_route import FixedRoute
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, PhysicsState, TickInput,
)
from mc2p.motion_nav.segment_entry import SegmentEntryWindow
from mc2p.motion_nav.world_model import Aabb, BlockPos


class GroundTraversalStatus(StrEnum):
    VERIFIED = "verified"
    NEEDS_WORLD = "needs_world"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"


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


def verify_ground_traversal(
    entry_state: PhysicsState,
    route: FixedRoute,
    world: PhysicsWorldView,
    profile: GroundMotionProfile,
    *,
    maximum_ticks: int,
) -> GroundTraversalResult:
    if (type(entry_state) is not PhysicsState or type(route) is not FixedRoute
            or type(world) is not PhysicsWorldView
            or type(profile) is not GroundMotionProfile
            or type(maximum_ticks) is not int or maximum_ticks < 1):
        raise ContractViolation("ground traversal verification requires typed bounded inputs")
    if entry_state.session != world.session:
        raise ContractViolation("ground traversal world belongs to another session")
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
            reasons=("ground_traversal_requires_cardinal_segments",),
        )
    if any(abs(second.y - first.y) > entry_state.step_height_blocks + 1.0e-9
           for first, second in zip(route.points, route.points[1:])):
        return GroundTraversalResult(
            GroundTraversalStatus.UNSUPPORTED,
            reasons=("support_height_change_exceeds_step_rule",),
        )

    radius = 0.45
    corridor = _corridor(route, radius)
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
        reached_terminal = (
            target_index == len(route.points) - 1
                and distance <= 0.35
                and abs(state.position[1] - target.y) <= 0.10
                and state.on_ground
        )
        horizontal_speed = math.hypot(
            state.velocity_blocks_per_tick[0],
            state.velocity_blocks_per_tick[2],
        ) * 20.0
        if reached_terminal:
            braking = True
        if braking and horizontal_speed <= 0.10:
            plan = GroundTraversalPlan(
                route, _entry_window(route, profile, trajectory[0], exit=False),
                _entry_window(route, profile, trajectory[-1], exit=True),
                tuple(trajectory),
                tuple(inputs), tuple(events), corridor,
                tuple(sorted(dependencies)), len(inputs),
                JAVA_1_21_RULESET.ruleset_id,
                "mc2p.input-projection.v1", profile.profile_id, radius,
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
