"""Shared player-eye to entity-center aiming geometry."""
from __future__ import annotations

import math

from mc2p.contracts.common import ContractViolation, require_finite
from mc2p.contracts.observation import Vec3V0


def combat_aim_angles(
    relative_position: Vec3V0,
    bounding_box_size: Vec3V0,
    eye_height_blocks: float,
) -> tuple[float, float]:
    """Return absolute Minecraft yaw/pitch from the observed player eye."""
    if (type(relative_position) is not Vec3V0
            or type(bounding_box_size) is not Vec3V0):
        raise ContractViolation("combat aim requires typed relative geometry")
    require_finite(eye_height_blocks, "combat eye height")
    if eye_height_blocks <= 0 or bounding_box_size.y <= 0:
        raise ContractViolation("combat aim heights must be positive")
    horizontal = max(
        math.hypot(relative_position.x, relative_position.z),
        .001,
    )
    target_center_y = relative_position.y + bounding_box_size.y / 2
    yaw = math.degrees(math.atan2(-relative_position.x, relative_position.z))
    pitch = math.degrees(math.atan2(
        eye_height_blocks - target_center_y,
        horizontal,
    ))
    return yaw, pitch
