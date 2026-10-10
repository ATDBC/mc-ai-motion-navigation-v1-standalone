"""Stable D096 action-primitive enumeration; physics remains in the caller."""
from __future__ import annotations

from dataclasses import dataclass, replace

from mc2p.motion_nav.physics_types import TickInput
from .contracts import InputTier


@dataclass(frozen=True, slots=True)
class PrimitivePrefix:
    """One G -> optional J -> A grid point before normal braking."""

    tier_id: str
    gait_index: int
    movement_yaw_radians: float
    ground_ticks: int
    jumped: bool
    air_ticks: int
    brake_ticks: int
    inputs: tuple[TickInput, ...]

    def with_brake(self, stop_input: TickInput, brake_ticks: int) -> "PrimitivePrefix":
        if type(stop_input) is not TickInput or type(brake_ticks) is not int or brake_ticks < 0:
            raise ValueError("brake identity requires a typed input and nonnegative tick count")
        return replace(self, brake_ticks=brake_ticks,
                       inputs=self.inputs + (stop_input,) * brake_ticks)


def _gaits(tier: InputTier) -> tuple[tuple[int, TickInput], ...]:
    return tuple((index, command) for index, command in enumerate(tier.inputs)
                 if not command.jump and (command.forward != 0. or command.strafe != 0.))


def _jump_for(gait: TickInput, tier: InputTier) -> TickInput | None:
    for command in tier.inputs:
        if (command.jump and command.forward == gait.forward
                and command.strafe == gait.strafe and command.sprint == gait.sprint
                and command.movement_yaw_radians == gait.movement_yaw_radians):
            return command
    return None


def primitive_prefixes(tier: InputTier) -> tuple[PrimitivePrefix, ...]:
    """Enumerate stable G(0..20), optional J1 and A(0..16) identities."""
    rows = []
    for gait_index, gait in _gaits(tier):
        jump = _jump_for(gait, tier)
        for ground_ticks in range(21):
            ground = (gait,) * ground_ticks
            rows.append(PrimitivePrefix(tier.tier_id, gait_index,
                                        gait.movement_yaw_radians, ground_ticks,
                                        False, 0, 0, ground))
            if jump is not None:
                for air_ticks in range(17):
                    rows.append(PrimitivePrefix(
                        tier.tier_id, gait_index, gait.movement_yaw_radians,
                        ground_ticks, True, air_ticks, 0,
                        ground + (jump,) + (gait,) * air_ticks,
                    ))
    return tuple(rows)
