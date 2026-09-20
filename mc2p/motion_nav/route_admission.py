"""Control-thread admission boundary for B04 background route candidates."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route import (
    ActionRoute, ControlledDropSegment, JumpGapSegment, JumpUpSegment,
    StepSegment, WalkSegment,
)
from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.jump_up import JumpUpEdge
from mc2p.motion_nav.movement_transition import compose_movement_transitions
from mc2p.motion_nav.movement_transition import GoalState, ResourceState
from mc2p.motion_nav.known_map_planner import (
    PlanningStatus, RouteCandidate, WalkEdge, WalkNode, WalkNodeId,
    SurfacePlanningStatus, SurfaceRouteCandidate, SurfaceWalkEdge,
    SurfaceControlledDropEdge, SurfaceJumpGapEdge, SurfaceJumpUpEdge,
)
from mc2p.motion_nav.step_transition import StepEdge
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.runtime_adapter import NavigationFrame
from mc2p.motion_nav.world_model import BlockPos


class AdmissionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ExecutableCorridor:
    node_ids: tuple[WalkNodeId | SurfaceNodeId, ...]
    dependencies: tuple[BlockPos, ...]
    length_blocks: float
    stop_node: WalkNodeId | SurfaceNodeId

    def affected_by(self, changed_cells: tuple[BlockPos, ...]) -> bool:
        return bool(set(self.dependencies).intersection(changed_cells))


@dataclass(frozen=True, slots=True)
class ActiveRoute:
    route_id: str
    route_revision: int
    source_request_id: str
    goal_id: str
    goal_revision: int
    world_session: str
    fixed_route: FixedRoute | None
    fixed_route_length_blocks: float
    connection_length_blocks: float
    connection_dependencies: tuple[BlockPos, ...]
    corridor: ExecutableCorridor
    action_route: ActionRoute
    goal_state: GoalState | None = None


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    status: AdmissionStatus
    reason: str
    route: ActiveRoute | None = None


class CorridorStatus(StrEnum):
    READY = "ready"
    BLOCKED_BY_CHANGE = "blocked_by_change"


@dataclass(frozen=True, slots=True)
class CorridorUpdate:
    status: CorridorStatus
    route: ActiveRoute
    reason: str


class RouteAdmitter:
    """Validate a bounded prefix before a background candidate becomes active."""

    def __init__(self, *, maximum_corridor_blocks: float = 8.0) -> None:
        if (type(maximum_corridor_blocks) not in (int,float)
                or not math.isfinite(float(maximum_corridor_blocks))
                or maximum_corridor_blocks<=0):
            raise ContractViolation("corridor length must be positive and finite")
        self.maximum_corridor_blocks=float(maximum_corridor_blocks)

    @staticmethod
    def _route_id(candidate: RouteCandidate) -> str:
        identity=dict(world=candidate.world_session,goal=candidate.goal_id,
                      goal_revision=candidate.goal_revision,
                      path=[node.node_id for node in candidate.path],
                      actions=[{
                      "kind": "jump_up" if type(edge) is JumpUpEdge else "walk",
                      "start": edge.start,
                      "end": edge.end,
                      "profile": (edge.profile_id if type(edge) is JumpUpEdge
                                  else (edge.transition.trajectory_profile_id
                                        if edge.transition is not None else None)),
                      "mode": (None if type(edge) is JumpUpEdge or edge.transition is None
                               else edge.transition.mode.value),
                      } for edge in candidate.segments])
        return hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:24]

    @staticmethod
    def _simplified_path(candidate: RouteCandidate):
        if len(candidate.path)<=2:return candidate.path
        kept=[candidate.path[0]]
        previous_direction=None
        for first,second in zip(candidate.path,candidate.path[1:]):
            direction=(second.node_id[0]-first.node_id[0],
                       second.node_id[1]-first.node_id[1],
                       second.node_id[2]-first.node_id[2])
            if previous_direction is not None and direction!=previous_direction:
                kept.append(first)
            previous_direction=direction
        kept.append(candidate.path[-1])
        return tuple(kept)

    @staticmethod
    def _simplified_nodes(nodes: tuple[WalkNode, ...]) -> tuple[WalkNode, ...]:
        if len(nodes) <= 2:
            return nodes
        kept = [nodes[0]]
        previous_direction = None
        for first, second in zip(nodes, nodes[1:]):
            direction = (
                second.node_id[0] - first.node_id[0],
                second.node_id[1] - first.node_id[1],
                second.node_id[2] - first.node_id[2],
            )
            if previous_direction is not None and direction != previous_direction:
                kept.append(first)
            previous_direction = direction
        kept.append(nodes[-1])
        return tuple(kept)

    @classmethod
    def _action_route(cls, candidate: RouteCandidate, frame: NavigationFrame,
                      connection_length: float,
                      connection_dependencies: tuple[BlockPos, ...],
                      route_id: str) -> ActionRoute | None:
        actions = []
        pending_nodes = [candidate.path[0]]
        pending_points = []
        pending_dependencies = set(connection_dependencies)
        pending_transitions = []
        if connection_length > 1e-9:
            pending_points.append(RoutePoint(
                frame.body.position[0], candidate.path[0].position[1],
                frame.body.position[2],
            ))
        pending_points.append(RoutePoint(*candidate.path[0].position))

        def flush_walk() -> None:
            nonlocal pending_nodes, pending_points, pending_dependencies, pending_transitions
            if len(pending_points) >= 2:
                if connection_length > 1e-9 and len(actions) == 0:
                    graph_nodes = tuple(pending_nodes)
                    route_points = tuple(pending_points)
                else:
                    simplified = cls._simplified_nodes(tuple(pending_nodes))
                    graph_nodes = tuple(node.node_id for node in pending_nodes)
                    route_points = tuple(RoutePoint(*node.position) for node in simplified)
                actions.append(WalkSegment(
                    FixedRoute(f"{route_id}-walk-{len(actions)}", route_points),
                    tuple(node.node_id for node in pending_nodes),
                    tuple(sorted(pending_dependencies)),
                    ((pending_transitions[0] if len(pending_transitions) == 1
                      else compose_movement_transitions(
                          f"{route_id}/walk/{len(actions)}", tuple(pending_transitions),
                      )) if pending_transitions
                     and len(pending_transitions) == len(pending_nodes) - 1
                     else None),
                ))
            pending_nodes = []
            pending_points = []
            pending_dependencies = set()
            pending_transitions = []

        for segment_index, (edge, next_node) in enumerate(
                zip(candidate.segments, candidate.path[1:])):
            if type(edge) is WalkEdge:
                if (pending_transitions and edge.transition is not None
                        and edge.transition.mode is not pending_transitions[-1].mode):
                    flush_walk()
                changes_resources = (edge.transition is not None and any(
                    abs(delta) > 1.0e-12
                    for _, delta in edge.transition.resource_change.deltas
                ))
                if changes_resources:
                    flush_walk()
                if not pending_nodes:
                    previous = candidate.path[segment_index]
                    pending_nodes = [previous]
                    pending_points = [RoutePoint(*previous.position)]
                pending_nodes.append(next_node)
                pending_points.append(RoutePoint(*next_node.position))
                pending_dependencies.update(edge.dependencies)
                pending_dependencies.update(next_node.dependencies)
                if edge.transition is not None:
                    pending_transitions.append(edge.transition)
                if changes_resources:
                    flush_walk()
            else:
                flush_walk()
                assert type(edge) is JumpUpEdge
                actions.append(JumpUpSegment(edge, edge.dependencies, edge.transition))
                pending_nodes = [next_node]
                pending_points = [RoutePoint(*next_node.position)]
                pending_dependencies = set(next_node.dependencies)
        flush_walk()
        return (ActionRoute(
            route_id, tuple(actions), candidate.goal_state,
            candidate.final_resources if candidate.final_resources is not None else ResourceState(),
        ) if actions else None)

    @staticmethod
    def _connection(candidate: RouteCandidate, frame: NavigationFrame
                    ) -> tuple[bool, float, tuple[BlockPos, ...]]:
        first=candidate.path[0].position
        dx,dz=first[0]-frame.body.position[0],first[2]-frame.body.position[2]
        if abs(first[1]-frame.body.position[1])>.10 or math.hypot(dx,dz)>.80:
            return False,0.0,()
        movement=sweep(frame.body.body_box,(dx,0.0,dz),frame.world)
        dependencies=set(movement.dependencies)
        if movement.status is not QueryStatus.FEASIBLE:
            return False,0.0,tuple(sorted(dependencies))
        for fraction in (.5,1.0):
            support=query_support(frame.body.body_box.moved(dx*fraction,0.0,dz*fraction),frame.world)
            dependencies.update(support.dependencies)
            if support.status is not QueryStatus.FEASIBLE:
                return False,0.0,tuple(sorted(dependencies))
        return True,math.hypot(dx,dz),tuple(sorted(dependencies))

    def admit(self, candidate: RouteCandidate, frame: NavigationFrame, *,
              expected_request_id: str, goal_id: str,
              goal_revision: int, changed_cells: tuple[BlockPos,...]) -> AdmissionResult:
        if type(candidate) is not RouteCandidate or type(frame) is not NavigationFrame:
            raise ContractViolation("route admission requires a candidate and navigation frame")
        if type(changed_cells) is not tuple:
            raise ContractViolation("route changes must be immutable")
        if candidate.status is not PlanningStatus.COMPLETE or not candidate.path:
            return AdmissionResult(AdmissionStatus.REJECTED,"candidate_not_complete")
        if candidate.request_id!=expected_request_id:
            return AdmissionResult(AdmissionStatus.REJECTED,"planning_request_replaced")
        if candidate.world_session!=frame.session.value:
            return AdmissionResult(AdmissionStatus.REJECTED,"world_session_changed")
        if candidate.goal_id!=goal_id or candidate.goal_revision!=goal_revision:
            return AdmissionResult(AdmissionStatus.REJECTED,"goal_revision_changed")
        if frame.world.geometry_revision!=candidate.geometry_revision and not changed_cells:
            return AdmissionResult(AdmissionStatus.REJECTED,"world_delta_missing")
        if set(candidate.dependencies).intersection(changed_cells):
            return AdmissionResult(AdmissionStatus.REJECTED,"route_dependencies_changed")
        connected,connection_length,connection_dependencies=self._connection(candidate,frame)
        if not connected:
            return AdmissionResult(AdmissionStatus.REJECTED,"current_body_cannot_connect")

        length=0.0;corridor_nodes=[candidate.path[0]];corridor_segments=[]
        for edge,node in zip(candidate.segments,candidate.path[1:]):
            segment_length=math.dist(candidate.path[len(corridor_segments)].position,node.position)
            if (corridor_segments and connection_length+length+segment_length
                    > self.maximum_corridor_blocks):
                break
            corridor_segments.append(edge);corridor_nodes.append(node);length+=segment_length
        dependencies=tuple(sorted(
            {cell for node in corridor_nodes for cell in node.dependencies}
            | {cell for edge in corridor_segments for cell in edge.dependencies}
            | set(connection_dependencies)
        ))
        route_id=self._route_id(candidate)
        action_route=self._action_route(
            candidate,frame,connection_length,connection_dependencies,route_id,
        )
        if action_route is None:
            return AdmissionResult(AdmissionStatus.REJECTED,"candidate_has_no_actions")
        fixed_route=(action_route.actions[0].fixed_route
                     if len(action_route.actions)==1
                     and type(action_route.actions[0]) is WalkSegment else None)
        full_length=connection_length+sum(
            math.dist(first.position,second.position)
            for first,second in zip(candidate.path,candidate.path[1:]))
        corridor=ExecutableCorridor(tuple(node.node_id for node in corridor_nodes),dependencies,
                                    connection_length+length,corridor_nodes[-1].node_id)
        active=ActiveRoute(route_id,1,candidate.request_id,candidate.goal_id,
                           candidate.goal_revision,candidate.world_session,fixed_route,
                           full_length,connection_length,connection_dependencies,corridor,
                           action_route,candidate.goal_state)
        return AdmissionResult(AdmissionStatus.ACCEPTED,"candidate_admitted",active)

    @staticmethod
    def _surface_route_id(candidate: SurfaceRouteCandidate) -> str:
        def node_value(node_id):
            return [node_id.column_x, node_id.column_z,
                    node_id.vertical_band, node_id.surface_index]
        identity = {
            "world": candidate.world_session,
            "goal": candidate.goal_id,
            "goal_revision": candidate.goal_revision,
            "path": [node_value(node.node_id) for node in candidate.path],
            "actions": [{
                "kind": ("step" if type(edge) is StepEdge else
                          "jump_up" if type(edge) is SurfaceJumpUpEdge else
                          "jump_gap" if type(edge) is SurfaceJumpGapEdge else
                          "controlled_drop"
                          if type(edge) is SurfaceControlledDropEdge else "walk"),
                "start": node_value(edge.start),
                "end": node_value(edge.end),
                "profile": (edge.profile_id if type(edge) is StepEdge else
                            edge.jump_edge.profile_id
                            if type(edge) is SurfaceJumpUpEdge else
                            edge.air_edge.profile_id
                            if type(edge) in (SurfaceJumpGapEdge,
                                              SurfaceControlledDropEdge) else None),
            } for edge in candidate.segments],
        }
        return hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()[:24]

    @classmethod
    def _surface_action_route(
        cls,
        candidate: SurfaceRouteCandidate,
        frame: NavigationFrame,
        connection_length: float,
        connection_dependencies: tuple[BlockPos, ...],
        route_id: str,
    ) -> ActionRoute | None:
        actions = []
        pending_nodes = [candidate.path[0]]
        pending_points = []
        pending_dependencies = set(connection_dependencies)
        pending_transitions = []
        if connection_length > 1.0e-9:
            pending_points.append(RoutePoint(
                frame.body.position[0], candidate.path[0].position[1],
                frame.body.position[2],
            ))
        pending_points.append(RoutePoint(*candidate.path[0].position))

        def flush_walk() -> None:
            nonlocal pending_nodes, pending_points, pending_dependencies, pending_transitions
            if len(pending_points) >= 2:
                actions.append(WalkSegment(
                    FixedRoute(f"{route_id}-walk-{len(actions)}", tuple(pending_points)),
                    tuple(node.node_id for node in pending_nodes),
                    tuple(sorted(pending_dependencies)),
                    ((pending_transitions[0] if len(pending_transitions) == 1
                      else compose_movement_transitions(
                          f"{route_id}/walk/{len(actions)}", tuple(pending_transitions),
                      )) if pending_transitions
                     and len(pending_transitions) == len(pending_nodes) - 1
                     else None),
                ))
            pending_nodes = []
            pending_points = []
            pending_dependencies = set()
            pending_transitions = []

        for index, (edge, next_node) in enumerate(zip(
                candidate.segments, candidate.path[1:])):
            if type(edge) is SurfaceWalkEdge:
                if (pending_transitions
                        and edge.transition.mode is not pending_transitions[-1].mode):
                    flush_walk()
                if not pending_nodes:
                    previous = candidate.path[index]
                    pending_nodes = [previous]
                    pending_points = [RoutePoint(*previous.position)]
                pending_nodes.append(next_node)
                pending_points.append(RoutePoint(*next_node.position))
                pending_dependencies.update(edge.dependencies)
                pending_dependencies.update(next_node.dependencies)
                pending_transitions.append(edge.transition)
            else:
                flush_walk()
                previous = candidate.path[index]
                if type(edge) is StepEdge:
                    actions.append(StepSegment(
                        edge, previous.surface, next_node.surface,
                        edge.dependencies, edge.transition,
                    ))
                elif type(edge) is SurfaceJumpUpEdge:
                    actions.append(JumpUpSegment(
                        edge.jump_edge, edge.dependencies, edge.transition,
                    ))
                elif type(edge) is SurfaceJumpGapEdge:
                    actions.append(JumpGapSegment(
                        edge.air_edge, previous.surface, next_node.surface,
                        edge.dependencies, edge.transition,
                    ))
                else:
                    assert type(edge) is SurfaceControlledDropEdge
                    actions.append(ControlledDropSegment(
                        edge.air_edge, previous.surface, next_node.surface,
                        edge.dependencies, edge.transition,
                    ))
                pending_nodes = [next_node]
                pending_points = [RoutePoint(*next_node.position)]
                pending_dependencies = set(next_node.dependencies)
        flush_walk()
        return (ActionRoute(
            route_id, tuple(actions), candidate.goal_state,
            candidate.final_resources if candidate.final_resources is not None
            else ResourceState(),
        ) if actions else None)

    def admit_surface(
        self,
        candidate: SurfaceRouteCandidate,
        frame: NavigationFrame,
        *,
        expected_request_id: str,
        goal_id: str,
        goal_revision: int,
        changed_cells: tuple[BlockPos, ...],
    ) -> AdmissionResult:
        """Recheck a B07 surface route before it enters the control thread."""
        if type(candidate) is not SurfaceRouteCandidate or type(frame) is not NavigationFrame:
            raise ContractViolation("surface route admission requires candidate and frame")
        if type(changed_cells) is not tuple:
            raise ContractViolation("route changes must be immutable")
        if candidate.status is not SurfacePlanningStatus.COMPLETE or not candidate.path:
            return AdmissionResult(AdmissionStatus.REJECTED, "candidate_not_complete")
        if candidate.request_id != expected_request_id:
            return AdmissionResult(AdmissionStatus.REJECTED, "planning_request_replaced")
        if candidate.world_session != frame.session.value:
            return AdmissionResult(AdmissionStatus.REJECTED, "world_session_changed")
        if candidate.goal_id != goal_id or candidate.goal_revision != goal_revision:
            return AdmissionResult(AdmissionStatus.REJECTED, "goal_revision_changed")
        if frame.world.geometry_revision != candidate.geometry_revision and not changed_cells:
            return AdmissionResult(AdmissionStatus.REJECTED, "world_delta_missing")
        if set(candidate.dependencies).intersection(changed_cells):
            return AdmissionResult(AdmissionStatus.REJECTED, "route_dependencies_changed")
        resource_names = {
            name for name, _ in candidate.initial_resources.values
        } | {
            name for name, _ in candidate.minimum_resources.values
        } | {
            name
            for edge in candidate.segments
            if edge.transition is not None
            for name, _ in edge.transition.minimum_entry_resources.values
        }
        observed_values = []
        for name in sorted(resource_names):
            if name != "food_points":
                return AdmissionResult(
                    AdmissionStatus.REJECTED, "route_resource_unobservable",
                )
            observed_values.append((name, float(frame.body.food_points)))
        resources = ResourceState(tuple(observed_values))
        if not resources.at_least(candidate.minimum_resources):
            return AdmissionResult(
                AdmissionStatus.REJECTED, "route_resources_below_minimum",
            )
        capacity = resources
        for edge in candidate.segments:
            if (edge.transition is not None
                    and not resources.at_least(
                        edge.transition.minimum_entry_resources)):
                return AdmissionResult(
                    AdmissionStatus.REJECTED, "route_entry_resources_unavailable",
                )
            updated = resources.apply(
                edge.resource_change, candidate.minimum_resources, capacity,
            )
            if updated is None:
                return AdmissionResult(
                    AdmissionStatus.REJECTED, "route_resources_unavailable",
                )
            resources = updated
        connected, connection_length, connection_dependencies = self._connection(
            candidate, frame,
        )
        if not connected:
            return AdmissionResult(AdmissionStatus.REJECTED,
                                   "current_body_cannot_connect")
        route_id = self._surface_route_id(candidate)
        action_route = self._surface_action_route(
            candidate, frame, connection_length, connection_dependencies, route_id,
        )
        if action_route is None:
            return AdmissionResult(AdmissionStatus.REJECTED, "candidate_has_no_actions")

        length = 0.0
        corridor_nodes = [candidate.path[0]]
        corridor_segments = []
        for edge, node in zip(candidate.segments, candidate.path[1:]):
            segment_length = math.dist(corridor_nodes[-1].position, node.position)
            if (corridor_segments and connection_length + length + segment_length
                    > self.maximum_corridor_blocks):
                break
            corridor_segments.append(edge)
            corridor_nodes.append(node)
            length += segment_length
        dependencies = tuple(sorted(
            {cell for node in corridor_nodes for cell in node.dependencies}
            | {cell for edge in corridor_segments for cell in edge.dependencies}
            | set(connection_dependencies)
        ))
        full_length = connection_length + sum(
            math.dist(first.position, second.position)
            for first, second in zip(candidate.path, candidate.path[1:])
        )
        corridor = ExecutableCorridor(
            tuple(node.node_id for node in corridor_nodes), dependencies,
            connection_length + length, corridor_nodes[-1].node_id,
        )
        active = ActiveRoute(
            route_id, 1, candidate.request_id, candidate.goal_id,
            candidate.goal_revision, candidate.world_session, None,
            full_length, connection_length, connection_dependencies, corridor,
            action_route, candidate.goal_state,
        )
        return AdmissionResult(AdmissionStatus.ACCEPTED,
                               "candidate_admitted", active)


class ActiveRouteTracker:
    """Own corridor progress and remembered route changes for one admitted route."""

    def __init__(self, route: ActiveRoute,
                 candidate: RouteCandidate | SurfaceRouteCandidate,
                 *, maximum_corridor_blocks: float = 8.0) -> None:
        if (type(route) is not ActiveRoute
                or type(candidate) not in (RouteCandidate, SurfaceRouteCandidate)):
            raise ContractViolation("route tracker requires an active route and source candidate")
        if route.source_request_id!=candidate.request_id:
            raise ContractViolation("active route and candidate identities differ")
        if (type(maximum_corridor_blocks) not in (int,float)
                or not math.isfinite(float(maximum_corridor_blocks))
                or maximum_corridor_blocks<=0):
            raise ContractViolation("corridor length must be positive and finite")
        self.route=route;self.candidate=candidate
        self.maximum_corridor_blocks=float(maximum_corridor_blocks)
        self._invalidated:set[BlockPos]=set()

    def apply_changes(self, changed_cells: tuple[BlockPos,...]) -> None:
        if type(changed_cells) is not tuple:
            raise ContractViolation("route tracker changes must be immutable")
        dependencies=(set(self.candidate.dependencies)
                      | set(self.route.connection_dependencies))
        self._invalidated.update(cell for cell in changed_cells if cell in dependencies)

    def update(self, progress_blocks: float) -> CorridorUpdate:
        if (type(progress_blocks) not in (int,float)
                or not math.isfinite(float(progress_blocks)) or progress_blocks<0):
            raise ContractViolation("route progress must be finite and nonnegative")
        remaining_connection=max(
            0.0,self.route.connection_length_blocks-progress_blocks,
        )
        graph_progress=max(0.0,progress_blocks-self.route.connection_length_blocks)
        lengths=[0.0]
        for first,second in zip(self.candidate.path,self.candidate.path[1:]):
            lengths.append(lengths[-1]+math.dist(first.position,second.position))
        start=0
        while start+1<len(lengths) and lengths[start+1]<=graph_progress+1e-9:
            start+=1
        nodes=[self.candidate.path[start]];segments=[];length=0.0
        for edge,node in zip(self.candidate.segments[start:],self.candidate.path[start+1:]):
            segment_length=math.dist(nodes[-1].position,node.position)
            if (segments and remaining_connection+length+segment_length
                    > self.maximum_corridor_blocks):break
            segments.append(edge);nodes.append(node);length+=segment_length
        dependencies=tuple(sorted(
            {cell for node in nodes for cell in node.dependencies}
            | {cell for edge in segments for cell in edge.dependencies}
            | (set(self.route.connection_dependencies)
               if remaining_connection>1e-9 else set())
        ))
        corridor=ExecutableCorridor(tuple(node.node_id for node in nodes),dependencies,
                                    remaining_connection+length,nodes[-1].node_id)
        route=ActiveRoute(self.route.route_id,self.route.route_revision,
                          self.route.source_request_id,self.route.goal_id,
                          self.route.goal_revision,self.route.world_session,
                          self.route.fixed_route,self.route.fixed_route_length_blocks,
                          self.route.connection_length_blocks,
                          self.route.connection_dependencies,corridor,
                          self.route.action_route,self.route.goal_state)
        self.route=route
        if self._invalidated.intersection(dependencies):
            return CorridorUpdate(CorridorStatus.BLOCKED_BY_CHANGE,route,
                                  "active_corridor_dependency_changed")
        return CorridorUpdate(CorridorStatus.READY,route,"active_corridor_ready")
