"""Shared observed-state core for calibrated gap jumps and controlled drops."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
from pathlib import Path
import time

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import MotionEnvironmentIdentity
from mc2p.motion_nav.ground_motion import GroundControl, control_world_direction
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.support_surfaces import SupportSurface
from mc2p.motion_nav.world_model import Aabb, BlockPos, WorldView


def _finite(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ContractViolation(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class AirMotionProfile:
    profile_id: str
    environment_id: str
    mode: MovementMode
    horizontal_cells: int
    target_height_delta_blocks: float
    jump_input: bool
    sprint_input: bool
    minimum_entry_speed_blocks_per_second: float
    maximum_entry_speed_blocks_per_second: float
    entry_center_tolerance_blocks: float
    maximum_forward_offset_blocks: float
    maximum_backward_offset_blocks: float
    maximum_lateral_offset_blocks: float
    maximum_yaw_error_degrees: float
    horizontal_safety_margin_blocks: float
    reference_positions: tuple[tuple[float, float, float], ...]
    departure_release_progress_blocks: float
    forward_release_progress_blocks: float
    maximum_departure_wait_ticks: int
    maximum_airborne_ticks: int
    landing_horizontal_radius_blocks: float
    landing_level_tolerance_blocks: float
    maximum_exit_speed_blocks_per_second: float
    maximum_settle_ticks: int
    recovery_forward_blocks: float
    maximum_fall_distance_blocks: float
    cost_seconds: float
    support_materials: frozenset[str]
    minecraft_version: str = "1.21"
    tick_seconds: float = .05
    risk_policy_id: str = "no_expected_damage"

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "air motion profile id")
        require_identifier(self.environment_id, "air motion environment id")
        require_identifier(self.risk_policy_id, "air motion risk policy id")
        if self.mode not in {MovementMode.JUMP_GAP, MovementMode.CONTROLLED_DROP}:
            raise ContractViolation("air motion profile requires a B09 movement mode")
        if self.minecraft_version != "1.21":
            raise ContractViolation("air motion Minecraft version is unsupported")
        if type(self.horizontal_cells) is not int or self.horizontal_cells < 1:
            raise ContractViolation("air motion horizontal distance must be positive cells")
        if type(self.jump_input) is not bool or type(self.sprint_input) is not bool:
            raise ContractViolation("air motion input flags must be bool")
        if self.mode is MovementMode.JUMP_GAP and not self.jump_input:
            raise ContractViolation("gap jump profile must request jump")
        if self.mode is MovementMode.CONTROLLED_DROP and self.jump_input:
            raise ContractViolation("controlled drop must not request jump")
        minimum = _finite(
            self.minimum_entry_speed_blocks_per_second, "minimum entry speed",
        )
        maximum = _finite(
            self.maximum_entry_speed_blocks_per_second, "maximum entry speed",
        )
        if minimum < 0 or maximum < minimum:
            raise ContractViolation("air motion entry speed interval is invalid")
        for name in (
            "entry_center_tolerance_blocks", "maximum_yaw_error_degrees",
            "forward_release_progress_blocks", "landing_horizontal_radius_blocks",
            "landing_level_tolerance_blocks", "maximum_exit_speed_blocks_per_second",
            "cost_seconds", "tick_seconds",
        ):
            if _finite(getattr(self, name), name) <= 0:
                raise ContractViolation(f"{name} must be positive")
        for name in (
            "horizontal_safety_margin_blocks", "recovery_forward_blocks",
            "maximum_fall_distance_blocks", "departure_release_progress_blocks",
            "maximum_forward_offset_blocks", "maximum_backward_offset_blocks",
            "maximum_lateral_offset_blocks",
        ):
            if _finite(getattr(self, name), name) < 0:
                raise ContractViolation(f"{name} must be nonnegative")
        if self.departure_release_progress_blocks > self.horizontal_cells:
            raise ContractViolation("departure release must lie within the transition")
        for name in (
            "maximum_departure_wait_ticks", "maximum_airborne_ticks",
            "maximum_settle_ticks",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ContractViolation(f"{name} must be a positive tick count")
        if type(self.support_materials) is not frozenset or not self.support_materials:
            raise ContractViolation("air motion support materials must be nonempty")
        for material in self.support_materials:
            require_identifier(material, "air motion support material")
        if (type(self.reference_positions) is not tuple
                or len(self.reference_positions) < 2
                or any(type(point) is not tuple or len(point) != 3
                       for point in self.reference_positions)):
            raise ContractViolation("air motion trajectory must be immutable 3D points")
        for point in self.reference_positions:
            for value in point:
                _finite(value, "air motion reference coordinate")
        if self.reference_positions[0] != (0.0, 0.0, 0.0):
            raise ContractViolation("air motion trajectory must start at the origin")
        final = self.reference_positions[-1]
        if (abs(final[0]) > 1.0e-9
                or abs(final[1] - self.target_height_delta_blocks)
                > self.landing_level_tolerance_blocks
                or abs(final[2] - self.horizontal_cells)
                > self.landing_horizontal_radius_blocks):
            raise ContractViolation("air motion trajectory does not end in its declared landing")
        if self.mode is MovementMode.CONTROLLED_DROP:
            fall = max(0.0, -self.target_height_delta_blocks)
            if fall > self.maximum_fall_distance_blocks + 1.0e-9:
                raise ContractViolation("controlled drop exceeds its zero-damage fall range")
        elif abs(self.target_height_delta_blocks) > 1.0e-9:
            raise ContractViolation("B09 gap jump profile must land at the start height")


def load_air_motion_profiles(
    path: Path,
    *,
    environment: MotionEnvironmentIdentity,
    catalog: BlockMotionCatalog,
) -> tuple[AirMotionProfile, ...]:
    if (not isinstance(path, Path)
            or type(environment) is not MotionEnvironmentIdentity
            or type(catalog) is not BlockMotionCatalog):
        raise ContractViolation("air motion profiles require path, environment and catalog")
    try:
        document = json.loads(path.read_text("utf-8"))
        if document.get("schema_version") != "mc2p.air-motion-profiles.v1":
            raise ContractViolation("unsupported air motion profile schema")
        scope = document["scope"]
        environment.require_profile_environment(scope["environment_id"])
        if (scope["minecraft_version"] != environment.minecraft_version
                or abs(float(scope["tick_seconds"]) - environment.tick_seconds) > 1.0e-12):
            raise ContractViolation("air motion profile environment does not match runtime")
        materials = catalog.materials_for_ground_model(scope["ground_model_id"])
        profiles = []
        for value in document["profiles"]:
            entry, trajectory, landing = (
                value["entry"], value["trajectory"], value["landing"]
            )
            profiles.append(AirMotionProfile(
                value["profile_id"], scope["environment_id"],
                MovementMode(value["mode"]), value["horizontal_cells"],
                value["target_height_delta_blocks"], value["jump_input"],
                value["sprint_input"],
                entry["minimum_horizontal_speed_blocks_per_second"],
                entry["maximum_horizontal_speed_blocks_per_second"],
                entry["center_tolerance_blocks"],
                entry["maximum_forward_offset_blocks"],
                entry["maximum_backward_offset_blocks"],
                entry["maximum_lateral_offset_blocks"],
                entry["maximum_yaw_error_degrees"],
                trajectory["horizontal_safety_margin_blocks"],
                tuple(tuple(float(axis) for axis in point)
                      for point in trajectory["reference_positions"]),
                trajectory.get("departure_release_progress_blocks", 0.0),
                trajectory["forward_release_progress_blocks"],
                trajectory["maximum_departure_wait_ticks"],
                trajectory["maximum_airborne_ticks"],
                landing["horizontal_radius_blocks"],
                landing["level_tolerance_blocks"],
                landing["maximum_exit_speed_blocks_per_second"],
                landing["maximum_settle_ticks"],
                landing["recovery_forward_blocks"],
                value["maximum_fall_distance_blocks"], value["cost_seconds"],
                materials, scope["minecraft_version"], scope["tick_seconds"],
                value.get("risk_policy_id", "no_expected_damage"),
            ))
        result = tuple(profiles)
        if (not result or len({profile.mode for profile in result}) != len(result)):
            raise ContractViolation("air motion profile modes must be nonempty and unique")
        return result
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, ContractViolation):
            raise
        raise ContractViolation("air motion profile document is invalid") from error


@dataclass(frozen=True, slots=True)
class AirMotionQuery:
    status: QueryStatus
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...] = ()
    reason_code: str = ""


def _merge_status(current: QueryStatus, incoming: QueryStatus) -> QueryStatus:
    order = {
        QueryStatus.FEASIBLE: 0,
        QueryStatus.NEEDS_INFORMATION: 1,
        QueryStatus.UNSUPPORTED: 2,
        QueryStatus.BLOCKED: 3,
    }
    return incoming if order[incoming] > order[current] else current


def _body_at(surface: SupportSurface, margin: float = 0.0) -> Aabb:
    x, y, z = surface.position
    return Aabb(
        x - .3 - margin, y, z - .3 - margin,
        x + .3 + margin, y + 1.8, z + .3 + margin,
    )


def _relation(start: SupportSurface, end: SupportSurface) -> tuple[int, int, int]:
    return (
        end.node_id.column_x - start.node_id.column_x,
        end.node_id.column_z - start.node_id.column_z,
        abs(end.node_id.column_x - start.node_id.column_x)
        + abs(end.node_id.column_z - start.node_id.column_z),
    )


def query_air_motion(world: WorldView, start: SupportSurface, end: SupportSurface,
                     profile: AirMotionProfile) -> AirMotionQuery:
    if (type(world) is not WorldView or type(start) is not SupportSurface
            or type(end) is not SupportSurface or type(profile) is not AirMotionProfile):
        raise ContractViolation("air motion query requires world, surfaces and profile")
    dx, dz, distance = _relation(start, end)
    height_delta = end.position[1] - start.position[1]
    base_dependencies = set(start.dependencies) | set(end.dependencies)
    if (distance != profile.horizontal_cells or (dx != 0 and dz != 0)
            or abs(height_delta - profile.target_height_delta_blocks)
            > profile.landing_level_tolerance_blocks):
        return AirMotionQuery(
            QueryStatus.UNSUPPORTED, tuple(sorted(base_dependencies)), (),
            "air_motion_relation_outside_profile",
        )
    unit_x, unit_z = dx // distance, dz // distance
    dependencies = set(base_dependencies)
    missing: set[BlockPos] = set()
    status = QueryStatus.FEASIBLE
    for surface in (start, end):
        support = query_support(_body_at(surface), world)
        dependencies.update(support.dependencies)
        missing.update(support.missing_cells)
        status = _merge_status(status, support.status)
        if (support.status is QueryStatus.FEASIBLE
                and (support.support_fraction < .5
                     or not set(support.support_materials).issubset(
                         profile.support_materials))):
            status = _merge_status(status, QueryStatus.UNSUPPORTED)

    forward_offsets = (
        -profile.maximum_backward_offset_blocks,
        0.0,
        profile.maximum_forward_offset_blocks,
    )
    lateral_offsets = (
        -profile.maximum_lateral_offset_blocks,
        0.0,
        profile.maximum_lateral_offset_blocks,
    )
    entry_offsets = tuple(sorted({
        (lateral, forward)
        for lateral in lateral_offsets
        for forward in forward_offsets
        if math.hypot(lateral, forward)
        <= profile.entry_center_tolerance_blocks + 1.0e-9
    }))
    for entry_lateral, entry_forward in entry_offsets:
        # Directional boundary samples keep the entry domain honest.  A single
        # enlarged box would incorrectly treat a legal forward offset as the
        # same thing as an unsafe backward offset at a drop edge.
        body = _body_at(start, profile.horizontal_safety_margin_blocks).moved(
            unit_x * entry_forward + unit_z * entry_lateral,
            0.0,
            unit_z * entry_forward - unit_x * entry_lateral,
        )
        entry_support = query_support(body, world)
        dependencies.update(entry_support.dependencies)
        missing.update(entry_support.missing_cells)
        status = _merge_status(status, entry_support.status)
        if (entry_support.status is QueryStatus.FEASIBLE
                and (entry_support.support_fraction < .5
                     or not set(entry_support.support_materials).issubset(
                         profile.support_materials))):
            status = _merge_status(status, QueryStatus.UNSUPPORTED)

        previous = profile.reference_positions[0]
        for index, point in enumerate(profile.reference_positions[1:], start=1):
            lateral, rise, forward = point
            old_lateral, old_rise, old_forward = previous
            # The reference uses forward/lateral axes. Minecraft resolves
            # vertical collision before horizontal collision during each tick.
            target_rise = rise
            if index == len(profile.reference_positions) - 1:
                target_rise += 1.0e-6
            horizontal = (
                unit_x * (forward - old_forward)
                + unit_z * (lateral - old_lateral),
                0.0,
                unit_z * (forward - old_forward)
                - unit_x * (lateral - old_lateral),
            )
            for movement in ((0.0, target_rise - old_rise, 0.0), horizontal):
                result = sweep(body, movement, world)
                dependencies.update(result.dependencies)
                missing.update(result.missing_cells)
                status = _merge_status(status, result.status)
                body = body.moved(*movement)
            previous = point

        landing_support = query_support(body, world)
        dependencies.update(landing_support.dependencies)
        missing.update(landing_support.missing_cells)
        status = _merge_status(status, landing_support.status)
        if (landing_support.status is QueryStatus.FEASIBLE
                and (landing_support.support_fraction < .5
                     or not set(landing_support.support_materials).issubset(
                         profile.support_materials))):
            status = _merge_status(status, QueryStatus.UNSUPPORTED)

        if profile.recovery_forward_blocks > 0:
            movement = (
                unit_x * profile.recovery_forward_blocks, 0.0,
                unit_z * profile.recovery_forward_blocks,
            )
            recovery = sweep(body, movement, world)
            dependencies.update(recovery.dependencies)
            missing.update(recovery.missing_cells)
            status = _merge_status(status, recovery.status)
            recovered_body = body.moved(*movement)
            support = query_support(recovered_body, world)
            dependencies.update(support.dependencies)
            missing.update(support.missing_cells)
            status = _merge_status(status, support.status)
            if (support.status is QueryStatus.FEASIBLE
                    and (support.support_fraction < .5
                         or not set(support.support_materials).issubset(
                             profile.support_materials))):
                status = _merge_status(status, QueryStatus.UNSUPPORTED)
    reason = {
        QueryStatus.FEASIBLE: "air_motion_feasible",
        QueryStatus.BLOCKED: "air_motion_blocked",
        QueryStatus.NEEDS_INFORMATION: "air_motion_needs_information",
        QueryStatus.UNSUPPORTED: "air_motion_unsupported",
    }[status]
    return AirMotionQuery(
        status, tuple(sorted(dependencies)), tuple(sorted(missing)), reason,
    )


class AirMotionState(StrEnum):
    IDLE = "idle"
    PREPARE = "prepare"
    REQUEST_DEPARTURE = "request_departure"
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
class AirMotionDecision:
    state: AirMotionState
    movement: MovementV1
    look: LookV1 | None
    input_lease_ticks: int
    missing_cells: tuple[BlockPos, ...]
    reason_code: str
    control_time_ns: int


class AirMotionController:
    """Execute one calibrated air transition from observed body state."""

    _TERMINAL = {
        AirMotionState.COMPLETE, AirMotionState.CANCELLED,
        AirMotionState.INPUT_LOST, AirMotionState.FAILED,
        AirMotionState.NEEDS_INFORMATION, AirMotionState.UNSUPPORTED,
        AirMotionState.BLOCKED,
    }

    def __init__(self, profile: AirMotionProfile) -> None:
        if type(profile) is not AirMotionProfile:
            raise ContractViolation("air motion controller requires a profile")
        self.profile = profile
        self.state = AirMotionState.IDLE
        self._start: SupportSurface | None = None
        self._end: SupportSurface | None = None
        self._session = None
        self._departure_origin: tuple[float, float] | None = None
        self._departure_wait = 0
        self._airborne_ticks = 0
        self._settle_ticks = 0
        self._departure_observed = False
        self._departure_requested = False
        self._cancel_requested = False
        self._input_lost = False

    def start(self, start: SupportSurface, end: SupportSurface,
              frame: NavigationFrame) -> None:
        if (type(start) is not SupportSurface or type(end) is not SupportSurface
                or type(frame) is not NavigationFrame):
            raise ContractViolation("air motion start requires surfaces and a frame")
        if self.state not in {AirMotionState.IDLE, *self._TERMINAL}:
            raise ContractViolation("active air motion must finish before restart")
        self._start, self._end, self._session = start, end, frame.session
        self.state = AirMotionState.PREPARE
        self._departure_wait = self._airborne_ticks = self._settle_ticks = 0
        self._departure_observed = self._departure_requested = False
        self._cancel_requested = self._input_lost = False
        self._departure_origin = None

    def cancel(self) -> None:
        if self.state not in {AirMotionState.IDLE, *self._TERMINAL}:
            self._cancel_requested = True

    def _direction(self) -> tuple[int, int]:
        assert self._start is not None and self._end is not None
        dx = self._end.node_id.column_x - self._start.node_id.column_x
        dz = self._end.node_id.column_z - self._start.node_id.column_z
        distance = abs(dx) + abs(dz)
        return dx // distance, dz // distance

    def _progress(self, frame: NavigationFrame) -> float:
        assert self._departure_origin is not None
        dx, dz = self._direction()
        return (
            (frame.body.position[0] - self._departure_origin[0]) * dx
            + (frame.body.position[2] - self._departure_origin[1]) * dz
        )

    def _movement(self, frame: NavigationFrame, *, jump: bool = False,
                  forward: bool = True) -> MovementV1:
        if not forward:
            return MovementV1()
        dx, dz = self._direction()
        candidates = tuple(
            MovementV1(
                forward=forward_axis, strafe=strafe_axis, jump=jump,
                sprint=self.profile.sprint_input,
            )
            for forward_axis in (-1, 0, 1)
            for strafe_axis in (-1, 0, 1)
            if forward_axis or strafe_axis
        )
        def alignment(movement: MovementV1) -> float:
            world_x, world_z = control_world_direction(GroundControl(
                movement.forward, -movement.strafe, frame.body.yaw_radians,
            ))
            return world_x * dx + world_z * dz
        return max(candidates, key=alignment)

    @staticmethod
    def _decision(state: AirMotionState, movement: MovementV1, reason: str,
                  started: int, missing: tuple[BlockPos, ...] = (),
                  look: LookV1 | None = None) -> AirMotionDecision:
        return AirMotionDecision(
            state, movement, look, 1, missing, reason,
            time.perf_counter_ns() - started,
        )

    def _landing_valid(self, frame: NavigationFrame) -> bool:
        assert self._end is not None
        horizontal = math.hypot(
            frame.body.position[0] - self._end.position[0],
            frame.body.position[2] - self._end.position[2],
        )
        support = query_support(frame.body.body_box, frame.world)
        return (
            frame.body.is_on_ground
            and abs(frame.body.position[1] - self._end.position[1])
            <= self.profile.landing_level_tolerance_blocks
            and horizontal <= self.profile.landing_horizontal_radius_blocks
            and support.status is QueryStatus.FEASIBLE
            and support.support_fraction >= .5
            and set(support.support_materials).issubset(self.profile.support_materials)
        )

    def decide(self, frame: NavigationFrame, *, input_confirmed: bool = True
               ) -> AirMotionDecision:
        started = time.perf_counter_ns()
        if type(frame) is not NavigationFrame:
            raise ContractViolation("air motion decision requires a frame")
        if self._start is None or self._end is None or self.state is AirMotionState.IDLE:
            return self._decision(AirMotionState.IDLE, MovementV1(), "not_started", started)
        if (frame.session != self._session or frame.body.session != self._session
                or frame.world.session != self._session):
            self.state = AirMotionState.FAILED
            return self._decision(self.state, MovementV1(), "world_session_changed", started)
        if self.state in self._TERMINAL:
            return self._decision(self.state, MovementV1(), self.state.value, started)

        speed = math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        )
        if (self._cancel_requested and not self._departure_observed
                and frame.body.is_on_ground):
            if speed <= self.profile.maximum_exit_speed_blocks_per_second:
                self.state = AirMotionState.CANCELLED
                return self._decision(
                    self.state, MovementV1(), "cancelled_before_departure", started,
                )
            self.state = AirMotionState.CANCELLING
            return self._decision(
                self.state, MovementV1(), "stopping_before_departure", started,
            )

        if self.state is AirMotionState.PREPARE:
            geometry = query_air_motion(frame.world, self._start, self._end, self.profile)
            if geometry.status is not QueryStatus.FEASIBLE:
                self.state = {
                    QueryStatus.NEEDS_INFORMATION: AirMotionState.NEEDS_INFORMATION,
                    QueryStatus.UNSUPPORTED: AirMotionState.UNSUPPORTED,
                    QueryStatus.BLOCKED: AirMotionState.BLOCKED,
                }[geometry.status]
                return self._decision(
                    self.state, MovementV1(), geometry.reason_code, started,
                    geometry.missing_cells,
                )
            if (frame.body.pose != "standing" or not frame.body.is_on_ground
                    or abs(frame.body.position[1] - self._start.position[1]) > .1):
                self.state = AirMotionState.UNSUPPORTED
                return self._decision(
                    self.state, MovementV1(), "invalid_entry_body", started,
                )
            if not (self.profile.minimum_entry_speed_blocks_per_second - 1.0e-9
                    <= speed
                    <= self.profile.maximum_entry_speed_blocks_per_second + 1.0e-9):
                self.state = AirMotionState.UNSUPPORTED
                return self._decision(
                    self.state, MovementV1(), "entry_speed_out_of_range", started,
                )
            dx, dz = self._direction()
            offset_x = frame.body.position[0] - self._start.position[0]
            offset_z = frame.body.position[2] - self._start.position[2]
            forward_offset = offset_x * dx + offset_z * dz
            lateral_offset = offset_x * dz - offset_z * dx
            if (math.hypot(offset_x, offset_z)
                    > self.profile.entry_center_tolerance_blocks + 1.0e-9
                    or forward_offset
                    > self.profile.maximum_forward_offset_blocks + 1.0e-9
                    or forward_offset
                    < -self.profile.maximum_backward_offset_blocks - 1.0e-9
                    or abs(lateral_offset)
                    > self.profile.maximum_lateral_offset_blocks + 1.0e-9):
                self.state = AirMotionState.UNSUPPORTED
                return self._decision(
                    self.state, MovementV1(), "entry_position_out_of_range", started,
                )
            target_yaw = math.degrees(math.atan2(-dx, dz))
            current_yaw = math.degrees(frame.body.yaw_radians)
            yaw_delta = (target_yaw - current_yaw + 180.0) % 360.0 - 180.0
            if abs(yaw_delta) > self.profile.maximum_yaw_error_degrees:
                return self._decision(
                    self.state, MovementV1(), "aligning_departure_direction",
                    started, look=LookV1(yaw_delta, 0.0),
                )
            self.state = AirMotionState.REQUEST_DEPARTURE
            self._departure_requested = True
            self._departure_origin = (
                frame.body.position[0], frame.body.position[2],
            )
            return self._decision(
                self.state,
                self._movement(frame, jump=self.profile.jump_input),
                "departure_requested", started,
            )

        if not frame.body.is_on_ground:
            self._departure_observed = True
            self.state = (
                AirMotionState.CANCELLING
                if self._cancel_requested or self._input_lost or not input_confirmed
                else AirMotionState.AIRBORNE
            )

        if self.state is AirMotionState.REQUEST_DEPARTURE:
            if not input_confirmed:
                self.state = AirMotionState.FAILED
                return self._decision(
                    self.state, MovementV1(), "departure_input_rejected", started,
                )
            self._departure_wait += 1
            if self._departure_wait > self.profile.maximum_departure_wait_ticks:
                self.state = AirMotionState.FAILED
                return self._decision(
                    self.state, MovementV1(), "departure_not_observed", started,
                )
            coast = (
                self.profile.mode is MovementMode.CONTROLLED_DROP
                and self._progress(frame)
                >= self.profile.departure_release_progress_blocks
            )
            return self._decision(
                self.state, self._movement(frame, forward=not coast),
                "coasting_to_drop_edge" if coast
                else "waiting_for_observed_departure", started,
            )

        if self.state in {AirMotionState.AIRBORNE, AirMotionState.CANCELLING}:
            if not input_confirmed:
                self._input_lost = True
                self.state = AirMotionState.CANCELLING
            if frame.body.is_on_ground:
                if not self._landing_valid(frame):
                    self.state = AirMotionState.FAILED
                    return self._decision(
                        self.state, MovementV1(), "landed_outside_target", started,
                    )
                if speed <= self.profile.maximum_exit_speed_blocks_per_second:
                    self.state = (
                        AirMotionState.INPUT_LOST if self._input_lost
                        else AirMotionState.CANCELLED if self._cancel_requested
                        else AirMotionState.COMPLETE
                    )
                    return self._decision(
                        self.state, MovementV1(), "landing_verified", started,
                    )
                self.state = AirMotionState.VERIFY_LANDING
                return self._decision(
                    self.state, MovementV1(), "settling_after_landing", started,
                )
            self._airborne_ticks += 1
            if self._airborne_ticks > self.profile.maximum_airborne_ticks:
                self.state = AirMotionState.FAILED
                return self._decision(
                    self.state, MovementV1(), "airborne_timeout", started,
                )
            progress = self._progress(frame)
            return self._decision(
                self.state,
                self._movement(
                    frame,
                    # Once the body has left support, cancellation and a lost
                    # receipt cannot make it stop in mid-air.  Keep the
                    # calibrated safe input until the landing is observed,
                    # then report the interrupted terminal state.
                    forward=(progress < self.profile.forward_release_progress_blocks),
                ),
                "airborne_tracking", started,
            )

        if self.state is AirMotionState.VERIFY_LANDING:
            if not input_confirmed:
                self._input_lost = True
            if not self._landing_valid(frame):
                self.state = AirMotionState.FAILED
                return self._decision(
                    self.state, MovementV1(), "landing_became_invalid", started,
                )
            self._settle_ticks += 1
            if speed <= self.profile.maximum_exit_speed_blocks_per_second:
                self.state = (
                    AirMotionState.INPUT_LOST if self._input_lost
                    else AirMotionState.CANCELLED if self._cancel_requested
                    else AirMotionState.COMPLETE
                )
                return self._decision(
                    self.state, MovementV1(), "landing_verified", started,
                )
            if self._settle_ticks > self.profile.maximum_settle_ticks:
                self.state = AirMotionState.FAILED
                return self._decision(
                    self.state, MovementV1(), "landing_did_not_settle", started,
                )
            return self._decision(
                self.state, MovementV1(), "settling_after_landing", started,
            )

        return self._decision(self.state, MovementV1(), self.state.value, started)
