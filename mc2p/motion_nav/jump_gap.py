"""Typed planning edge for a calibrated gap jump."""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.air_motion import AirMotionProfile, AirMotionQuery, query_air_motion
from mc2p.motion_nav.movement_transition import MovementMode, MovementTransition, ResourceChange
from mc2p.motion_nav.support_surfaces import SupportSurface, SurfaceNodeId
from mc2p.motion_nav.world_model import BlockPos, WorldView


@dataclass(frozen=True, slots=True)
class JumpGapEdge:
    start: SurfaceNodeId
    end: SurfaceNodeId
    profile_id: str
    cost_seconds: float
    dependencies: tuple[BlockPos, ...]
    transition: MovementTransition | None = None

    def __post_init__(self) -> None:
        if type(self.start) is not SurfaceNodeId or type(self.end) is not SurfaceNodeId:
            raise ContractViolation("gap jump edge requires surface node ids")
        require_identifier(self.profile_id, "gap jump profile id")
        if self.cost_seconds <= 0 or type(self.dependencies) is not tuple:
            raise ContractViolation("gap jump edge cost and dependencies are invalid")
        if self.transition is not None and type(self.transition) is not MovementTransition:
            raise ContractViolation("gap jump transition must be typed")

    @property
    def resource_change(self) -> ResourceChange:
        return self.transition.resource_change if self.transition else ResourceChange()


def query_jump_gap(world: WorldView, start: SupportSurface, end: SupportSurface,
                   profile: AirMotionProfile) -> AirMotionQuery:
    if type(profile) is not AirMotionProfile or profile.mode is not MovementMode.JUMP_GAP:
        raise ContractViolation("gap jump query requires a gap jump profile")
    return query_air_motion(world, start, end, profile)
