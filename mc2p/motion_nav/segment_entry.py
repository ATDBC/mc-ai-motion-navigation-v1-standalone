"""Directional geometric and kinematic entry conditions for route segments."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.physics_types import PhysicsState
from mc2p.motion_nav.runtime_adapter import BodyState


_DIRECTION_DEFINED_SPEED_BLOCKS_PER_SECOND = 0.1


def _finite(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ContractViolation(f"{name} must be finite")
    return float(value)


def _angle_error(first: float, second: float) -> float:
    return abs((first - second + math.pi) % (2.0 * math.pi) - math.pi)


@dataclass(frozen=True, slots=True)
class SegmentEntryWindow:
    reference_point: tuple[float, float, float]
    horizontal_approach_direction: tuple[float, float]
    minimum_longitudinal_offset_blocks: float
    maximum_longitudinal_offset_blocks: float
    maximum_lateral_offset_blocks: float
    minimum_feet_y: float
    maximum_feet_y: float
    minimum_speed_blocks_per_second: float
    maximum_speed_blocks_per_second: float
    maximum_velocity_direction_error_radians: float
    allowed_poses: frozenset[str]
    allowed_modes: frozenset[MovementMode]
    required_yaw_radians: float | None
    maximum_yaw_error_radians: float | None
    profile_id: str

    def __post_init__(self) -> None:
        if type(self.reference_point) is not tuple or len(self.reference_point) != 3:
            raise ContractViolation("segment entry reference must be a 3D point")
        object.__setattr__(self, "reference_point", tuple(
            _finite(value, "segment entry reference")
            for value in self.reference_point
        ))
        if (type(self.horizontal_approach_direction) is not tuple
                or len(self.horizontal_approach_direction) != 2):
            raise ContractViolation("segment entry approach must be a planar vector")
        dx, dz = tuple(
            _finite(value, "segment entry approach direction")
            for value in self.horizontal_approach_direction
        )
        if abs(math.hypot(dx, dz) - 1.0) > 1.0e-6:
            raise ContractViolation("segment entry approach direction must be normalized")
        object.__setattr__(self, "horizontal_approach_direction", (dx, dz))
        minimum_offset = _finite(
            self.minimum_longitudinal_offset_blocks,
            "minimum longitudinal entry offset",
        )
        maximum_offset = _finite(
            self.maximum_longitudinal_offset_blocks,
            "maximum longitudinal entry offset",
        )
        minimum_y = _finite(self.minimum_feet_y, "minimum entry feet height")
        maximum_y = _finite(self.maximum_feet_y, "maximum entry feet height")
        minimum_speed = _finite(
            self.minimum_speed_blocks_per_second, "minimum entry speed",
        )
        maximum_speed = _finite(
            self.maximum_speed_blocks_per_second, "maximum entry speed",
        )
        lateral = _finite(
            self.maximum_lateral_offset_blocks, "maximum lateral entry offset",
        )
        direction_error = _finite(
            self.maximum_velocity_direction_error_radians,
            "maximum entry velocity direction error",
        )
        if (minimum_offset > maximum_offset or minimum_y > maximum_y
                or minimum_speed < 0.0 or minimum_speed > maximum_speed
                or lateral < 0.0 or not 0.0 <= direction_error <= math.pi):
            raise ContractViolation("segment entry ranges are invalid")
        if (type(self.allowed_poses) is not frozenset or not self.allowed_poses
                or any(type(pose) is not str or not pose
                       for pose in self.allowed_poses)):
            raise ContractViolation("segment entry poses must be explicit")
        if (type(self.allowed_modes) is not frozenset or not self.allowed_modes
                or any(type(mode) is not MovementMode
                       for mode in self.allowed_modes)):
            raise ContractViolation("segment entry modes must be explicit")
        if (self.required_yaw_radians is None) != (
                self.maximum_yaw_error_radians is None):
            raise ContractViolation("segment entry yaw requirement is incomplete")
        if self.required_yaw_radians is not None:
            _finite(self.required_yaw_radians, "required entry yaw")
            yaw_error = _finite(
                self.maximum_yaw_error_radians, "maximum entry yaw error",
            )
            if not 0.0 <= yaw_error <= math.pi:
                raise ContractViolation("maximum entry yaw error must be within 0..pi")
        require_identifier(self.profile_id, "segment entry profile id")


def _fits(window: SegmentEntryWindow, *, position, velocity, yaw, pose, mode) -> bool:
    if type(window) is not SegmentEntryWindow or type(mode) is not MovementMode:
        raise ContractViolation("segment entry check requires typed inputs")
    if pose not in window.allowed_poses or mode not in window.allowed_modes:
        return False
    dx, dz = window.horizontal_approach_direction
    offset_x = position[0] - window.reference_point[0]
    offset_z = position[2] - window.reference_point[2]
    longitudinal = offset_x * dx + offset_z * dz
    lateral = abs(-offset_x * dz + offset_z * dx)
    if not (window.minimum_longitudinal_offset_blocks - 1.0e-9
            <= longitudinal <= window.maximum_longitudinal_offset_blocks + 1.0e-9):
        return False
    if lateral > window.maximum_lateral_offset_blocks + 1.0e-9:
        return False
    if not (window.minimum_feet_y - 1.0e-9
            <= position[1] <= window.maximum_feet_y + 1.0e-9):
        return False
    vx, _, vz = velocity
    speed = math.hypot(vx, vz)
    if not (window.minimum_speed_blocks_per_second - 1.0e-9
            <= speed <= window.maximum_speed_blocks_per_second + 1.0e-9):
        return False
    # Below the shared stopped-speed boundary, the residual vector is too
    # small to define a useful approach direction.  Requiring its angle lets
    # tiny lateral decay prevent an otherwise valid stopped handoff forever.
    if speed > _DIRECTION_DEFINED_SPEED_BLOCKS_PER_SECOND + 1.0e-9:
        cosine = max(-1.0, min(1.0, (vx * dx + vz * dz) / speed))
        if math.acos(cosine) > window.maximum_velocity_direction_error_radians + 1.0e-9:
            return False
    if (window.required_yaw_radians is not None
            and _angle_error(yaw, window.required_yaw_radians)
                > window.maximum_yaw_error_radians + 1.0e-9):
        return False
    return True


def body_fits_segment_entry(
    window: SegmentEntryWindow, body: BodyState, mode: MovementMode,
) -> bool:
    if type(body) is not BodyState:
        raise ContractViolation("body entry check requires BodyState")
    return _fits(
        window, position=body.position,
        velocity=body.velocity_blocks_per_second,
        yaw=body.yaw_radians, pose=body.pose, mode=mode,
    )


def physics_fits_segment_entry(
    window: SegmentEntryWindow, state: PhysicsState, mode: MovementMode,
) -> bool:
    if type(state) is not PhysicsState:
        raise ContractViolation("physics entry check requires PhysicsState")
    return _fits(
        window, position=state.position,
        velocity=tuple(value * 20.0 for value in state.velocity_blocks_per_tick),
        yaw=state.yaw_radians, pose=state.pose, mode=mode,
    )


@dataclass(frozen=True, slots=True)
class MotionContinuationRequirement:
    """One successor's bounded entry corridor, shared by proof and execution.

    The window grants no world knowledge. Every candidate still has to prove
    collision, support, release safety and all affected world dependencies.
    """

    entry_window: SegmentEntryWindow
    mode: MovementMode
    following_route_id: str
    recovery_entry_window: SegmentEntryWindow | None = None
    minimum_recovery_support_fraction: float = .15

    def __post_init__(self) -> None:
        if (type(self.entry_window) is not SegmentEntryWindow
                or type(self.mode) is not MovementMode
                or self.mode not in self.entry_window.allowed_modes):
            raise ContractViolation("motion continuation requires a typed successor entry")
        require_identifier(self.following_route_id, "following route id")
        if (self.recovery_entry_window is not None
                and type(self.recovery_entry_window) is not SegmentEntryWindow):
            raise ContractViolation("recovery entry must be typed")
        if not 0.0 < self.minimum_recovery_support_fraction <= 1.0:
            raise ContractViolation("recovery support fraction must be within 0..1")

    def accepts(self, state: PhysicsState) -> bool:
        return state.on_ground and physics_fits_segment_entry(
            self.entry_window, state, self.mode,
        )

    def accepts_recovery(self, state: PhysicsState) -> bool:
        """Geometric necessary condition; actual support must be checked live."""
        return state.on_ground and physics_fits_segment_entry(
            self.recovery_entry_window or self.entry_window, state, self.mode,
        )

    def progress(self, position: tuple[float, float, float]) -> float:
        window = self.entry_window
        dx, dz = window.horizontal_approach_direction
        return ((position[0] - window.reference_point[0]) * dx
                + (position[2] - window.reference_point[2]) * dz)
