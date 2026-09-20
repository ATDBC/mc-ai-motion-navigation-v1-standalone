"""Typed executable route segments shared by planning admission and execution."""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.controlled_drop import ControlledDropEdge
from mc2p.motion_nav.fixed_route import FixedRoute
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.jump_up import JumpUpEdge
from mc2p.motion_nav.known_map_planner import WalkNodeId
from mc2p.motion_nav.movement_transition import (
    GoalState, MovementTransition, ResourceState,
)
from mc2p.motion_nav.world_model import BlockPos
from mc2p.motion_nav.step_transition import StepEdge
from mc2p.motion_nav.support_surfaces import SupportSurface, SurfaceNodeId


@dataclass(frozen=True, slots=True)
class WalkSegment:
    fixed_route: FixedRoute
    node_ids: tuple[WalkNodeId, ...]
    dependencies: tuple[BlockPos, ...]
    transition: MovementTransition | None = None

    def __post_init__(self) -> None:
        if type(self.fixed_route) is not FixedRoute:
            raise ContractViolation("walk segment requires a fixed route")
        if type(self.node_ids) is not tuple or len(self.node_ids) < 1:
            raise ContractViolation("walk segment requires immutable graph nodes")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("walk segment dependencies must be immutable")
        if self.transition is not None and type(self.transition) is not MovementTransition:
            raise ContractViolation("walk segment transition must be typed")


@dataclass(frozen=True, slots=True)
class JumpUpSegment:
    edge: JumpUpEdge
    dependencies: tuple[BlockPos, ...]
    transition: MovementTransition | None = None

    def __post_init__(self) -> None:
        if type(self.edge) is not JumpUpEdge:
            raise ContractViolation("JumpUp segment requires a JumpUp edge")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("JumpUp segment dependencies must be immutable")
        if self.transition is not None and type(self.transition) is not MovementTransition:
            raise ContractViolation("JumpUp segment transition must be typed")


@dataclass(frozen=True, slots=True)
class StepSegment:
    edge: StepEdge
    start_surface: SupportSurface
    end_surface: SupportSurface
    dependencies: tuple[BlockPos, ...]
    transition: MovementTransition | None = None

    def __post_init__(self) -> None:
        if type(self.edge) is not StepEdge:
            raise ContractViolation("Step segment requires a Step edge")
        if (type(self.start_surface) is not SupportSurface
                or type(self.end_surface) is not SupportSurface):
            raise ContractViolation("Step segment requires endpoint surfaces")
        if (self.edge.start != self.start_surface.node_id
                or self.edge.end != self.end_surface.node_id):
            raise ContractViolation("Step segment surfaces do not match its edge")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("Step segment dependencies must be immutable")
        if self.transition is not None and type(self.transition) is not MovementTransition:
            raise ContractViolation("Step segment transition must be typed")


@dataclass(frozen=True, slots=True)
class JumpGapSegment:
    edge: JumpGapEdge
    start_surface: SupportSurface
    end_surface: SupportSurface
    dependencies: tuple[BlockPos, ...]
    transition: MovementTransition | None = None

    def __post_init__(self) -> None:
        if type(self.edge) is not JumpGapEdge:
            raise ContractViolation("gap jump segment requires a typed edge")
        if (type(self.start_surface) is not SupportSurface
                or type(self.end_surface) is not SupportSurface
                or self.edge.start != self.start_surface.node_id
                or self.edge.end != self.end_surface.node_id):
            raise ContractViolation("gap jump segment surfaces do not match its edge")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("gap jump segment dependencies must be immutable")
        if self.transition is not None and type(self.transition) is not MovementTransition:
            raise ContractViolation("gap jump segment transition must be typed")


@dataclass(frozen=True, slots=True)
class ControlledDropSegment:
    edge: ControlledDropEdge
    start_surface: SupportSurface
    end_surface: SupportSurface
    dependencies: tuple[BlockPos, ...]
    transition: MovementTransition | None = None

    def __post_init__(self) -> None:
        if type(self.edge) is not ControlledDropEdge:
            raise ContractViolation("controlled drop segment requires a typed edge")
        if (type(self.start_surface) is not SupportSurface
                or type(self.end_surface) is not SupportSurface
                or self.edge.start != self.start_surface.node_id
                or self.edge.end != self.end_surface.node_id):
            raise ContractViolation("controlled drop segment surfaces do not match its edge")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("controlled drop segment dependencies must be immutable")
        if self.transition is not None and type(self.transition) is not MovementTransition:
            raise ContractViolation("controlled drop segment transition must be typed")


RouteAction = (
    WalkSegment | JumpUpSegment | StepSegment
    | JumpGapSegment | ControlledDropSegment
)


@dataclass(frozen=True, slots=True)
class ActionRoute:
    route_id: str
    actions: tuple[RouteAction, ...]
    goal_state: GoalState | None = None
    final_resources: ResourceState = ResourceState()

    def __post_init__(self) -> None:
        require_identifier(self.route_id, "action route id")
        if (type(self.actions) is not tuple or not self.actions
                or any(type(action) not in (WalkSegment, JumpUpSegment, StepSegment)
                       and type(action) not in (JumpGapSegment, ControlledDropSegment)
                       for action in self.actions)):
            raise ContractViolation("action route requires typed immutable actions")
        if self.goal_state is not None and type(self.goal_state) is not GoalState:
            raise ContractViolation("action route goal state must be typed")
        if type(self.final_resources) is not ResourceState:
            raise ContractViolation("action route final resources must be typed")

    @property
    def dependencies(self) -> tuple[BlockPos, ...]:
        return tuple(sorted({cell for action in self.actions
                             for cell in action.dependencies}))
