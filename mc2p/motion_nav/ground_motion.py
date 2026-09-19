"""Explicit-unit ordinary-ground prediction; parameters require later calibration."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation, require_identifier


def _finite(value: float, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ContractViolation(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class GroundMotionProfile:
    tick_seconds: float
    acceleration_blocks_per_second2: float
    velocity_retention_per_tick: float
    maximum_speed_blocks_per_second: float
    support_materials: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("tick_seconds", "acceleration_blocks_per_second2",
                     "velocity_retention_per_tick", "maximum_speed_blocks_per_second"):
            _finite(getattr(self, name), name)
        if (self.tick_seconds <= 0 or self.acceleration_blocks_per_second2 < 0
                or not 0 <= self.velocity_retention_per_tick <= 1
                or self.maximum_speed_blocks_per_second <= 0):
            raise ContractViolation("invalid ordinary-ground motion profile")
        if type(self.support_materials) is not frozenset:
            raise ContractViolation("ordinary-ground support materials must be a frozenset")
        for material in self.support_materials:
            require_identifier(material, "ordinary-ground support material")


@dataclass(frozen=True, slots=True)
class GroundControl:
    forward: float
    strafe: float
    yaw_radians: float

    def __post_init__(self) -> None:
        for name in ("forward", "strafe", "yaw_radians"):
            _finite(getattr(self, name), name)
        if abs(self.forward) > 1 or abs(self.strafe) > 1:
            raise ContractViolation("movement axes must be in [-1, 1]")


@dataclass(frozen=True, slots=True)
class PlanarBodyState:
    x: float
    z: float
    velocity_x: float
    velocity_z: float
    yaw_radians: float

    def __post_init__(self) -> None:
        for name in ("x", "z", "velocity_x", "velocity_z", "yaw_radians"):
            _finite(getattr(self, name), name)


def control_world_direction(control: GroundControl) -> tuple[float, float]:
    """Convert camera-relative input to an X/Z world direction."""
    if type(control) is not GroundControl:
        raise ContractViolation("world direction requires a ground control")
    length = math.hypot(control.forward, control.strafe)
    forward, strafe = control.forward, control.strafe
    if length > 1:
        forward, strafe = forward / length, strafe / length
    sin_yaw, cos_yaw = math.sin(control.yaw_radians), math.cos(control.yaw_radians)
    return (-sin_yaw * forward - cos_yaw * strafe,
            cos_yaw * forward - sin_yaw * strafe)


def world_direction_control(direction_x: float, direction_z: float,
                            yaw_radians: float) -> GroundControl:
    """Express one desired X/Z world direction in the current camera frame."""
    for value, name in ((direction_x, "world direction x"),
                        (direction_z, "world direction z"),
                        (yaw_radians, "yaw")):
        _finite(value, name)
    length = math.hypot(direction_x, direction_z)
    if length > 1:
        direction_x, direction_z = direction_x / length, direction_z / length
    sin_yaw, cos_yaw = math.sin(yaw_radians), math.cos(yaw_radians)
    forward = -sin_yaw * direction_x + cos_yaw * direction_z
    strafe = -cos_yaw * direction_x - sin_yaw * direction_z
    return GroundControl(forward, strafe, yaw_radians)


def _step(state: PlanarBodyState, control: GroundControl,
          profile: GroundMotionProfile) -> PlanarBodyState:
    direction_x, direction_z = control_world_direction(control)
    velocity_x = state.velocity_x + direction_x * profile.acceleration_blocks_per_second2 * profile.tick_seconds
    velocity_z = state.velocity_z + direction_z * profile.acceleration_blocks_per_second2 * profile.tick_seconds
    speed = math.hypot(velocity_x, velocity_z)
    if speed > profile.maximum_speed_blocks_per_second:
        scale = profile.maximum_speed_blocks_per_second / speed
        velocity_x *= scale; velocity_z *= scale
    x = state.x + velocity_x * profile.tick_seconds
    z = state.z + velocity_z * profile.tick_seconds
    velocity_x *= profile.velocity_retention_per_tick
    velocity_z *= profile.velocity_retention_per_tick
    return PlanarBodyState(x, z, velocity_x, velocity_z, control.yaw_radians)


def predict_ground(initial: PlanarBodyState, controls: tuple[GroundControl, ...],
                   profile: GroundMotionProfile) -> tuple[PlanarBodyState, ...]:
    if type(initial) is not PlanarBodyState or type(profile) is not GroundMotionProfile:
        raise ContractViolation("ground prediction requires typed state and profile")
    if type(controls) is not tuple or any(type(control) is not GroundControl for control in controls):
        raise ContractViolation("ground controls must be an immutable typed tuple")
    states = [initial]
    for control in controls:
        states.append(_step(states[-1], control, profile))
    return tuple(states)
