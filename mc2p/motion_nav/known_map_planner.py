"""Known ordinary-ground graph construction and deterministic B04 planning."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import heapq
import math

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.jump_up import JumpUpEdge, JumpUpProfile, query_jump_up
from mc2p.motion_nav.world_model import (
    Aabb, BlockPos, CellFact, CellKnowledge, WorldView,
)


WalkNodeId = tuple[int, int, int]
_DIRECTIONS = ((-1, 0), (0, -1), (0, 1), (1, 0))


def _node_id(value: WalkNodeId, name: str = "walk node") -> None:
    if type(value) is not tuple or len(value) != 3 or any(type(part) is not int for part in value):
        raise ContractViolation(f"{name} must be an integer triple")


@dataclass(frozen=True, slots=True)
class KnownMapBounds:
    min_x: int
    max_x: int
    min_feet_y: int
    max_feet_y: int
    min_z: int
    max_z: int
    complete_scope: bool
    extra_top_clearance_cells: int = 0

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (
                self.min_x, self.max_x, self.min_feet_y, self.max_feet_y,
                self.min_z, self.max_z)):
            raise ContractViolation("known map bounds must use integer cells")
        if (self.min_x > self.max_x or self.min_feet_y > self.max_feet_y
                or self.min_z > self.max_z):
            raise ContractViolation("known map bounds are inverted")
        if type(self.complete_scope) is not bool:
            raise ContractViolation("known map completeness must be explicit")
        if (type(self.extra_top_clearance_cells) is not int
                or not 0 <= self.extra_top_clearance_cells <= 8):
            raise ContractViolation("known map extra top clearance must be within 0..8 cells")


@dataclass(frozen=True, slots=True)
class KnownMapSnapshot:
    world: WorldView
    bounds: KnownMapBounds

    def __post_init__(self) -> None:
        if type(self.world) is not WorldView or type(self.bounds) is not KnownMapBounds:
            raise ContractViolation("known map snapshot requires a detached view and bounds")
        if self.world._owner is not None:
            raise ContractViolation("known map snapshot cannot retain the live world owner")


class SnapshotBuildStatus(StrEnum):
    BUILDING = "building"
    COMPLETE = "complete"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class SnapshotBuildProgress:
    status: SnapshotBuildStatus
    scanned_cells: int
    total_cells: int
    snapshot: KnownMapSnapshot | None = None


class KnownMapSnapshotBuilder:
    """Copy one planning prism in caller-chosen bounded slices."""

    def __init__(self, source: WorldView, bounds: KnownMapBounds) -> None:
        if type(source) is not WorldView or type(bounds) is not KnownMapBounds:
            raise ContractViolation("snapshot builder requires a world view and bounds")
        self._session = source.session
        self._geometry_revision = source.geometry_revision
        self._evidence_revision = source.evidence_revision
        self._bounds = bounds
        self._width = bounds.max_x - bounds.min_x + 1
        # Every snapshot includes support below the lowest feet level and the
        # two body cells at the highest feet level.  Abilities that rise above
        # that volume must request their own additional top clearance.
        self._height = (bounds.max_feet_y - bounds.min_feet_y + 3
                        + bounds.extra_top_clearance_cells)
        self._depth = bounds.max_z - bounds.min_z + 1
        self._total = self._width * self._depth * self._height
        self._index = 0
        self._facts: dict[BlockPos, CellFact] = {}
        self._has_unknown = False
        self._complete: KnownMapSnapshot | None = None
        self._stale = False

    def _position(self, index: int) -> BlockPos:
        horizontal, layer = divmod(index, self._height)
        x_offset, z_offset = divmod(horizontal, self._depth)
        return (self._bounds.min_x + x_offset,
                self._bounds.min_feet_y - 1 + layer,
                self._bounds.min_z + z_offset)

    def advance(self, source: WorldView, maximum_cells: int) -> SnapshotBuildProgress:
        if type(source) is not WorldView:
            raise ContractViolation("snapshot advance requires a world view")
        if type(maximum_cells) is not int or maximum_cells < 1:
            raise ContractViolation("snapshot cell budget must be positive")
        if self._complete is not None:
            return SnapshotBuildProgress(
                SnapshotBuildStatus.COMPLETE, self._total, self._total, self._complete,
            )
        if (self._stale or source.session != self._session
                or source.geometry_revision != self._geometry_revision):
            self._stale = True
            return SnapshotBuildProgress(
                SnapshotBuildStatus.STALE, self._index, self._total,
            )
        stop = min(self._total, self._index + maximum_cells)
        try:
            while self._index < stop:
                position = self._position(self._index)
                fact = source.cell(position)
                if fact.knowledge is CellKnowledge.UNKNOWN:
                    self._has_unknown = True
                else:
                    self._facts[position] = fact
                self._index += 1
        except ContractViolation:
            self._stale = True
            return SnapshotBuildProgress(
                SnapshotBuildStatus.STALE, self._index, self._total,
            )
        if self._index < self._total:
            return SnapshotBuildProgress(
                SnapshotBuildStatus.BUILDING, self._index, self._total,
            )
        actual_bounds = KnownMapBounds(
            self._bounds.min_x, self._bounds.max_x,
            self._bounds.min_feet_y, self._bounds.max_feet_y,
            self._bounds.min_z, self._bounds.max_z,
            self._bounds.complete_scope and not self._has_unknown,
            self._bounds.extra_top_clearance_cells,
        )
        # Every fact was already validated while it was copied under the
        # caller's per-step budget. Transfer the owned dictionary in O(1)
        # instead of validating and copying the whole prism on the last step.
        facts = self._facts
        self._facts = {}
        detached = WorldView(
            self._session, self._geometry_revision, self._evidence_revision,
            None, facts,
        )
        self._complete = KnownMapSnapshot(detached, actual_bounds)
        return SnapshotBuildProgress(
            SnapshotBuildStatus.COMPLETE, self._total, self._total, self._complete,
        )


@dataclass(frozen=True, slots=True)
class WalkNode:
    node_id: WalkNodeId
    position: tuple[float, float, float]
    dependencies: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        _node_id(self.node_id)
        if (type(self.position) is not tuple or len(self.position) != 3
                or any(type(value) not in (int, float) or not math.isfinite(float(value))
                       for value in self.position)):
            raise ContractViolation("walk node position must be finite")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("walk node dependencies must be immutable")


@dataclass(frozen=True, slots=True)
class WalkEdge:
    start: WalkNodeId
    end: WalkNodeId
    cost_seconds: float
    dependencies: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        _node_id(self.start, "walk edge start");_node_id(self.end, "walk edge end")
        if (type(self.cost_seconds) not in (int, float)
                or not math.isfinite(float(self.cost_seconds)) or self.cost_seconds <= 0):
            raise ContractViolation("walk edge cost must be positive and finite")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("walk edge dependencies must be immutable")


@dataclass(frozen=True, slots=True)
class WalkGraph:
    world_session: str
    geometry_revision: int
    bounds: KnownMapBounds
    nodes: tuple[WalkNode, ...]
    edges: tuple[WalkEdge | JumpUpEdge, ...]
    has_unsupported: bool

    def __post_init__(self) -> None:
        require_identifier(self.world_session, "walk graph world session")
        require_nonnegative_int(self.geometry_revision, "walk graph geometry revision")
        if type(self.bounds) is not KnownMapBounds:
            raise ContractViolation("walk graph requires known bounds")
        if type(self.nodes) is not tuple or any(type(node) is not WalkNode for node in self.nodes):
            raise ContractViolation("walk graph nodes must be immutable")
        if (type(self.edges) is not tuple
                or any(type(edge) not in (WalkEdge, JumpUpEdge) for edge in self.edges)):
            raise ContractViolation("navigation graph edges must be immutable")
        if type(self.has_unsupported) is not bool:
            raise ContractViolation("walk graph unsupported flag must be explicit")
        ids=tuple(node.node_id for node in self.nodes)
        if ids!=tuple(sorted(set(ids))):
            raise ContractViolation("walk graph nodes must be sorted and unique")
        known=set(ids)
        if any(edge.start not in known or edge.end not in known for edge in self.edges):
            raise ContractViolation("walk edge references a missing node")
        edge_keys=tuple((edge.start,edge.end) for edge in self.edges)
        if edge_keys!=tuple(sorted(set(edge_keys))):
            raise ContractViolation("walk graph edges must be sorted and unique")

    @property
    def complete_scope(self) -> bool:
        return self.bounds.complete_scope


class PlanningStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    PARTIAL = "partial"
    NO_KNOWN_ROUTE = "no_known_route"
    NO_ROUTE_WITHIN_COMPLETE_SCOPE = "no_route_within_complete_scope"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    sequence: int
    request_id: str
    goal_id: str
    goal_revision: int
    world_session: str
    start: WalkNodeId
    goal: WalkNodeId
    maximum_expansions: int = 100_000

    def __post_init__(self) -> None:
        require_nonnegative_int(self.sequence, "planning request sequence")
        require_identifier(self.request_id, "planning request id")
        require_identifier(self.goal_id, "planning goal id")
        require_nonnegative_int(self.goal_revision, "planning goal revision")
        require_identifier(self.world_session, "planning world session")
        _node_id(self.start, "planning start");_node_id(self.goal, "planning goal")
        if type(self.maximum_expansions) is not int or not 1<=self.maximum_expansions<=1_000_000:
            raise ContractViolation("planning expansion budget must be within 1..1000000")


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    request_sequence: int
    request_id: str
    goal_id: str
    goal_revision: int
    world_session: str
    geometry_revision: int
    planning_start: WalkNodeId
    planning_goal: WalkNodeId
    status: PlanningStatus
    path: tuple[WalkNode, ...]
    segments: tuple[WalkEdge | JumpUpEdge, ...]
    total_cost_seconds: float | None
    dependencies: tuple[BlockPos, ...]
    expanded_nodes: int


def _body_at(node_id: WalkNodeId) -> Aabb:
    x,y,z=node_id
    return Aabb(x+.2,float(y),z+.2,x+.8,float(y)+1.8,z+.8)


def _walk_state(world: WorldView, node_id: WalkNodeId,
                profile: GroundMotionProfile) -> tuple[QueryStatus,tuple[BlockPos,...]]:
    body=_body_at(node_id)
    clearance=sweep(body,(0.0,0.0,0.0),world)
    support=query_support(body,world)
    dependencies=tuple(sorted(set(clearance.dependencies+support.dependencies)))
    if clearance.status is QueryStatus.BLOCKED or support.status is QueryStatus.BLOCKED:
        return QueryStatus.BLOCKED,dependencies
    if (clearance.status is QueryStatus.UNSUPPORTED
            or support.status is QueryStatus.UNSUPPORTED):
        return QueryStatus.UNSUPPORTED,dependencies
    if (clearance.status is QueryStatus.NEEDS_INFORMATION
            or support.status is QueryStatus.NEEDS_INFORMATION):
        return QueryStatus.NEEDS_INFORMATION,dependencies
    if (profile.support_materials
            and not set(support.support_materials).issubset(profile.support_materials)):
        return QueryStatus.UNSUPPORTED,dependencies
    return QueryStatus.FEASIBLE,dependencies


def _walk_edge(world: WorldView, start: WalkNodeId, end: WalkNodeId,
               profile: GroundMotionProfile) -> tuple[QueryStatus,tuple[BlockPos,...]]:
    body=_body_at(start);dx=end[0]-start[0];dz=end[2]-start[2]
    movement=sweep(body,(float(dx),0.0,float(dz)),world)
    dependencies=set(movement.dependencies)
    if movement.status is not QueryStatus.FEASIBLE:
        return movement.status,tuple(sorted(dependencies))
    unsupported=False
    missing=False
    for fraction in (.25,.5,.75,1.0):
        support=query_support(body.moved(dx*fraction,0.0,dz*fraction),world)
        dependencies.update(support.dependencies)
        if support.status is QueryStatus.BLOCKED:
            return QueryStatus.BLOCKED,tuple(sorted(dependencies))
        if support.status is QueryStatus.UNSUPPORTED:
            unsupported=True
        elif support.status is QueryStatus.NEEDS_INFORMATION:
            missing=True
        elif (profile.support_materials
              and not set(support.support_materials).issubset(profile.support_materials)):
            unsupported=True
    if unsupported:
        return QueryStatus.UNSUPPORTED,tuple(sorted(dependencies))
    if missing:
        return QueryStatus.NEEDS_INFORMATION,tuple(sorted(dependencies))
    return QueryStatus.FEASIBLE,tuple(sorted(dependencies))


def build_walk_graph(world: WorldView, bounds: KnownMapBounds,
                     profile: GroundMotionProfile,
                     jump_profile: JumpUpProfile | None = None) -> WalkGraph:
    if type(world) is not WorldView or type(bounds) is not KnownMapBounds:
        raise ContractViolation("walk graph construction requires a world view and bounds")
    if type(profile) is not GroundMotionProfile:
        raise ContractViolation("walk graph construction requires a motion profile")
    if jump_profile is not None and type(jump_profile) is not JumpUpProfile:
        raise ContractViolation("navigation graph requires a JumpUp profile or None")
    nodes=[];complete=bounds.complete_scope;has_unsupported=False
    for x in range(bounds.min_x,bounds.max_x+1):
        for y in range(bounds.min_feet_y,bounds.max_feet_y+1):
            for z in range(bounds.min_z,bounds.max_z+1):
                node_id=(x,y,z)
                status,dependencies=_walk_state(world,node_id,profile)
                complete=complete and status is not QueryStatus.NEEDS_INFORMATION
                has_unsupported=has_unsupported or status is QueryStatus.UNSUPPORTED
                if status is QueryStatus.FEASIBLE:
                    nodes.append(WalkNode(node_id,(x+.5,float(y),z+.5),dependencies))
    known={node.node_id for node in nodes};edges=[]
    for start in sorted(known):
        for dx,dz in _DIRECTIONS:
            end=(start[0]+dx,start[1],start[2]+dz)
            if end not in known:continue
            status,dependencies=_walk_edge(world,start,end,profile)
            complete=complete and status is not QueryStatus.NEEDS_INFORMATION
            has_unsupported=has_unsupported or status is QueryStatus.UNSUPPORTED
            if status is QueryStatus.FEASIBLE:
                edges.append(WalkEdge(start,end,1.0/profile.maximum_speed_blocks_per_second,
                                      dependencies))
        if jump_profile is not None and start[1] < bounds.max_feet_y:
            for dx,dz in _DIRECTIONS:
                end=(start[0]+dx,start[1]+1,start[2]+dz)
                if end not in known:continue
                result=query_jump_up(world,start,end,jump_profile)
                complete=complete and result.status is not QueryStatus.NEEDS_INFORMATION
                has_unsupported=has_unsupported or result.status is QueryStatus.UNSUPPORTED
                if result.status is QueryStatus.FEASIBLE:
                    edges.append(JumpUpEdge(
                        start,end,jump_profile.profile_id,(dx,dz),
                        jump_profile.cost_seconds,result.dependencies,
                    ))
    actual_bounds=KnownMapBounds(bounds.min_x,bounds.max_x,
                                 bounds.min_feet_y,bounds.max_feet_y,
                                 bounds.min_z,bounds.max_z,complete,
                                 bounds.extra_top_clearance_cells)
    return WalkGraph(world.session.value,world.geometry_revision,actual_bounds,
                     tuple(nodes),tuple(sorted(edges,key=lambda edge:(edge.start,edge.end))),
                     has_unsupported)


def plan_known_snapshot(snapshot: KnownMapSnapshot, profile: GroundMotionProfile,
                        request: PlanningRequest,
                        jump_profile: JumpUpProfile | None = None) -> RouteCandidate:
    """Run A* while materializing only the known nodes and edges it reaches."""
    if (type(snapshot) is not KnownMapSnapshot
            or type(profile) is not GroundMotionProfile
            or type(request) is not PlanningRequest):
        raise ContractViolation("snapshot planning requires snapshot, profile and request")
    world, bounds = snapshot.world, snapshot.bounds

    def result(status: PlanningStatus, path_ids: tuple[WalkNodeId, ...] = (),
               segments: tuple[WalkEdge | JumpUpEdge, ...] = (), cost: float | None = None,
               expanded: int = 0) -> RouteCandidate:
        path = tuple(nodes[node_id] for node_id in path_ids)
        dependencies = tuple(sorted(
            {cell for node in path for cell in node.dependencies}
            | {cell for edge in segments for cell in edge.dependencies}
        ))
        return RouteCandidate(
            request.sequence, request.request_id, request.goal_id,
            request.goal_revision, request.world_session, world.geometry_revision,
            request.start, request.goal, status, path, segments, cost,
            dependencies, expanded,
        )

    nodes: dict[WalkNodeId, WalkNode] = {}
    node_state: dict[WalkNodeId, QueryStatus] = {}
    edges: dict[tuple[WalkNodeId, WalkNodeId], WalkEdge | JumpUpEdge | None] = {}
    saw_unsupported = False

    if world.session.value != request.world_session:
        return result(PlanningStatus.UNSUPPORTED)

    def inside(node_id: WalkNodeId) -> bool:
        return (bounds.min_x <= node_id[0] <= bounds.max_x
                and bounds.min_feet_y <= node_id[1] <= bounds.max_feet_y
                and bounds.min_z <= node_id[2] <= bounds.max_z)

    def node(node_id: WalkNodeId) -> WalkNode | None:
        nonlocal saw_unsupported
        if not inside(node_id):
            return None
        if node_id not in node_state:
            status, dependencies = _walk_state(world, node_id, profile)
            node_state[node_id] = status
            saw_unsupported = saw_unsupported or status is QueryStatus.UNSUPPORTED
            if status is QueryStatus.FEASIBLE:
                nodes[node_id] = WalkNode(
                    node_id,
                    (node_id[0] + .5, float(node_id[1]), node_id[2] + .5),
                    dependencies,
                )
        return nodes.get(node_id)

    def edge(start: WalkNodeId, end: WalkNodeId) -> WalkEdge | JumpUpEdge | None:
        nonlocal saw_unsupported
        key = start, end
        if key not in edges:
            if node(start) is None or node(end) is None:
                edges[key] = None
            elif end[1] == start[1]:
                status, dependencies = _walk_edge(world, start, end, profile)
                saw_unsupported = saw_unsupported or status is QueryStatus.UNSUPPORTED
                edges[key] = (WalkEdge(
                    start, end, 1.0 / profile.maximum_speed_blocks_per_second,
                    dependencies,
                ) if status is QueryStatus.FEASIBLE else None)
            elif jump_profile is not None and end[1] == start[1] + 1:
                jump = query_jump_up(world, start, end, jump_profile)
                saw_unsupported = saw_unsupported or jump.status is QueryStatus.UNSUPPORTED
                edges[key] = (JumpUpEdge(
                    start, end, jump_profile.profile_id,
                    (end[0] - start[0], end[2] - start[2]),
                    jump_profile.cost_seconds, jump.dependencies,
                ) if jump.status is QueryStatus.FEASIBLE else None)
            else:
                edges[key] = None
        return edges[key]

    if node(request.start) is None or node(request.goal) is None:
        status = (PlanningStatus.UNSUPPORTED if saw_unsupported else
                  PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
                  if bounds.complete_scope else PlanningStatus.NO_KNOWN_ROUTE)
        return result(status)

    step_cost = 1.0 / profile.maximum_speed_blocks_per_second
    def heuristic(node_id: WalkNodeId) -> float:
        if jump_profile is not None:
            return 0.0
        return (abs(node_id[0] - request.goal[0])
                + abs(node_id[2] - request.goal[2])) * step_cost
    frontier = [(heuristic(request.start), 0.0, request.start)]
    costs = {request.start: 0.0}
    previous: dict[WalkNodeId, WalkEdge | JumpUpEdge] = {}
    expanded = 0
    while frontier:
        _, cost, current = heapq.heappop(frontier)
        if cost > costs.get(current, math.inf) + 1e-12:
            continue
        if current == request.goal:
            path = [current]
            segments: list[WalkEdge | JumpUpEdge] = []
            while path[-1] != request.start:
                segment = previous[path[-1]]
                segments.append(segment)
                path.append(segment.start)
            path.reverse(); segments.reverse()
            return result(
                PlanningStatus.COMPLETE, tuple(path), tuple(segments),
                cost, expanded,
            )
        expanded += 1
        if expanded > request.maximum_expansions:
            return result(PlanningStatus.TIMEOUT, expanded=expanded)
        neighbors=[(current[0]+dx,current[1],current[2]+dz) for dx,dz in _DIRECTIONS]
        if jump_profile is not None and current[1] < bounds.max_feet_y:
            neighbors += [(current[0]+dx,current[1]+1,current[2]+dz) for dx,dz in _DIRECTIONS]
        for other in neighbors:
            segment = edge(current, other)
            if segment is None:
                continue
            candidate = cost + segment.cost_seconds
            if candidate + 1e-12 < costs.get(other, math.inf):
                costs[other] = candidate
                previous[other] = segment
                heapq.heappush(frontier, (candidate + heuristic(other), candidate, other))
    status = (PlanningStatus.UNSUPPORTED if saw_unsupported else
              PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
              if bounds.complete_scope else PlanningStatus.NO_KNOWN_ROUTE)
    return result(status, expanded=expanded)


def _candidate(request: PlanningRequest, graph: WalkGraph, status: PlanningStatus,
               path_ids: tuple[WalkNodeId,...], segments: tuple[WalkEdge | JumpUpEdge,...],
               cost: float | None, expanded: int) -> RouteCandidate:
    by_id={node.node_id:node for node in graph.nodes}
    path=tuple(by_id[node_id] for node_id in path_ids)
    dependencies=tuple(sorted({cell for node in path for cell in node.dependencies}
                              | {cell for edge in segments for cell in edge.dependencies}))
    return RouteCandidate(request.sequence,request.request_id,request.goal_id,
                          request.goal_revision,request.world_session,
                          graph.geometry_revision,request.start,request.goal,status,path,
                          segments,cost,dependencies,expanded)


def astar_plan(graph: WalkGraph, request: PlanningRequest) -> RouteCandidate:
    if type(graph) is not WalkGraph or type(request) is not PlanningRequest:
        raise ContractViolation("A* requires a walk graph and planning request")
    if graph.world_session!=request.world_session:
        return _candidate(request,graph,PlanningStatus.UNSUPPORTED,(),(),None,0)
    nodes={node.node_id for node in graph.nodes}
    if request.start not in nodes or request.goal not in nodes:
        status=(PlanningStatus.UNSUPPORTED if graph.has_unsupported else
                PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE if graph.complete_scope
                else PlanningStatus.NO_KNOWN_ROUTE)
        return _candidate(request,graph,status,(),(),None,0)
    adjacency={node:[] for node in nodes}
    minimum_cost=math.inf
    for edge in graph.edges:
        adjacency[edge.start].append(edge);minimum_cost=min(minimum_cost,edge.cost_seconds)
    minimum_cost=0.0 if not math.isfinite(minimum_cost) else minimum_cost
    has_jump = any(type(edge) is JumpUpEdge for edge in graph.edges)
    def heuristic(node: WalkNodeId) -> float:
        if has_jump:
            return 0.0
        return (abs(node[0]-request.goal[0])+abs(node[2]-request.goal[2]))*minimum_cost
    frontier=[(heuristic(request.start),0.0,request.start)]
    costs={request.start:0.0};previous:dict[WalkNodeId,WalkEdge | JumpUpEdge]={};expanded=0
    while frontier:
        _,cost,node=heapq.heappop(frontier)
        if cost>costs.get(node,math.inf)+1e-12:continue
        if node==request.goal:
            path=[node];segments=[]
            while path[-1]!=request.start:
                edge=previous[path[-1]];segments.append(edge);path.append(edge.start)
            path.reverse();segments.reverse()
            return _candidate(request,graph,PlanningStatus.COMPLETE,tuple(path),
                              tuple(segments),cost,expanded)
        expanded+=1
        if expanded>request.maximum_expansions:
            return _candidate(request,graph,PlanningStatus.TIMEOUT,(),(),None,expanded)
        for edge in adjacency[node]:
            candidate=cost+edge.cost_seconds
            if candidate+1e-12<costs.get(edge.end,math.inf):
                costs[edge.end]=candidate;previous[edge.end]=edge
                heapq.heappush(frontier,(candidate+heuristic(edge.end),candidate,edge.end))
    status=(PlanningStatus.UNSUPPORTED if graph.has_unsupported else
            PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE if graph.complete_scope
            else PlanningStatus.NO_KNOWN_ROUTE)
    return _candidate(request,graph,status,(),(),None,expanded)


def dijkstra_reference(graph: WalkGraph, start: WalkNodeId,
                       goal: WalkNodeId) -> tuple[float,tuple[WalkNodeId,...]] | None:
    """Independent zero-heuristic reference used by B04 acceptance."""
    nodes={node.node_id for node in graph.nodes}
    if start not in nodes or goal not in nodes:return None
    adjacency={node:[] for node in nodes}
    for edge in graph.edges:adjacency[edge.start].append((edge.end,edge.cost_seconds))
    queue=[(0.0,start)];costs={start:0.0};previous={}
    while queue:
        cost,node=heapq.heappop(queue)
        if cost!=costs.get(node):continue
        if node==goal:
            path=[node]
            while path[-1]!=start:path.append(previous[path[-1]])
            path.reverse();return cost,tuple(path)
        for other,edge_cost in adjacency[node]:
            candidate=cost+edge_cost
            if candidate<costs.get(other,math.inf):
                costs[other]=candidate;previous[other]=node
                heapq.heappush(queue,(candidate,other))
    return None
