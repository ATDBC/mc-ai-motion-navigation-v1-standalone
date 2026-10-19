"""Calibrated adjacent one-block JumpUp capability and observed-state executor."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import math
from pathlib import Path

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.block_motion_traits import (
    BlockMotionCatalog, unsupported_motion_cells,
)
from mc2p.motion_nav.environment_identity import MotionEnvironmentIdentity
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_motion import GroundControl, control_world_direction
from mc2p.motion_nav.movement_transition import MovementTransition, ResourceChange
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import Aabb, BlockPos, WorldView


JumpNodeId = tuple[int, int, int]


def _finite(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ContractViolation(f"{name} must be finite")
    return float(value)


def _node(value: JumpNodeId, name: str) -> None:
    if type(value) is not tuple or len(value) != 3 or any(type(item) is not int for item in value):
        raise ContractViolation(f"{name} must be an integer triple")


@dataclass(frozen=True, slots=True)
class JumpUpProfile:
    profile_id: str
    minecraft_version: str
    tick_seconds: float
    support_materials: frozenset[str]
    maximum_entry_speed_blocks_per_second: float
    entry_center_tolerance_blocks: float
    maximum_forward_offset_blocks: float
    maximum_backward_offset_blocks: float
    maximum_lateral_offset_blocks: float
    maximum_yaw_error_degrees: float
    horizontal_safety_margin_blocks: float
    reference_positions: tuple[tuple[float, float, float], ...]
    forward_release_progress_blocks: float
    maximum_takeoff_wait_ticks: int
    maximum_airborne_ticks: int
    landing_horizontal_radius_blocks: float
    landing_level_tolerance_blocks: float
    maximum_exit_speed_blocks_per_second: float
    maximum_settle_ticks: int
    cost_seconds: float
    environment_id: str = "legacy-unbound"
    ground_model_id: str | None = None
    motion_catalog: BlockMotionCatalog | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "JumpUp profile id")
        require_identifier(self.environment_id, "JumpUp environment id")
        if self.ground_model_id is not None:
            require_identifier(self.ground_model_id, "JumpUp ground model id")
        if self.motion_catalog is not None and type(self.motion_catalog) is not BlockMotionCatalog:
            raise ContractViolation("JumpUp motion catalog must be typed")
        if (self.motion_catalog is None) != (self.ground_model_id is None):
            raise ContractViolation("JumpUp ground model and motion catalog must be declared together")
        if self.minecraft_version != "1.21":
            raise ContractViolation("JumpUp profile Minecraft version is unsupported")
        if type(self.support_materials) is not frozenset or not self.support_materials:
            raise ContractViolation("JumpUp support materials must be a nonempty frozenset")
        for material in self.support_materials:
            require_identifier(material, "JumpUp support material")
        positive = (
            "tick_seconds", "maximum_entry_speed_blocks_per_second",
            "entry_center_tolerance_blocks", "maximum_yaw_error_degrees",
            "maximum_backward_offset_blocks", "maximum_lateral_offset_blocks",
            "landing_horizontal_radius_blocks",
            "landing_level_tolerance_blocks", "maximum_exit_speed_blocks_per_second",
            "cost_seconds",
        )
        for name in positive:
            if _finite(getattr(self, name), name) <= 0:
                raise ContractViolation(f"{name} must be positive")
        if _finite(self.horizontal_safety_margin_blocks, "horizontal safety margin") < 0:
            raise ContractViolation("horizontal safety margin must be nonnegative")
        if _finite(self.maximum_forward_offset_blocks, "maximum forward offset") < 0:
            raise ContractViolation("maximum forward offset must be nonnegative")
        if _finite(self.forward_release_progress_blocks, "forward release progress") <= 0:
            raise ContractViolation("forward release progress must be positive")
        for name in ("maximum_takeoff_wait_ticks", "maximum_airborne_ticks", "maximum_settle_ticks"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ContractViolation(f"{name} must be a positive tick count")
        if (type(self.reference_positions) is not tuple or len(self.reference_positions) < 2
                or any(type(point) is not tuple or len(point) != 3 for point in self.reference_positions)):
            raise ContractViolation("JumpUp reference trajectory must contain immutable 3D points")
        for point in self.reference_positions:
            for value in point:
                _finite(value, "JumpUp reference position")
        if self.reference_positions[0] != (0.0, 0.0, 0.0):
            raise ContractViolation("JumpUp reference trajectory must start at the origin")
        if abs(self.reference_positions[-1][1] - 1.0) > self.landing_level_tolerance_blocks:
            raise ContractViolation("JumpUp reference trajectory must land one block up")


def load_jump_up_profile(
    path: Path,
    *,
    environment: MotionEnvironmentIdentity | None = None,
    catalog: BlockMotionCatalog | None = None,
) -> JumpUpProfile:
    if not isinstance(path, Path):
        raise ContractViolation("JumpUp profile path must be a Path")
    value = json.loads(path.read_text("utf-8"))
    schema = value.get("schema_version")
    if schema not in {"mc2p.jump-up-profile.v1", "mc2p.jump-up-profile.v2"}:
        raise ContractViolation("unsupported JumpUp profile schema")
    scope, entry, trajectory, landing = (
        value["scope"], value["entry"], value["trajectory"], value["landing"]
    )
    if schema == "mc2p.jump-up-profile.v2":
        if type(environment) is not MotionEnvironmentIdentity or type(catalog) is not BlockMotionCatalog:
            raise ContractViolation("JumpUp v2 profile requires environment and block catalog")
        environment.require_profile_environment(scope["environment_id"])
        if scope["minecraft_version"] != environment.minecraft_version:
            raise ContractViolation("JumpUp Minecraft version does not match environment")
        model_id = scope["ground_model_id"]
        support_materials = catalog.materials_for_ground_model(model_id)
        environment_id = scope["environment_id"]
    else:
        model_id = None
        support_materials = frozenset(scope["support_materials"])
        environment_id = "legacy-unbound"
    return JumpUpProfile(
        value["profile_id"], scope["minecraft_version"], scope["tick_seconds"],
        support_materials,
        entry["maximum_horizontal_speed_blocks_per_second"],
        entry["center_tolerance_blocks"],
        entry["maximum_forward_offset_blocks"],
        entry["maximum_backward_offset_blocks"],
        entry["maximum_lateral_offset_blocks"],
        entry["maximum_yaw_error_degrees"],
        trajectory["horizontal_safety_margin_blocks"],
        tuple(tuple(float(axis) for axis in point) for point in trajectory["reference_positions"]),
        trajectory["forward_release_progress_blocks"],
        trajectory["maximum_takeoff_wait_ticks"],
        trajectory["maximum_airborne_ticks"],
        landing["horizontal_radius_blocks"], landing["level_tolerance_blocks"],
        landing["maximum_exit_speed_blocks_per_second"],
        landing["maximum_settle_ticks"], value["cost_seconds"],
        environment_id, model_id, catalog if schema == "mc2p.jump-up-profile.v2" else None,
    )


@dataclass(frozen=True, slots=True)
class JumpUpQuery:
    status: QueryStatus
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...] = ()
    reason_code: str = ""


@dataclass(frozen=True, slots=True)
class JumpUpEdge:
    start: JumpNodeId
    end: JumpNodeId
    profile_id: str
    direction: tuple[int, int]
    cost_seconds: float
    dependencies: tuple[BlockPos, ...]
    transition: MovementTransition | None = None

    def __post_init__(self) -> None:
        _node(self.start, "JumpUp edge start")
        _node(self.end, "JumpUp edge end")
        require_identifier(self.profile_id, "JumpUp edge profile id")
        if (self.end[1] - self.start[1] != 1
                or abs(self.end[0] - self.start[0]) + abs(self.end[2] - self.start[2]) != 1):
            raise ContractViolation("JumpUp edge must rise one block to a horizontal neighbor")
        expected = (self.end[0] - self.start[0], self.end[2] - self.start[2])
        if self.direction != expected:
            raise ContractViolation("JumpUp edge direction does not match its nodes")
        if _finite(self.cost_seconds, "JumpUp edge cost") <= 0:
            raise ContractViolation("JumpUp edge cost must be positive")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("JumpUp edge dependencies must be immutable")
        if self.transition is not None and type(self.transition) is not MovementTransition:
            raise ContractViolation("JumpUp edge transition must be typed")

    @property
    def resource_change(self) -> ResourceChange:
        return self.transition.resource_change if self.transition is not None else ResourceChange()


def _body_at(node: JumpNodeId, margin: float = 0.0) -> Aabb:
    x, y, z = node
    return Aabb(x + .2 - margin, float(y), z + .2 - margin,
                x + .8 + margin, float(y) + 1.8, z + .8 + margin)


def _merge_status(current: QueryStatus, incoming: QueryStatus) -> QueryStatus:
    order = {
        QueryStatus.FEASIBLE: 0,
        QueryStatus.NEEDS_INFORMATION: 1,
        QueryStatus.UNSUPPORTED: 2,
        QueryStatus.BLOCKED: 3,
    }
    return incoming if order[incoming] > order[current] else current


def query_jump_up(world: WorldView, start: JumpNodeId, end: JumpNodeId,
                  profile: JumpUpProfile) -> JumpUpQuery:
    if type(world) is not WorldView or type(profile) is not JumpUpProfile:
        raise ContractViolation("JumpUp query requires a world view and profile")
    _node(start, "JumpUp start"); _node(end, "JumpUp end")
    dx, dy, dz = end[0] - start[0], end[1] - start[1], end[2] - start[2]
    if dy != 1 or abs(dx) + abs(dz) != 1:
        return JumpUpQuery(QueryStatus.UNSUPPORTED, (), (), "unsupported_relation")

    dependencies: set[BlockPos] = set()
    missing: set[BlockPos] = set()
    status = QueryStatus.FEASIBLE

    for label, body in (("start", _body_at(start)), ("landing", _body_at(end))):
        support = query_support(body, world)
        dependencies.update(support.dependencies)
        missing.update(support.missing_cells)
        status = _merge_status(status, support.status)
        if (profile.motion_catalog is not None and profile.ground_model_id is not None
                and unsupported_motion_cells(
                    profile.motion_catalog, world, support.dependencies,
                    profile.ground_model_id,
                )):
            status = _merge_status(status, QueryStatus.UNSUPPORTED)
        if (support.status is QueryStatus.FEASIBLE
                and (support.support_fraction < .999
                     or not set(support.support_materials).issubset(profile.support_materials))):
            status = _merge_status(status, QueryStatus.UNSUPPORTED)

    base = _body_at(start, profile.horizontal_safety_margin_blocks)
    previous = profile.reference_positions[0]
    for index, point in enumerate(profile.reference_positions[1:], start=1):
        # Reference X is lateral, reference Z is forward. Rotate forward onto
        # the requested cardinal direction and lateral onto its right-hand axis.
        lateral, rise, forward = point
        previous_lateral, previous_rise, previous_forward = previous
        target_rise = rise + (1.0e-6 if index == len(profile.reference_positions) - 1 else 0.0)
        source_rise = previous_rise
        horizontal = (
            dx * (forward - previous_forward) + dz * (lateral - previous_lateral),
            0.0,
            dz * (forward - previous_forward) - dx * (lateral - previous_lateral),
        )
        # Minecraft resolves the vertical component before horizontal collision.
        # Preserve that order; a straight chord falsely intersects the target
        # platform while the player is already rising past its upper corner.
        for move in ((0.0, target_rise - source_rise, 0.0), horizontal):
            result = sweep(base, move, world)
            dependencies.update(result.dependencies)
            missing.update(result.missing_cells)
            status = _merge_status(status, result.status)
            if (profile.motion_catalog is not None and profile.ground_model_id is not None
                    and unsupported_motion_cells(
                        profile.motion_catalog, world, result.dependencies,
                        profile.ground_model_id,
                    )):
                status = _merge_status(status, QueryStatus.UNSUPPORTED)
            base = base.moved(*move)
        previous = point
    reason = {
        QueryStatus.FEASIBLE: "jump_up_feasible",
        QueryStatus.BLOCKED: "jump_up_blocked",
        QueryStatus.NEEDS_INFORMATION: "jump_up_needs_information",
        QueryStatus.UNSUPPORTED: "jump_up_unsupported",
    }[status]
    return JumpUpQuery(status, tuple(sorted(dependencies)), tuple(sorted(missing)), reason)


class JumpUpState(StrEnum):
    IDLE = "idle"
    PREPARE = "prepare"
    REQUEST_TAKEOFF = "request_takeoff"
    AIRBORNE = "airborne"
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
class JumpUpDecision:
    state: JumpUpState
    movement: MovementV1
    look: LookV1 | None
    input_lease_ticks: int
    missing_cells: tuple[BlockPos, ...]
    reason_code: str
    control_time_ns: int


class JumpUpController:
    """Advance JumpUp only from observed body state; ticks provide bounds only."""

    def __init__(self, profile: JumpUpProfile) -> None:
        if type(profile) is not JumpUpProfile:
            raise ContractViolation("JumpUp controller requires a calibrated profile")
        self.profile = profile
        self.state = JumpUpState.IDLE
        self._start: JumpNodeId | None = None
        self._end: JumpNodeId | None = None
        self._session = None
        self._takeoff_wait = 0
        self._airborne_ticks = 0
        self._settle_ticks = 0
        self._takeoff_observed = False
        self._cancel_requested = False
        self._input_lost = False

    def start(self, start: JumpNodeId, end: JumpNodeId, frame: NavigationFrame) -> None:
        if type(frame) is not NavigationFrame:
            raise ContractViolation("JumpUp start requires a navigation frame")
        if self.state in {
            JumpUpState.PREPARE, JumpUpState.REQUEST_TAKEOFF,
            JumpUpState.AIRBORNE, JumpUpState.VERIFY_LANDING,
            JumpUpState.CANCELLING,
        }:
            raise ContractViolation("active JumpUp must finish or cancel before restart")
        _node(start, "JumpUp start"); _node(end, "JumpUp end")
        self._start, self._end = start, end
        self._session = frame.session
        self.state = JumpUpState.PREPARE
        self._takeoff_wait = self._airborne_ticks = self._settle_ticks = 0
        self._takeoff_observed = False
        self._cancel_requested = False
        self._input_lost = False

    def cancel(self) -> None:
        if self.state not in {JumpUpState.IDLE, JumpUpState.COMPLETE,
                              JumpUpState.CANCELLED, JumpUpState.FAILED}:
            self._cancel_requested = True

    def _movement(self, frame: NavigationFrame, *, jump: bool = False,
                  forward: bool = True) -> MovementV1:
        assert self._start is not None and self._end is not None
        dx, dz = self._end[0] - self._start[0], self._end[2] - self._start[2]
        if not forward:
            return MovementV1(jump=jump)
        candidates = tuple(
            MovementV1(forward=forward_axis, strafe=strafe_axis, jump=jump)
            for forward_axis in (-1, 0, 1) for strafe_axis in (-1, 0, 1)
            if forward_axis or strafe_axis
        )
        def alignment(movement: MovementV1) -> float:
            world_x, world_z = control_world_direction(GroundControl(
                movement.forward, -movement.strafe, frame.body.yaw_radians,
            ))
            return world_x * dx + world_z * dz
        return max(candidates, key=alignment)

    @staticmethod
    def _decision(state: JumpUpState, movement: MovementV1, reason: str,
                  started: int, missing: tuple[BlockPos, ...] = (),
                  look: LookV1 | None = None) -> JumpUpDecision:
        import time
        return JumpUpDecision(state, movement, look, 1, missing, reason,
                              time.perf_counter_ns() - started)

    def _landing_valid(self, frame: NavigationFrame) -> bool:
        assert self._end is not None
        target_x, target_y, target_z = self._end[0] + .5, float(self._end[1]), self._end[2] + .5
        horizontal = math.hypot(frame.body.position[0] - target_x,
                                frame.body.position[2] - target_z)
        support = query_support(frame.body.body_box, frame.world)
        return (frame.body.is_on_ground
                and abs(frame.body.position[1] - target_y) <= self.profile.landing_level_tolerance_blocks
                and horizontal <= self.profile.landing_horizontal_radius_blocks
                and support.status is QueryStatus.FEASIBLE
                and support.support_fraction >= .80
                and set(support.support_materials).issubset(self.profile.support_materials))

    def decide(self, frame: NavigationFrame, *, input_confirmed: bool = True) -> JumpUpDecision:
        import time
        started = time.perf_counter_ns()
        if type(frame) is not NavigationFrame:
            raise ContractViolation("JumpUp decision requires a navigation frame")
        if self._start is None or self._end is None or self.state is JumpUpState.IDLE:
            return self._decision(JumpUpState.IDLE, MovementV1(), "not_started", started)
        if (frame.session != self._session or frame.body.session != self._session
                or frame.world.session != self._session):
            self.state = JumpUpState.FAILED
            return self._decision(self.state, MovementV1(), "world_session_changed", started)
        if self.state in {
            JumpUpState.COMPLETE, JumpUpState.CANCELLED, JumpUpState.INPUT_LOST,
            JumpUpState.FAILED, JumpUpState.NEEDS_INFORMATION,
            JumpUpState.UNSUPPORTED, JumpUpState.BLOCKED,
        }:
            return self._decision(self.state, MovementV1(), self.state.value, started)

        speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                           frame.body.velocity_blocks_per_second[2])
        if (self._cancel_requested
                and self.state in {JumpUpState.PREPARE, JumpUpState.REQUEST_TAKEOFF}):
            if frame.body.is_on_ground:
                if speed <= self.profile.maximum_exit_speed_blocks_per_second:
                    self.state = JumpUpState.CANCELLED
                    return self._decision(
                        self.state, MovementV1(), "cancelled_after_ground_stop", started,
                    )
                self.state = JumpUpState.CANCELLING
                return self._decision(
                    self.state, MovementV1(), "cancelling_before_takeoff", started,
                )
            self._takeoff_observed = True
            self.state = JumpUpState.CANCELLING

        if (self.state is JumpUpState.CANCELLING and not self._takeoff_observed
                and frame.body.is_on_ground):
            if speed <= self.profile.maximum_exit_speed_blocks_per_second:
                self.state = JumpUpState.CANCELLED
                return self._decision(
                    self.state, MovementV1(), "cancelled_after_ground_stop", started,
                )
            return self._decision(
                self.state, MovementV1(), "cancelling_before_takeoff", started,
            )

        if self.state is JumpUpState.PREPARE:
            geometry = query_jump_up(frame.world, self._start, self._end, self.profile)
            if geometry.status is not QueryStatus.FEASIBLE:
                self.state = {
                    QueryStatus.NEEDS_INFORMATION: JumpUpState.NEEDS_INFORMATION,
                    QueryStatus.UNSUPPORTED: JumpUpState.UNSUPPORTED,
                    QueryStatus.BLOCKED: JumpUpState.BLOCKED,
                }[geometry.status]
                return self._decision(self.state, MovementV1(), geometry.reason_code,
                                      started, geometry.missing_cells)
            center = (self._start[0] + .5, self._start[2] + .5)
            if (frame.body.pose != "standing" or not frame.body.is_on_ground
                    or abs(frame.body.position[1] - self._start[1]) > .10):
                self.state = JumpUpState.UNSUPPORTED
                return self._decision(self.state, MovementV1(), "invalid_entry_body", started)
            if speed > self.profile.maximum_entry_speed_blocks_per_second:
                self.state = JumpUpState.UNSUPPORTED
                return self._decision(self.state, MovementV1(), "entry_speed_out_of_range", started)
            if math.hypot(frame.body.position[0] - center[0],
                          frame.body.position[2] - center[1]) > self.profile.entry_center_tolerance_blocks:
                self.state = JumpUpState.UNSUPPORTED
                return self._decision(self.state, MovementV1(), "entry_position_out_of_range", started)
            dx, dz = self._end[0] - self._start[0], self._end[2] - self._start[2]
            offset_x = frame.body.position[0] - center[0]
            offset_z = frame.body.position[2] - center[1]
            forward_offset = offset_x * dx + offset_z * dz
            lateral_offset = offset_x * dz - offset_z * dx
            entry_epsilon = 1.0e-6
            if (forward_offset > self.profile.maximum_forward_offset_blocks + entry_epsilon
                    or forward_offset < -self.profile.maximum_backward_offset_blocks - entry_epsilon
                    or abs(lateral_offset) > self.profile.maximum_lateral_offset_blocks + entry_epsilon):
                self.state = JumpUpState.UNSUPPORTED
                return self._decision(
                    self.state, MovementV1(), "entry_directional_offset_out_of_range", started,
                )
            target_yaw = math.degrees(math.atan2(-dx, dz))
            current_yaw = math.degrees(frame.body.yaw_radians)
            yaw_delta = (target_yaw - current_yaw + 180.0) % 360.0 - 180.0
            if abs(yaw_delta) > self.profile.maximum_yaw_error_degrees:
                return self._decision(
                    self.state, MovementV1(), "aligning_takeoff_direction", started,
                    look=LookV1(yaw_delta, 0.0),
                )
            self.state = JumpUpState.REQUEST_TAKEOFF
            return self._decision(self.state, self._movement(frame, jump=True),
                                  "takeoff_requested", started)

        if not frame.body.is_on_ground:
            self._takeoff_observed = True
            self.state = (JumpUpState.CANCELLING
                          if self._cancel_requested or self._input_lost or not input_confirmed
                          else JumpUpState.AIRBORNE)

        if self.state is JumpUpState.REQUEST_TAKEOFF:
            if not input_confirmed:
                self.state = JumpUpState.FAILED
                return self._decision(self.state, MovementV1(), "takeoff_input_rejected", started)
            self._takeoff_wait += 1
            if self._takeoff_wait > self.profile.maximum_takeoff_wait_ticks:
                self.state = JumpUpState.FAILED
                return self._decision(self.state, MovementV1(), "takeoff_not_observed", started)
            return self._decision(self.state, self._movement(frame),
                                  "waiting_for_observed_takeoff", started)

        if self.state in {JumpUpState.AIRBORNE, JumpUpState.CANCELLING}:
            if not input_confirmed:
                self._input_lost = True
                self.state = JumpUpState.CANCELLING
            if frame.body.is_on_ground:
                if not self._landing_valid(frame):
                    self.state = JumpUpState.FAILED
                    return self._decision(self.state, MovementV1(), "landed_outside_target", started)
                speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                                   frame.body.velocity_blocks_per_second[2])
                if speed <= self.profile.maximum_exit_speed_blocks_per_second:
                    self.state = (JumpUpState.INPUT_LOST if self._input_lost
                                  else JumpUpState.CANCELLED if self._cancel_requested
                                  else JumpUpState.COMPLETE)
                    return self._decision(self.state, MovementV1(),
                                          "landing_verified", started)
                self.state = JumpUpState.VERIFY_LANDING
                return self._decision(self.state, MovementV1(), "settling_after_landing", started)
            self._airborne_ticks += 1
            if self._airborne_ticks > self.profile.maximum_airborne_ticks:
                self.state = JumpUpState.FAILED
                return self._decision(self.state, MovementV1(), "airborne_timeout", started)
            assert self._start is not None and self._end is not None
            dx, dz = self._end[0] - self._start[0], self._end[2] - self._start[2]
            progress = ((frame.body.position[0] - (self._start[0] + .5)) * dx
                        + (frame.body.position[2] - (self._start[2] + .5)) * dz)
            return self._decision(self.state, self._movement(
                frame, forward=progress < self.profile.forward_release_progress_blocks,
            ), "airborne_tracking", started)

        if self.state is JumpUpState.VERIFY_LANDING:
            if not input_confirmed:
                self._input_lost = True
            if not self._landing_valid(frame):
                self.state = JumpUpState.FAILED
                return self._decision(self.state, MovementV1(), "landing_became_invalid", started)
            self._settle_ticks += 1
            speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                               frame.body.velocity_blocks_per_second[2])
            if speed <= self.profile.maximum_exit_speed_blocks_per_second:
                self.state = (JumpUpState.INPUT_LOST if self._input_lost
                              else JumpUpState.CANCELLED if self._cancel_requested
                              else JumpUpState.COMPLETE)
                return self._decision(self.state, MovementV1(), "landing_verified", started)
            if self._settle_ticks > self.profile.maximum_settle_ticks:
                self.state = JumpUpState.FAILED
                return self._decision(self.state, MovementV1(), "landing_did_not_settle", started)
            return self._decision(self.state, MovementV1(), "settling_after_landing", started)

        return self._decision(self.state, MovementV1(), self.state.value, started)
