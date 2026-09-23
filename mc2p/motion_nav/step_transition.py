"""Observed-state walking transition between adjacent support surfaces."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
from pathlib import Path
import time

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_motion import GroundControl, control_world_direction
from mc2p.motion_nav.environment_identity import MotionEnvironmentIdentity
from mc2p.motion_nav.movement_transition import MovementTransition, ResourceChange
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.support_surfaces import SupportSurface, SurfaceNodeId
from mc2p.motion_nav.world_model import Aabb, BlockPos, WorldView


_EPSILON = 1.0e-9


def _finite(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ContractViolation(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class StepProfile:
    profile_id: str
    environment_id: str
    maximum_up_height_blocks: float
    maximum_down_height_blocks: float
    maximum_entry_speed_blocks_per_second: float
    maximum_exit_speed_blocks_per_second: float
    target_horizontal_radius_blocks: float
    target_level_tolerance_blocks: float
    maximum_ticks: int
    cost_seconds: float
    minecraft_version: str = "1.21"
    tick_seconds: float = .05

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "Step profile id")
        require_identifier(self.environment_id, "Step environment id")
        if self.minecraft_version != "1.21":
            raise ContractViolation("Step profile Minecraft version is unsupported")
        for name in (
            "maximum_up_height_blocks", "maximum_down_height_blocks",
            "maximum_entry_speed_blocks_per_second",
            "maximum_exit_speed_blocks_per_second",
            "target_horizontal_radius_blocks", "target_level_tolerance_blocks",
            "cost_seconds",
            "tick_seconds",
        ):
            if _finite(getattr(self, name), name) <= 0:
                raise ContractViolation(f"{name} must be positive")
        if type(self.maximum_ticks) is not int or self.maximum_ticks < 1:
            raise ContractViolation("maximum_ticks must be a positive tick count")


def load_step_profile(path: Path, *,
                      environment: MotionEnvironmentIdentity) -> StepProfile:
    if not isinstance(path, Path) or type(environment) is not MotionEnvironmentIdentity:
        raise ContractViolation("Step profile requires a path and environment")
    try:
        document = json.loads(path.read_text("utf-8"))
        if document.get("schema_version") != "mc2p.step-profile.v1":
            raise ContractViolation("unsupported Step profile schema")
        scope = document["scope"]
        limits = document["limits"]
        environment.require_profile_environment(scope["environment_id"])
        if scope["minecraft_version"] != environment.minecraft_version:
            raise ContractViolation("Step Minecraft version does not match environment")
        if abs(float(scope["tick_seconds"]) - environment.tick_seconds) > 1.0e-12:
            raise ContractViolation("Step tick duration does not match environment")
        return StepProfile(
            document["profile_id"], scope["environment_id"],
            limits["maximum_up_height_blocks"],
            limits["maximum_down_height_blocks"],
            limits["maximum_entry_speed_blocks_per_second"],
            limits["maximum_exit_speed_blocks_per_second"],
            limits["target_horizontal_radius_blocks"],
            limits["target_level_tolerance_blocks"],
            limits["maximum_ticks"], document["cost_seconds"],
            scope["minecraft_version"], scope["tick_seconds"],
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ContractViolation("Step profile is invalid") from error


@dataclass(frozen=True, slots=True)
class StepQuery:
    status: QueryStatus
    direction: str
    height_delta_blocks: float
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...] = ()
    reason_code: str = ""


@dataclass(frozen=True, slots=True)
class StepEdge:
    start: SurfaceNodeId
    end: SurfaceNodeId
    profile_id: str
    direction: str
    cost_seconds: float
    dependencies: tuple[BlockPos, ...]
    transition: MovementTransition | None = None

    def __post_init__(self) -> None:
        if type(self.start) is not SurfaceNodeId or type(self.end) is not SurfaceNodeId:
            raise ContractViolation("Step edge requires surface node ids")
        require_identifier(self.profile_id, "Step edge profile id")
        if self.direction not in {"up", "down"}:
            raise ContractViolation("Step edge direction must be up or down")
        if _finite(self.cost_seconds, "Step edge cost") <= 0:
            raise ContractViolation("Step edge cost must be positive")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("Step edge dependencies must be immutable")
        if self.transition is not None and type(self.transition) is not MovementTransition:
            raise ContractViolation("Step edge transition must be typed")

    @property
    def resource_change(self) -> ResourceChange:
        return (self.transition.resource_change
                if self.transition is not None else ResourceChange())


def _merge_status(current: QueryStatus, incoming: QueryStatus) -> QueryStatus:
    order = {
        QueryStatus.FEASIBLE: 0,
        QueryStatus.NEEDS_INFORMATION: 1,
        QueryStatus.UNSUPPORTED: 2,
        QueryStatus.BLOCKED: 3,
    }
    return incoming if order[incoming] > order[current] else current


def _surface_body(surface: SupportSurface) -> Aabb:
    x, y, z = surface.position
    return Aabb(x - .3, y, z - .3, x + .3, y + 1.8, z + .3)


def query_step(world: WorldView, start: SupportSurface, end: SupportSurface,
               profile: StepProfile) -> StepQuery:
    """Check a walking step using the same clearance and support queries as motion."""
    if (type(world) is not WorldView or type(start) is not SupportSurface
            or type(end) is not SupportSurface or type(profile) is not StepProfile):
        raise ContractViolation("Step query requires world, surfaces and profile")
    dx = end.node_id.column_x - start.node_id.column_x
    dz = end.node_id.column_z - start.node_id.column_z
    delta_y = end.position[1] - start.position[1]
    direction = "up" if delta_y > _EPSILON else "down" if delta_y < -_EPSILON else "level"
    base_dependencies = set(start.dependencies) | set(end.dependencies)
    horizontal_distance = math.hypot(
        end.position[0] - start.position[0],
        end.position[2] - start.position[2],
    )
    if (abs(dx) + abs(dz) > 1 or horizontal_distance <= _EPSILON
            or horizontal_distance > 1.5 + _EPSILON or direction == "level"):
        return StepQuery(
            QueryStatus.UNSUPPORTED, direction, delta_y,
            tuple(sorted(base_dependencies)), (), "unsupported_step_relation",
        )
    if ((delta_y > 0 and delta_y > profile.maximum_up_height_blocks + _EPSILON)
            or (delta_y < 0 and -delta_y > profile.maximum_down_height_blocks + _EPSILON)):
        return StepQuery(
            QueryStatus.UNSUPPORTED, direction, delta_y,
            tuple(sorted(base_dependencies)), (), "step_height_outside_profile",
        )

    body = _surface_body(start)
    horizontal = (end.position[0] - start.position[0], 0.0,
                  end.position[2] - start.position[2])
    moves = ((0.0, delta_y, 0.0), horizontal) if delta_y > 0 else (
        horizontal, (0.0, delta_y, 0.0),
    )
    status = QueryStatus.FEASIBLE
    dependencies = set(base_dependencies)
    missing: set[BlockPos] = set()
    for move in moves:
        result = sweep(body, move, world)
        dependencies.update(result.dependencies)
        missing.update(result.missing_cells)
        status = _merge_status(status, result.status)
        body = body.moved(*move)
    support = query_support(body, world)
    dependencies.update(support.dependencies)
    missing.update(support.missing_cells)
    status = _merge_status(status, support.status)
    if support.status is QueryStatus.FEASIBLE and support.support_fraction < .5:
        status = _merge_status(status, QueryStatus.UNSUPPORTED)
    reason = {
        QueryStatus.FEASIBLE: "step_feasible",
        QueryStatus.BLOCKED: "step_blocked",
        QueryStatus.NEEDS_INFORMATION: "step_needs_information",
        QueryStatus.UNSUPPORTED: "step_unsupported",
    }[status]
    return StepQuery(status, direction, delta_y, tuple(sorted(dependencies)),
                     tuple(sorted(missing)), reason)


class StepState(StrEnum):
    IDLE = "idle"
    PREPARE = "prepare"
    MOVING = "moving"
    VERIFY_LANDING = "verify_landing"
    CANCELLING = "cancelling"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    INPUT_LOST = "input_lost"
    FAILED = "failed"
    NEEDS_INFORMATION = "needs_information"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class StepDecision:
    state: StepState
    movement: MovementV1
    input_lease_ticks: int
    missing_cells: tuple[BlockPos, ...]
    reason_code: str
    control_time_ns: int


class StepController:
    """Track one adjacent step from observations; elapsed ticks only bound failure."""

    _TERMINAL = {
        StepState.COMPLETE, StepState.CANCELLED, StepState.INPUT_LOST,
        StepState.FAILED, StepState.NEEDS_INFORMATION, StepState.UNSUPPORTED,
        StepState.BLOCKED,
    }

    def __init__(self, profile: StepProfile) -> None:
        if type(profile) is not StepProfile:
            raise ContractViolation("Step controller requires a Step profile")
        self.profile = profile
        self.state = StepState.IDLE
        self._start: SupportSurface | None = None
        self._end: SupportSurface | None = None
        self._session = None
        self._ticks = 0
        self._cancel_requested = False
        self._input_lost = False
        self._dependencies: tuple[BlockPos, ...] = ()

    def start(self, start: SupportSurface, end: SupportSurface,
              frame: NavigationFrame) -> None:
        if (type(start) is not SupportSurface or type(end) is not SupportSurface
                or type(frame) is not NavigationFrame):
            raise ContractViolation("Step start requires surfaces and navigation frame")
        if self.state in {StepState.PREPARE, StepState.MOVING,
                          StepState.VERIFY_LANDING, StepState.CANCELLING}:
            raise ContractViolation("active Step must finish or cancel before restart")
        self._start, self._end = start, end
        self._session = frame.session
        self._ticks = 0
        self._cancel_requested = False
        self._input_lost = False
        self._dependencies = ()
        self.state = StepState.PREPARE

    def cancel(self) -> None:
        if self.state not in self._TERMINAL | {StepState.IDLE}:
            self._cancel_requested = True

    @staticmethod
    def _decision(state: StepState, movement: MovementV1, reason: str,
                  started: int, missing: tuple[BlockPos, ...] = ()) -> StepDecision:
        return StepDecision(state, movement, 1, missing, reason,
                            time.perf_counter_ns() - started)

    @staticmethod
    def _speed(frame: NavigationFrame) -> float:
        vx, _, vz = frame.body.velocity_blocks_per_second
        return math.hypot(vx, vz)

    def _target_reached(self, frame: NavigationFrame) -> bool:
        assert self._end is not None
        x, y, z = self._end.position
        return (
            frame.body.is_on_ground
            and math.hypot(frame.body.position[0] - x,
                           frame.body.position[2] - z)
            <= self.profile.target_horizontal_radius_blocks
            and abs(frame.body.position[1] - y)
            <= self.profile.target_level_tolerance_blocks
            and query_support(frame.body.body_box, frame.world).status
            is QueryStatus.FEASIBLE
        )

    def _movement(self, frame: NavigationFrame) -> MovementV1:
        assert self._end is not None
        dx = self._end.position[0] - frame.body.position[0]
        dz = self._end.position[2] - frame.body.position[2]
        if math.hypot(dx, dz) <= _EPSILON:
            return MovementV1()
        candidates = tuple(
            MovementV1(forward=forward, strafe=strafe)
            for forward in (-1, 0, 1) for strafe in (-1, 0, 1)
            if forward or strafe
        )

        def alignment(movement: MovementV1) -> float:
            world_x, world_z = control_world_direction(GroundControl(
                movement.forward, -movement.strafe, frame.body.yaw_radians,
            ))
            return world_x * dx + world_z * dz

        return max(candidates, key=alignment)

    def _body_is_on_start_surface(self, frame: NavigationFrame) -> bool:
        assert self._start is not None
        region = self._start.region
        x, y, z = frame.body.position
        return (
            abs(y - self._start.position[1]) <= .1
            and region.min_x - _EPSILON <= x <= region.max_x + _EPSILON
            and region.min_z - _EPSILON <= z <= region.max_z + _EPSILON
        )

    def _apply_geometry_result(self, geometry: StepQuery, started: int) -> StepDecision | None:
        self._dependencies = geometry.dependencies
        if geometry.status is QueryStatus.FEASIBLE:
            return None
        self.state = {
            QueryStatus.NEEDS_INFORMATION: StepState.NEEDS_INFORMATION,
            QueryStatus.UNSUPPORTED: StepState.UNSUPPORTED,
            QueryStatus.BLOCKED: StepState.BLOCKED,
        }[geometry.status]
        return self._decision(
            self.state, MovementV1(), geometry.reason_code, started,
            geometry.missing_cells,
        )

    def decide(self, frame: NavigationFrame, *, input_confirmed: bool = True) -> StepDecision:
        started = time.perf_counter_ns()
        if type(frame) is not NavigationFrame:
            raise ContractViolation("Step decision requires a navigation frame")
        if self._start is None or self._end is None or self.state is StepState.IDLE:
            return self._decision(StepState.IDLE, MovementV1(), "not_started", started)
        if (frame.session != self._session or frame.body.session != self._session
                or frame.world.session != self._session):
            self.state = StepState.FAILED
            return self._decision(self.state, MovementV1(),
                                  "world_session_changed", started)
        if self.state in self._TERMINAL:
            return self._decision(self.state, MovementV1(), self.state.value, started)
        speed = self._speed(frame)
        if not input_confirmed:
            self._input_lost = True
        if self._cancel_requested or self._input_lost or self.state is StepState.CANCELLING:
            if frame.body.is_on_ground and speed <= self.profile.maximum_exit_speed_blocks_per_second:
                self.state = (StepState.INPUT_LOST if self._input_lost
                              else StepState.CANCELLED)
                reason = ("input_lost_after_safe_stop" if self._input_lost
                          else "cancelled_after_stop")
                return self._decision(self.state, MovementV1(), reason, started)
            self.state = StepState.CANCELLING
            reason = ("waiting_for_safe_stop_after_input_loss" if self._input_lost
                      else "cancelling")
            return self._decision(self.state, MovementV1(), reason, started)

        if (self.state in {StepState.MOVING, StepState.VERIFY_LANDING}
                and set(frame.changed_cells).intersection(self._dependencies)):
            geometry_decision = self._apply_geometry_result(
                query_step(frame.world, self._start, self._end, self.profile), started,
            )
            if geometry_decision is not None:
                return geometry_decision

        if self.state is StepState.PREPARE:
            geometry = query_step(frame.world, self._start, self._end, self.profile)
            geometry_decision = self._apply_geometry_result(geometry, started)
            if geometry_decision is not None:
                return geometry_decision
            if (frame.body.pose != "standing" or not frame.body.is_on_ground
                    or speed > self.profile.maximum_entry_speed_blocks_per_second):
                self.state = StepState.UNSUPPORTED
                return self._decision(self.state, MovementV1(),
                                      "invalid_entry_body", started)
            if not self._body_is_on_start_surface(frame):
                self.state = StepState.UNSUPPORTED
                return self._decision(
                    self.state, MovementV1(), "invalid_entry_surface", started,
                )
            self.state = StepState.MOVING

        self._ticks += 1
        if self._ticks > self.profile.maximum_ticks:
            self.state = StepState.FAILED
            return self._decision(self.state, MovementV1(), "step_timeout", started)
        assert self._start is not None and self._end is not None
        horizontal_error = math.hypot(
            frame.body.position[0] - self._end.position[0],
            frame.body.position[2] - self._end.position[2],
        )
        stepping_down = self._end.position[1] < self._start.position[1] - _EPSILON
        if (stepping_down and not frame.body.is_on_ground
                and (self.state is StepState.VERIFY_LANDING
                     or horizontal_error <= self.profile.target_horizontal_radius_blocks)):
            self.state = StepState.VERIFY_LANDING
            return self._decision(self.state, MovementV1(),
                                  "waiting_for_step_down_landing", started)
        if self._target_reached(frame):
            if speed <= self.profile.maximum_exit_speed_blocks_per_second:
                self.state = StepState.COMPLETE
                return self._decision(self.state, MovementV1(),
                                      "endpoint_observed", started)
            self.state = StepState.VERIFY_LANDING
            return self._decision(self.state, MovementV1(),
                                  "settling_at_endpoint", started)
        if self.state is StepState.VERIFY_LANDING:
            self.state = StepState.MOVING
        return self._decision(self.state, self._movement(frame), "step_tracking", started)
