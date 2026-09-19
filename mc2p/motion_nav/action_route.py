"""Typed executable route segments shared by planning admission and execution."""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.fixed_route import FixedRoute
from mc2p.motion_nav.jump_up import JumpUpEdge
from mc2p.motion_nav.known_map_planner import WalkNodeId
from mc2p.motion_nav.world_model import BlockPos


@dataclass(frozen=True, slots=True)
class WalkSegment:
    fixed_route: FixedRoute
    node_ids: tuple[WalkNodeId, ...]
    dependencies: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        if type(self.fixed_route) is not FixedRoute:
            raise ContractViolation("walk segment requires a fixed route")
        if type(self.node_ids) is not tuple or len(self.node_ids) < 1:
            raise ContractViolation("walk segment requires immutable graph nodes")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("walk segment dependencies must be immutable")


@dataclass(frozen=True, slots=True)
class JumpUpSegment:
    edge: JumpUpEdge
    dependencies: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        if type(self.edge) is not JumpUpEdge:
            raise ContractViolation("JumpUp segment requires a JumpUp edge")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("JumpUp segment dependencies must be immutable")


RouteAction = WalkSegment | JumpUpSegment


@dataclass(frozen=True, slots=True)
class ActionRoute:
    route_id: str
    actions: tuple[RouteAction, ...]

    def __post_init__(self) -> None:
        require_identifier(self.route_id, "action route id")
        if (type(self.actions) is not tuple or not self.actions
                or any(type(action) not in (WalkSegment, JumpUpSegment)
                       for action in self.actions)):
            raise ContractViolation("action route requires typed immutable actions")

    @property
    def dependencies(self) -> tuple[BlockPos, ...]:
        return tuple(sorted({cell for action in self.actions
                             for cell in action.dependencies}))
