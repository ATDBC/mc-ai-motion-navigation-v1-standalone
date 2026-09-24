"""Eight-way normal inputs and bounded gaze geometry, not player velocity."""

from __future__ import annotations

import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import (
    ContractViolation,
    require_finite,
    require_nonnegative_int,
)


_EIGHT_WAY_INPUTS = (
    (MovementV1(forward=1), 0.0),
    (MovementV1(forward=1, strafe=1), -45.0),
    (MovementV1(strafe=1), -90.0),
    (MovementV1(forward=-1, strafe=1), -135.0),
    (MovementV1(forward=-1), -180.0),
    (MovementV1(forward=-1, strafe=-1), 135.0),
    (MovementV1(strafe=-1), 90.0),
    (MovementV1(forward=1, strafe=-1), 45.0),
)


def _wrap_yaw(yaw: float) -> float:
    return (yaw + 180.0) % 360.0 - 180.0


def world_direction(yaw: float, forward: int, strafe: int) -> tuple[float, float]:
    """Return the unit horizontal intent direction for discrete player inputs."""
    require_finite(yaw, "yaw")
    for name, value in (("forward", forward), ("strafe", strafe)):
        if type(value) is not int or value not in (-1, 0, 1):
            raise ContractViolation(f"{name} must be -1, 0 or 1")
    length = math.hypot(forward, strafe)
    if length == 0.0:
        return (0.0, 0.0)
    radians = math.radians(yaw)
    return (
        (-math.sin(radians) * forward + math.cos(radians) * strafe) / length,
        (math.cos(radians) * forward + math.sin(radians) * strafe) / length,
    )


def aligned_controls(
    travel_yaw: float,
    gaze_yaw: float,
    *,
    max_gaze_adjustment_degrees: float = 22.5,
) -> tuple[MovementV1, float]:
    """Choose a stable nearest eight-way input and the absolute gaze it needs."""
    require_finite(travel_yaw, "travel yaw")
    require_finite(gaze_yaw, "gaze yaw")
    require_finite(max_gaze_adjustment_degrees, "maximum gaze adjustment")
    if not 0.0 <= max_gaze_adjustment_degrees <= 22.5:
        raise ContractViolation("maximum gaze adjustment must be within [0,22.5]")

    relative_yaw = _wrap_yaw(travel_yaw - gaze_yaw)
    best_movement, first_offset = _EIGHT_WAY_INPUTS[0]
    best_error = _wrap_yaw(relative_yaw - first_offset)
    for movement, input_offset in _EIGHT_WAY_INPUTS[1:]:
        error = _wrap_yaw(relative_yaw - input_offset)
        # Strictly smaller replaces; an exact tie retains the fixed earlier input.
        if abs(error) < abs(best_error):
            best_movement = movement
            best_error = error

    if abs(best_error) > max_gaze_adjustment_degrees:
        raise ContractViolation("nearest input exceeds maximum gaze adjustment")
    return best_movement, _wrap_yaw(gaze_yaw + best_error)


def bounded_look(
    current_yaw: float,
    current_pitch: float,
    target_yaw: float,
    target_pitch: float,
    elapsed_ns: int,
) -> LookV1:
    """Cap the combined yaw/pitch delta at 60 degrees/s and at most 50 ms."""
    for name, value in (
        ("current yaw", current_yaw),
        ("current pitch", current_pitch),
        ("target yaw", target_yaw),
        ("target pitch", target_pitch),
    ):
        require_finite(value, name)
    require_nonnegative_int(elapsed_ns, "elapsed time")
    bounded_elapsed_ns = min(elapsed_ns, 50_000_000)
    limit = 60.0 * bounded_elapsed_ns / 1_000_000_000.0
    yaw_error = _wrap_yaw(target_yaw - current_yaw)
    pitch_error = target_pitch - current_pitch
    yaw_delta = max(-limit, min(limit, yaw_error))
    pitch_delta = max(-limit, min(limit, pitch_error))
    length = math.hypot(yaw_delta, pitch_delta)
    combined_limit = limit
    if yaw_delta != 0.0 and pitch_delta != 0.0:
        # Java stores both the requested delta and resulting angle as float32.
        # Reserve their rounding error before scaling a simultaneous turn.
        rounding_margin = 2.0**-23 * math.hypot(
            abs(current_yaw) + 2.0 * abs(yaw_delta),
            abs(current_pitch) + 2.0 * abs(pitch_delta),
        )
        combined_limit = max(0.0, limit - rounding_margin)
    if length > combined_limit:
        scale = combined_limit / length
        yaw_delta *= scale
        pitch_delta *= scale
    return LookV1(yaw_delta, pitch_delta)
