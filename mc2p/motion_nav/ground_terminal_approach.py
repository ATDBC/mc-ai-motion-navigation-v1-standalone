"""Physics-proved ordinary-ground connection from a surface into a goal."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
import math
import time

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.ground_traversal import (
    GroundTraversalExitRequirement, GroundTraversalPlan,
    GroundTraversalStatus, verify_ground_traversal,
)
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import PhysicsState, TickInput
from mc2p.motion_nav.segment_entry import SegmentEntryWindow
from mc2p.motion_nav.world_model import BlockPos


class GroundTerminalApproachStatus(StrEnum):
    FEASIBLE = "feasible"
    ALREADY_SATISFIED = "already_satisfied"
    ENTRY_UNPROVEN = "entry_unproven"
    NO_CONTROLLABLE_ROUTE = "no_controllable_route"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class GroundTerminalPlan:
    """The immutable calculator result, including the zero-input case."""

    route: FixedRoute
    traversal: GroundTraversalPlan | None
    trajectory: tuple[PhysicsState, ...]
    inputs: tuple[TickInput, ...]
    commands: tuple[MovementV1, ...]

    def __post_init__(self) -> None:
        if (type(self.route) is not FixedRoute
                or (self.traversal is not None
                    and type(self.traversal) is not GroundTraversalPlan)
                or type(self.trajectory) is not tuple or not self.trajectory
                or type(self.inputs) is not tuple
                or type(self.commands) is not tuple
                or len(self.inputs) != len(self.commands)
                or len(self.trajectory) != len(self.inputs) + 1):
            raise ContractViolation("terminal ground plan is incomplete")
        if self.traversal is None and (self.inputs or len(self.route.points) != 1):
            raise ContractViolation("only an already-satisfied plan may omit traversal")
        if self.traversal is not None and (
                self.route != self.traversal.route
                or self.inputs != self.traversal.inputs
                or self.trajectory != self.traversal.trajectory):
            raise ContractViolation("terminal plan differs from calculator proof")


@dataclass(frozen=True, slots=True)
class GroundTerminalApproach:
    approach_id: str
    route: FixedRoute
    entry_window: SegmentEntryWindow
    exit_requirement: GroundTraversalExitRequirement
    plan: GroundTerminalPlan
    cost_ticks: int
    dependencies: tuple[BlockPos, ...]
    candidate_kind: str

    def __post_init__(self) -> None:
        require_identifier(self.approach_id, "terminal approach id")
        require_identifier(self.candidate_kind, "terminal approach candidate kind")
        if (type(self.route) is not FixedRoute
                or type(self.entry_window) is not SegmentEntryWindow
                or type(self.exit_requirement) is not GroundTraversalExitRequirement
                or type(self.plan) is not GroundTerminalPlan
                or self.plan.route != self.route
                or type(self.cost_ticks) is not int or self.cost_ticks < 0
                or self.cost_ticks != len(self.plan.inputs)
                or type(self.dependencies) is not tuple
                or self.dependencies != tuple(sorted(set(self.dependencies)))):
            raise ContractViolation("terminal approach proof is inconsistent")


@dataclass(frozen=True, slots=True)
class GroundTerminalApproachResult:
    status: GroundTerminalApproachStatus
    approach: GroundTerminalApproach | None = None
    dependencies: tuple[BlockPos, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not GroundTerminalApproachStatus:
            raise ContractViolation("terminal approach result status must be typed")
        has_proof = self.status in {
            GroundTerminalApproachStatus.FEASIBLE,
            GroundTerminalApproachStatus.ALREADY_SATISFIED,
        }
        if has_proof != (type(self.approach) is GroundTerminalApproach):
            raise ContractViolation("terminal approach result proof differs from status")


def _movement_mode(state: PhysicsState) -> MovementMode | None:
    if state.pose != "standing" or state.sprinting or state.sneaking:
        return None
    if state.swimming or state.climbing or state.fall_flying or state.flying:
        return None
    return MovementMode.WALK


def _entry_window(state: PhysicsState, route: FixedRoute,
                  profile: GroundMotionProfile) -> SegmentEntryWindow:
    first = route.points[0]
    target = next((point for point in route.points[1:]
                   if math.hypot(point.x-first.x, point.z-first.z) > 1.0e-9), None)
    if target is None:
        direction = (0.0, 1.0)
    else:
        length = math.hypot(target.x-first.x, target.z-first.z)
        direction = ((target.x-first.x)/length, (target.z-first.z)/length)
    speed = math.hypot(state.velocity_blocks_per_tick[0],
                       state.velocity_blocks_per_tick[2]) * 20.0
    return SegmentEntryWindow(
        state.position, direction, -.05, .05, .05,
        state.position[1]-.05, state.position[1]+.05,
        max(0.0, speed-.10),
        min(profile.maximum_speed_blocks_per_second+.10, speed+.10),
        math.radians(35.0), frozenset({"standing"}),
        frozenset({MovementMode.WALK}), None, None, profile.profile_id,
    )


def _candidate_routes(start: tuple[float, float, float],
                      target: tuple[float, float, float],
                      prefix: str) -> tuple[tuple[str, FixedRoute], ...]:
    sx, sy, sz = start
    tx, ty, tz = target
    raw = (
        ("straight", ((sx, sy, sz), (tx, ty, tz))),
        ("x_then_z", ((sx, sy, sz), (tx, ty, sz), (tx, ty, tz))),
        ("z_then_x", ((sx, sy, sz), (sx, ty, tz), (tx, ty, tz))),
    )
    found = []
    identities = set()
    for kind, values in raw:
        points = []
        for value in values:
            if not points or math.dist(points[-1], value) > 1.0e-9:
                points.append(value)
        identity = tuple(points)
        if len(points) < 2 or identity in identities:
            continue
        identities.add(identity)
        found.append((kind, FixedRoute(
            f"{prefix}-{kind}", tuple(RoutePoint(*value) for value in points),
        )))
    return tuple(found)


def _commands(inputs: tuple[TickInput, ...]) -> tuple[MovementV1, ...]:
    return tuple(MovementV1(
        forward=int(round(value.forward)),
        strafe=int(round(value.strafe)),
        jump=value.jump,
        sneak=value.sneak,
        sprint=value.sprint,
    ) for value in inputs)


def _identity(kind: str, route: FixedRoute, entry: SegmentEntryWindow,
              exit_requirement: GroundTraversalExitRequirement,
              dependencies: tuple[BlockPos, ...], cost_ticks: int) -> str:
    payload = {
        "kind": kind,
        "route": [(point.x, point.y, point.z) for point in route.points],
        "entry": repr(entry),
        "exit": repr(exit_requirement),
        "dependencies": dependencies,
        "cost_ticks": cost_ticks,
    }
    digest = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()[:24]
    return f"terminal-{digest}"


def solve_ground_terminal_approach(
    *,
    entry_state: PhysicsState,
    entry_window: SegmentEntryWindow | None,
    completion_region: GroundCompletionRegion,
    exit_requirement: GroundTraversalExitRequirement,
    world: PhysicsWorldView,
    profile: GroundMotionProfile,
    maximum_ticks: int,
    deadline_ns: int | None = None,
    route_id_prefix: str = "terminal-approach",
) -> GroundTerminalApproachResult:
    """Choose the cheapest proved straight or cardinal-L terminal route."""
    if (type(entry_state) is not PhysicsState
            or (entry_window is not None and type(entry_window) is not SegmentEntryWindow)
            or type(completion_region) is not GroundCompletionRegion
            or type(exit_requirement) is not GroundTraversalExitRequirement
            or exit_requirement.completion_region != completion_region
            or type(world) is not PhysicsWorldView
            or type(profile) is not GroundMotionProfile
            or type(maximum_ticks) is not int or maximum_ticks < 1
            or (deadline_ns is not None and type(deadline_ns) is not int)):
        raise ContractViolation("terminal approach solver requires typed bounded inputs")
    mode = _movement_mode(entry_state)
    if mode is not MovementMode.WALK:
        return GroundTerminalApproachResult(
            GroundTerminalApproachStatus.ENTRY_UNPROVEN,
            reasons=("standing_walk_entry_required",),
        )
    if entry_window is not None and (
            entry_state.pose not in entry_window.allowed_poses
            or MovementMode.WALK not in entry_window.allowed_modes):
        return GroundTerminalApproachResult(
            GroundTerminalApproachStatus.ENTRY_UNPROVEN,
            reasons=("entry_window_does_not_admit_standing_walk",),
        )
    if exit_requirement.accepts(entry_state, mode):
        point = RoutePoint(*entry_state.position)
        route = FixedRoute(f"{route_id_prefix}-satisfied", (point,))
        window = entry_window or _entry_window(entry_state,
            FixedRoute(f"{route_id_prefix}-entry", (point, RoutePoint(
                point.x, point.y, point.z + 1.0))), profile)
        plan = GroundTerminalPlan(route, None, (entry_state,), (), ())
        dependencies = completion_region.dependencies
        approach = GroundTerminalApproach(
            _identity("already_satisfied", route, window, exit_requirement,
                      dependencies, 0),
            route, window, exit_requirement, plan, 0, dependencies,
            "already_satisfied",
        )
        return GroundTerminalApproachResult(
            GroundTerminalApproachStatus.ALREADY_SATISFIED,
            approach, dependencies,
        )

    results = []
    dependencies: set[BlockPos] = set(completion_region.dependencies)
    missing: set[BlockPos] = set()
    statuses = []
    for order, (kind, route) in enumerate(_candidate_routes(
            entry_state.position, completion_region.reference_point,
            route_id_prefix)):
        if deadline_ns is not None and time.perf_counter_ns() >= deadline_ns:
            return GroundTerminalApproachResult(
                GroundTerminalApproachStatus.TIMEOUT,
                dependencies=tuple(sorted(dependencies)),
                missing_cells=tuple(sorted(missing)),
                reasons=("terminal_approach_deadline",),
            )
        result = verify_ground_traversal(
            entry_state, route, world, profile,
            maximum_ticks=maximum_ticks,
            exit_requirement=exit_requirement,
        )
        statuses.append(result.status)
        dependencies.update(result.dependencies)
        missing.update(result.missing_cells)
        if result.status is not GroundTraversalStatus.VERIFIED:
            continue
        assert result.plan is not None
        window = entry_window or result.plan.entry_window
        plan = GroundTerminalPlan(
            route, result.plan, result.plan.trajectory, result.plan.inputs,
            _commands(result.plan.inputs),
        )
        proof_dependencies = tuple(sorted(
            set(result.plan.dependencies) | set(completion_region.dependencies)
        ))
        approach = GroundTerminalApproach(
            _identity(kind, route, window, exit_requirement,
                      proof_dependencies, result.plan.estimated_ticks),
            route, window, exit_requirement, plan,
            result.plan.estimated_ticks, proof_dependencies, kind,
        )
        results.append((approach.cost_ticks, order,
                        tuple((p.x, p.y, p.z) for p in route.points), approach))
    if results:
        results.sort(key=lambda value: value[:-1])
        approach = results[0][-1]
        return GroundTerminalApproachResult(
            GroundTerminalApproachStatus.FEASIBLE,
            approach, approach.dependencies,
        )
    if GroundTraversalStatus.NEEDS_WORLD in statuses:
        status = GroundTerminalApproachStatus.UNKNOWN
    elif GroundTraversalStatus.BUDGET_EXHAUSTED in statuses:
        status = GroundTerminalApproachStatus.TIMEOUT
    elif GroundTraversalStatus.UNSUPPORTED in statuses:
        status = GroundTerminalApproachStatus.UNSUPPORTED
    else:
        status = GroundTerminalApproachStatus.NO_CONTROLLABLE_ROUTE
    return GroundTerminalApproachResult(
        status, dependencies=tuple(sorted(dependencies)),
        missing_cells=tuple(sorted(missing)),
        reasons=("no_proved_terminal_route",),
    )


__all__ = [
    "GroundTerminalApproach", "GroundTerminalApproachResult",
    "GroundTerminalApproachStatus", "GroundTerminalPlan",
    "GroundTraversalExitRequirement", "solve_ground_terminal_approach",
]
