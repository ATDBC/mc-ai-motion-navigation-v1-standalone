"""Known ordinary-ground graph construction and deterministic B04 planning."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import heapq
import math

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.motion_nav.block_motion_traits import unsupported_motion_cells
from mc2p.motion_nav.air_motion import AirMotionProfile
from mc2p.motion_nav.controlled_drop import ControlledDropEdge, query_controlled_drop
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.jump_gap import JumpGapEdge, query_jump_gap
from mc2p.motion_nav.jump_up import JumpUpEdge, JumpUpProfile, query_jump_up
from mc2p.motion_nav.movement_transition import (
    CancellationMode, GoalState, GoalSupport, MovementMode, MovementStateClass,
    MovementTransition, ResourceChange, ResourceState,
)
from mc2p.motion_nav.world_model import (
    Aabb, BlockPos, COLLISION_OWNER_BELOW_REACH_CELLS, CellFact, CellKnowledge,
    WorldView,
)
from mc2p.motion_nav.step_transition import StepEdge, StepProfile, query_step
from mc2p.motion_nav.support_surfaces import (
    SupportSurface, SurfaceNodeId, query_support_surfaces,
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
        self._owner_reach = COLLISION_OWNER_BELOW_REACH_CELLS
        self._width = bounds.max_x - bounds.min_x + 1
        # Every snapshot includes support below the lowest feet level and the
        # two body cells at the highest feet level.  Abilities that rise above
        # that volume must request their own additional top clearance.
        self._height = (bounds.max_feet_y - bounds.min_feet_y + 3
                        + bounds.extra_top_clearance_cells
                        + self._owner_reach)
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
                self._bounds.min_feet_y - 1 - self._owner_reach + layer,
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
    transition: MovementTransition | None = None

    def __post_init__(self) -> None:
        _node_id(self.start, "walk edge start");_node_id(self.end, "walk edge end")
        if (type(self.cost_seconds) not in (int, float)
                or not math.isfinite(float(self.cost_seconds)) or self.cost_seconds <= 0):
            raise ContractViolation("walk edge cost must be positive and finite")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("walk edge dependencies must be immutable")
        if self.transition is not None and type(self.transition) is not MovementTransition:
            raise ContractViolation("walk edge transition must be typed")

    @property
    def resource_change(self) -> ResourceChange:
        return self.transition.resource_change if self.transition is not None else ResourceChange()


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
    initial_resources: ResourceState = ResourceState()
    minimum_resources: ResourceState = ResourceState()
    goal_state: GoalState | None = None

    def __post_init__(self) -> None:
        require_nonnegative_int(self.sequence, "planning request sequence")
        require_identifier(self.request_id, "planning request id")
        require_identifier(self.goal_id, "planning goal id")
        require_nonnegative_int(self.goal_revision, "planning goal revision")
        require_identifier(self.world_session, "planning world session")
        _node_id(self.start, "planning start");_node_id(self.goal, "planning goal")
        if type(self.maximum_expansions) is not int or not 1<=self.maximum_expansions<=1_000_000:
            raise ContractViolation("planning expansion budget must be within 1..1000000")
        if (type(self.initial_resources) is not ResourceState
                or type(self.minimum_resources) is not ResourceState):
            raise ContractViolation("planning resources must use resource states")
        if not self.initial_resources.at_least(self.minimum_resources):
            raise ContractViolation("planning initial resources are below the required minimum")
        if self.goal_state is not None:
            if type(self.goal_state) is not GoalState:
                raise ContractViolation("planning goal state must be typed")
            if not self.minimum_resources.at_least(self.goal_state.minimum_resources):
                raise ContractViolation("planning resource minimum omits the goal requirement")
            position = (self.goal[0] + .5, float(self.goal[1]), self.goal[2] + .5)
            region = self.goal_state.region
            spatially_compatible = (
                region.min_x <= position[0] <= region.max_x
                and region.min_y <= position[1] <= region.max_y
                and region.min_z <= position[2] <= region.max_z
                and self.goal_state.support in {GoalSupport.SOLID, GoalSupport.ANY}
                and MovementMode.WALK in self.goal_state.allowed_modes
                and "standing" in self.goal_state.allowed_poses
            )
            if not spatially_compatible:
                raise ContractViolation("planning node does not satisfy the declared goal state")


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
    final_resources: ResourceState | None = None
    goal_state: GoalState | None = None


@dataclass(frozen=True, slots=True)
class _SearchResult:
    path: tuple[WalkNodeId, ...]
    segments: tuple[WalkEdge | JumpUpEdge, ...]
    cost_seconds: float | None
    final_resources: ResourceState | None
    expanded: int
    timed_out: bool = False


def _resource_aware_search(
    start: WalkNodeId,
    goal: WalkNodeId,
    initial_resources: ResourceState,
    minimum_resources: ResourceState,
    maximum_expansions: int,
    heuristic,
    outgoing,
) -> _SearchResult:
    """A* over nondominated (node, remaining-resources) labels."""
    serial = 0
    frontier = [(heuristic(start), serial, 0.0, start, initial_resources)]
    labels: dict[WalkNodeId, list[tuple[float, ResourceState]]] = {
        start: [(0.0, initial_resources)],
    }
    previous: dict[
        tuple[WalkNodeId, ResourceState],
        tuple[tuple[WalkNodeId, ResourceState], WalkEdge | JumpUpEdge],
    ] = {}
    expanded = 0
    while frontier:
        _, _, cost, current, resources = heapq.heappop(frontier)
        if not any(abs(cost - known_cost) <= 1.0e-12 and resources == known_resources
                   for known_cost, known_resources in labels.get(current, ())):
            continue
        if current == goal:
            key = (current, resources)
            path = [current]
            segments: list[WalkEdge | JumpUpEdge] = []
            while path[-1] != start:
                prior, segment = previous[key]
                segments.append(segment)
                path.append(prior[0])
                key = prior
            path.reverse()
            segments.reverse()
            return _SearchResult(
                tuple(path), tuple(segments), cost, resources, expanded,
            )
        expanded += 1
        if expanded > maximum_expansions:
            return _SearchResult((), (), None, None, expanded, True)
        for segment in outgoing(current):
            updated = resources.apply(
                segment.resource_change, minimum_resources, initial_resources,
            )
            if updated is None:
                continue
            candidate = cost + segment.cost_seconds
            existing = labels.setdefault(segment.end, [])
            if any(
                known_cost <= candidate + 1.0e-12 and known_resources.dominates(updated)
                for known_cost, known_resources in existing
            ):
                continue
            existing[:] = [
                (known_cost, known_resources)
                for known_cost, known_resources in existing
                if not (candidate <= known_cost + 1.0e-12
                        and updated.dominates(known_resources))
            ]
            existing.append((candidate, updated))
            previous[(segment.end, updated)] = ((current, resources), segment)
            serial += 1
            heapq.heappush(
                frontier,
                (candidate + heuristic(segment.end), serial, candidate, segment.end, updated),
            )
    return _SearchResult((), (), None, None, expanded)


def _body_at(node_id: WalkNodeId) -> Aabb:
    x,y,z=node_id
    return Aabb(x+.2,float(y),z+.2,x+.8,float(y)+1.8,z+.8)


def _walk_transition(
    profile: GroundMotionProfile,
    cost_seconds: float,
    dependencies: tuple[BlockPos, ...],
) -> MovementTransition:
    state = MovementStateClass(
        MovementMode.WALK, "standing", 0.0,
        profile.maximum_speed_blocks_per_second,
    )
    return MovementTransition(
        f"{profile.profile_id}/walk", profile.environment_id,
        MovementMode.WALK, state, (state,), cost_seconds, dependencies,
        ResourceChange(), CancellationMode.GROUND_STOP,
        CancellationMode.GROUND_STOP,
        trajectory_profile_id=profile.profile_id,
        risk_tags=frozenset(),
    )


def _jump_transition(
    profile: JumpUpProfile,
    dependencies: tuple[BlockPos, ...],
) -> MovementTransition:
    entry = MovementStateClass(
        MovementMode.JUMP_UP, "standing", 0.0,
        profile.maximum_entry_speed_blocks_per_second,
    )
    exit_state = MovementStateClass(
        MovementMode.WALK, "standing", 0.0,
        profile.maximum_exit_speed_blocks_per_second,
    )
    return MovementTransition(
        f"{profile.profile_id}/jump-up", profile.environment_id,
        MovementMode.JUMP_UP, entry, (exit_state,), profile.cost_seconds,
        dependencies, ResourceChange(), CancellationMode.SAFE_LANDING,
        CancellationMode.SAFE_LANDING,
        trajectory_profile_id=profile.profile_id,
        risk_tags=frozenset(),
    )


def _walk_state(world: WorldView, node_id: WalkNodeId,
                profile: GroundMotionProfile) -> tuple[QueryStatus,tuple[BlockPos,...]]:
    body=_body_at(node_id)
    clearance=sweep(body,(0.0,0.0,0.0),world)
    support=query_support(body,world)
    dependencies=tuple(sorted(set(clearance.dependencies+support.dependencies)))
    if clearance.status is QueryStatus.BLOCKED or support.status is QueryStatus.BLOCKED:
        return QueryStatus.BLOCKED,dependencies
    if (profile.motion_catalog is not None and profile.ground_model_id is not None
            and unsupported_motion_cells(
                profile.motion_catalog, world, dependencies, profile.ground_model_id,
            )):
        return QueryStatus.UNSUPPORTED,dependencies
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
    if (profile.motion_catalog is not None and profile.ground_model_id is not None
            and unsupported_motion_cells(
                profile.motion_catalog, world, movement.dependencies,
                profile.ground_model_id,
            )):
        return QueryStatus.UNSUPPORTED,tuple(sorted(dependencies))
    unsupported=False
    missing=False
    for fraction in (.25,.5,.75,1.0):
        support=query_support(body.moved(dx*fraction,0.0,dz*fraction),world)
        dependencies.update(support.dependencies)
        if support.status is QueryStatus.BLOCKED:
            return QueryStatus.BLOCKED,tuple(sorted(dependencies))
        if (profile.motion_catalog is not None and profile.ground_model_id is not None
                and unsupported_motion_cells(
                    profile.motion_catalog, world, support.dependencies,
                    profile.ground_model_id,
                )):
            unsupported=True
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
                cost = 1.0 / profile.maximum_speed_blocks_per_second
                edges.append(WalkEdge(
                    start, end, cost, dependencies,
                    _walk_transition(profile, cost, dependencies),
                ))
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
                        _jump_transition(jump_profile, result.dependencies),
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
               expanded: int = 0,
               final_resources: ResourceState | None = None) -> RouteCandidate:
        path = tuple(nodes[node_id] for node_id in path_ids)
        dependencies = tuple(sorted(
            {cell for node in path for cell in node.dependencies}
            | {cell for edge in segments for cell in edge.dependencies}
        ))
        return RouteCandidate(
            request.sequence, request.request_id, request.goal_id,
            request.goal_revision, request.world_session, world.geometry_revision,
            request.start, request.goal, status, path, segments, cost,
            dependencies, expanded, final_resources, request.goal_state,
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
                cost = 1.0 / profile.maximum_speed_blocks_per_second
                edges[key] = (WalkEdge(
                    start, end, cost, dependencies,
                    _walk_transition(profile, cost, dependencies),
                ) if status is QueryStatus.FEASIBLE else None)
            elif jump_profile is not None and end[1] == start[1] + 1:
                jump = query_jump_up(world, start, end, jump_profile)
                saw_unsupported = saw_unsupported or jump.status is QueryStatus.UNSUPPORTED
                edges[key] = (JumpUpEdge(
                    start, end, jump_profile.profile_id,
                    (end[0] - start[0], end[2] - start[2]),
                    jump_profile.cost_seconds, jump.dependencies,
                    _jump_transition(jump_profile, jump.dependencies),
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
    def outgoing(current: WalkNodeId) -> tuple[WalkEdge | JumpUpEdge, ...]:
        found = []
        neighbors=[(current[0]+dx,current[1],current[2]+dz) for dx,dz in _DIRECTIONS]
        if jump_profile is not None and current[1] < bounds.max_feet_y:
            neighbors += [(current[0]+dx,current[1]+1,current[2]+dz) for dx,dz in _DIRECTIONS]
        for other in neighbors:
            segment = edge(current, other)
            if segment is not None:
                found.append(segment)
        return tuple(found)

    search = _resource_aware_search(
        request.start, request.goal,
        request.initial_resources, request.minimum_resources,
        request.maximum_expansions, heuristic, outgoing,
    )
    if search.timed_out:
        return result(PlanningStatus.TIMEOUT, expanded=search.expanded)
    if search.path:
        return result(
            PlanningStatus.COMPLETE, search.path, search.segments,
            search.cost_seconds, search.expanded, search.final_resources,
        )
    status = (PlanningStatus.UNSUPPORTED if saw_unsupported else
              PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
              if bounds.complete_scope else PlanningStatus.NO_KNOWN_ROUTE)
    return result(status, expanded=search.expanded)


def _candidate(request: PlanningRequest, graph: WalkGraph, status: PlanningStatus,
               path_ids: tuple[WalkNodeId,...], segments: tuple[WalkEdge | JumpUpEdge,...],
               cost: float | None, expanded: int,
               final_resources: ResourceState | None = None) -> RouteCandidate:
    by_id={node.node_id:node for node in graph.nodes}
    path=tuple(by_id[node_id] for node_id in path_ids)
    dependencies=tuple(sorted({cell for node in path for cell in node.dependencies}
                              | {cell for edge in segments for cell in edge.dependencies}))
    return RouteCandidate(request.sequence,request.request_id,request.goal_id,
                          request.goal_revision,request.world_session,
                          graph.geometry_revision,request.start,request.goal,status,path,
                          segments,cost,dependencies,expanded,final_resources,
                          request.goal_state)


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
    search = _resource_aware_search(
        request.start, request.goal,
        request.initial_resources, request.minimum_resources,
        request.maximum_expansions, heuristic,
        lambda node: tuple(adjacency[node]),
    )
    if search.timed_out:
        return _candidate(
            request, graph, PlanningStatus.TIMEOUT, (), (), None, search.expanded,
        )
    if search.path:
        return _candidate(
            request, graph, PlanningStatus.COMPLETE,
            search.path, search.segments, search.cost_seconds, search.expanded,
            search.final_resources,
        )
    status=(PlanningStatus.UNSUPPORTED if graph.has_unsupported else
            PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE if graph.complete_scope
            else PlanningStatus.NO_KNOWN_ROUTE)
    return _candidate(request,graph,status,(),(),None,search.expanded)


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


# B07 keeps the verified integer Walk graph intact and adds a surface graph
# whose identity does not discard fractional feet height.
@dataclass(frozen=True, slots=True)
class SurfaceNode:
    surface: SupportSurface

    @property
    def node_id(self) -> SurfaceNodeId:
        return self.surface.node_id

    @property
    def position(self) -> tuple[float, float, float]:
        return self.surface.position

    @property
    def dependencies(self) -> tuple[BlockPos, ...]:
        return self.surface.dependencies


@dataclass(frozen=True, slots=True)
class SurfaceWalkEdge:
    start: SurfaceNodeId
    end: SurfaceNodeId
    cost_seconds: float
    dependencies: tuple[BlockPos, ...]
    transition: MovementTransition

    def __post_init__(self) -> None:
        if type(self.start) is not SurfaceNodeId or type(self.end) is not SurfaceNodeId:
            raise ContractViolation("surface walk edge requires surface node ids")
        if (type(self.cost_seconds) not in (int, float)
                or not math.isfinite(float(self.cost_seconds))
                or self.cost_seconds <= 0):
            raise ContractViolation("surface walk edge cost must be positive")
        if type(self.dependencies) is not tuple or type(self.transition) is not MovementTransition:
            raise ContractViolation("surface walk edge requires immutable typed facts")

    @property
    def resource_change(self) -> ResourceChange:
        return self.transition.resource_change


@dataclass(frozen=True, slots=True)
class SurfaceJumpUpEdge:
    start: SurfaceNodeId
    end: SurfaceNodeId
    jump_edge: JumpUpEdge

    def __post_init__(self) -> None:
        if type(self.start) is not SurfaceNodeId or type(self.end) is not SurfaceNodeId:
            raise ContractViolation("surface JumpUp edge requires surface node ids")
        if type(self.jump_edge) is not JumpUpEdge:
            raise ContractViolation("surface JumpUp edge requires a calibrated JumpUp edge")

    @property
    def cost_seconds(self) -> float:
        return self.jump_edge.cost_seconds

    @property
    def dependencies(self) -> tuple[BlockPos, ...]:
        return self.jump_edge.dependencies

    @property
    def transition(self) -> MovementTransition | None:
        return self.jump_edge.transition

    @property
    def resource_change(self) -> ResourceChange:
        return self.jump_edge.resource_change


@dataclass(frozen=True, slots=True)
class SurfaceJumpGapEdge:
    start: SurfaceNodeId
    end: SurfaceNodeId
    air_edge: JumpGapEdge

    def __post_init__(self) -> None:
        if type(self.start) is not SurfaceNodeId or type(self.end) is not SurfaceNodeId:
            raise ContractViolation("surface gap jump edge requires surface node ids")
        if type(self.air_edge) is not JumpGapEdge:
            raise ContractViolation("surface gap jump requires a calibrated edge")

    @property
    def cost_seconds(self) -> float:
        return self.air_edge.cost_seconds

    @property
    def dependencies(self) -> tuple[BlockPos, ...]:
        return self.air_edge.dependencies

    @property
    def transition(self) -> MovementTransition | None:
        return self.air_edge.transition

    @property
    def resource_change(self) -> ResourceChange:
        return self.air_edge.resource_change


@dataclass(frozen=True, slots=True)
class SurfaceControlledDropEdge:
    start: SurfaceNodeId
    end: SurfaceNodeId
    air_edge: ControlledDropEdge

    def __post_init__(self) -> None:
        if type(self.start) is not SurfaceNodeId or type(self.end) is not SurfaceNodeId:
            raise ContractViolation("surface controlled drop edge requires surface node ids")
        if type(self.air_edge) is not ControlledDropEdge:
            raise ContractViolation("surface controlled drop requires a calibrated edge")

    @property
    def cost_seconds(self) -> float:
        return self.air_edge.cost_seconds

    @property
    def dependencies(self) -> tuple[BlockPos, ...]:
        return self.air_edge.dependencies

    @property
    def transition(self) -> MovementTransition | None:
        return self.air_edge.transition

    @property
    def resource_change(self) -> ResourceChange:
        return self.air_edge.resource_change


SurfaceEdge = (
    SurfaceWalkEdge | StepEdge | SurfaceJumpUpEdge
    | SurfaceJumpGapEdge | SurfaceControlledDropEdge
)


@dataclass(frozen=True, slots=True)
class SurfaceGraph:
    world_session: str
    geometry_revision: int
    bounds: KnownMapBounds
    nodes: tuple[SurfaceNode, ...]
    edges: tuple[SurfaceEdge, ...]
    has_unsupported: bool

    def __post_init__(self) -> None:
        require_identifier(self.world_session, "surface graph world session")
        require_nonnegative_int(self.geometry_revision, "surface graph geometry revision")
        if type(self.bounds) is not KnownMapBounds:
            raise ContractViolation("surface graph requires known bounds")
        if type(self.nodes) is not tuple or any(type(node) is not SurfaceNode for node in self.nodes):
            raise ContractViolation("surface graph nodes must be immutable")
        if type(self.edges) is not tuple or any(type(edge) not in (
                SurfaceWalkEdge, StepEdge, SurfaceJumpUpEdge,
                SurfaceJumpGapEdge, SurfaceControlledDropEdge)
                                                 for edge in self.edges):
            raise ContractViolation("surface graph edges must be immutable")
        ids = tuple(node.node_id for node in self.nodes)
        if ids != tuple(sorted(set(ids))):
            raise ContractViolation("surface graph nodes must be sorted and unique")
        known = set(ids)
        if any(edge.start not in known or edge.end not in known for edge in self.edges):
            raise ContractViolation("surface edge references a missing node")
        edge_keys = tuple((edge.start, edge.end) for edge in self.edges)
        if edge_keys != tuple(sorted(set(edge_keys))):
            raise ContractViolation("surface graph edges must be sorted and unique")
        if type(self.has_unsupported) is not bool:
            raise ContractViolation("surface graph unsupported flag must be explicit")

    @property
    def complete_scope(self) -> bool:
        return self.bounds.complete_scope


class SurfacePlanningStatus(StrEnum):
    COMPLETE = "complete"
    NO_KNOWN_ROUTE = "no_known_route"
    NO_ROUTE_WITHIN_COMPLETE_SCOPE = "no_route_within_complete_scope"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class SurfacePlanningRequest:
    sequence: int
    request_id: str
    goal_id: str
    goal_revision: int
    world_session: str
    start: SurfaceNodeId
    goal: SurfaceNodeId
    maximum_expansions: int = 100_000

    def __post_init__(self) -> None:
        require_nonnegative_int(self.sequence, "surface planning request sequence")
        require_identifier(self.request_id, "surface planning request id")
        require_identifier(self.goal_id, "surface planning goal id")
        require_nonnegative_int(self.goal_revision, "surface planning goal revision")
        require_identifier(self.world_session, "surface planning world session")
        if type(self.start) is not SurfaceNodeId or type(self.goal) is not SurfaceNodeId:
            raise ContractViolation("surface planning requires surface node ids")
        if type(self.maximum_expansions) is not int or not 1 <= self.maximum_expansions <= 1_000_000:
            raise ContractViolation("surface planning expansion budget is invalid")


@dataclass(frozen=True, slots=True)
class SurfaceRouteCandidate:
    request_sequence: int
    request_id: str
    goal_id: str
    goal_revision: int
    world_session: str
    geometry_revision: int
    planning_start: SurfaceNodeId
    planning_goal: SurfaceNodeId
    status: SurfacePlanningStatus
    path: tuple[SurfaceNode, ...]
    segments: tuple[SurfaceEdge, ...]
    total_cost_seconds: float | None
    dependencies: tuple[BlockPos, ...]
    expanded_nodes: int


def _surface_walk_query(world: WorldView, start: SupportSurface,
                        end: SupportSurface) -> tuple[QueryStatus, tuple[BlockPos, ...]]:
    if abs(end.position[1] - start.position[1]) > 1.0e-6:
        return QueryStatus.UNSUPPORTED, tuple(sorted(
            set(start.dependencies) | set(end.dependencies)
        ))
    body = Aabb(start.position[0] - .3, start.position[1], start.position[2] - .3,
                start.position[0] + .3, start.position[1] + 1.8,
                start.position[2] + .3)
    dx = end.position[0] - start.position[0]
    dz = end.position[2] - start.position[2]
    movement = sweep(body, (dx, 0.0, dz), world)
    dependencies = set(start.dependencies) | set(end.dependencies) | set(movement.dependencies)
    if movement.status is not QueryStatus.FEASIBLE:
        return movement.status, tuple(sorted(dependencies))
    for fraction in (.25, .5, .75, 1.0):
        support = query_support(body.moved(dx * fraction, 0.0, dz * fraction), world)
        dependencies.update(support.dependencies)
        if support.status is not QueryStatus.FEASIBLE:
            return support.status, tuple(sorted(dependencies))
        if support.support_fraction < .5:
            return QueryStatus.BLOCKED, tuple(sorted(dependencies))
    return QueryStatus.FEASIBLE, tuple(sorted(dependencies))


def _step_transition(profile: StepProfile,
                     dependencies: tuple[BlockPos, ...]) -> MovementTransition:
    entry = MovementStateClass(
        MovementMode.WALK, "standing", 0.0,
        profile.maximum_entry_speed_blocks_per_second,
    )
    exit_state = MovementStateClass(
        MovementMode.WALK, "standing", 0.0,
        profile.maximum_exit_speed_blocks_per_second,
    )
    return MovementTransition(
        f"{profile.profile_id}/step", profile.environment_id, MovementMode.WALK,
        entry, (exit_state,), profile.cost_seconds, dependencies,
        ResourceChange(), CancellationMode.GROUND_STOP,
        CancellationMode.GROUND_STOP,
        trajectory_profile_id=profile.profile_id,
    )


def _air_transition(profile: AirMotionProfile,
                    dependencies: tuple[BlockPos, ...]) -> MovementTransition:
    entry = MovementStateClass(
        profile.mode, "standing",
        profile.minimum_entry_speed_blocks_per_second,
        profile.maximum_entry_speed_blocks_per_second,
    )
    exit_state = MovementStateClass(
        MovementMode.WALK, "standing", 0.0,
        profile.maximum_exit_speed_blocks_per_second,
    )
    return MovementTransition(
        f"{profile.profile_id}/{profile.mode.value}", profile.environment_id,
        profile.mode, entry, (exit_state,), profile.cost_seconds, dependencies,
        ResourceChange(), CancellationMode.SAFE_LANDING,
        CancellationMode.SAFE_LANDING,
        trajectory_profile_id=profile.profile_id,
        risk_tags=frozenset(),
    )


def build_surface_graph(world: WorldView, bounds: KnownMapBounds,
                        ground_profile: GroundMotionProfile,
                        step_profile: StepProfile,
                        jump_profile: JumpUpProfile | None = None,
                        *, air_profiles: tuple[AirMotionProfile, ...] = ()) -> SurfaceGraph:
    """Materialize the known standable surfaces and verified adjacent edges."""
    if (type(world) is not WorldView or type(bounds) is not KnownMapBounds
            or type(ground_profile) is not GroundMotionProfile
            or type(step_profile) is not StepProfile):
        raise ContractViolation("surface graph construction requires typed inputs")
    if jump_profile is not None and type(jump_profile) is not JumpUpProfile:
        raise ContractViolation("surface graph JumpUp profile must be typed")
    if (type(air_profiles) is not tuple
            or any(type(profile) is not AirMotionProfile for profile in air_profiles)
            or len({profile.mode for profile in air_profiles}) != len(air_profiles)):
        raise ContractViolation("surface graph air profiles must be typed and unique")
    air_by_mode = {profile.mode: profile for profile in air_profiles}
    nodes: list[SurfaceNode] = []
    complete = bounds.complete_scope
    has_unsupported = False
    for x in range(bounds.min_x, bounds.max_x + 1):
        for z in range(bounds.min_z, bounds.max_z + 1):
            result = query_support_surfaces(
                world, x, z, float(bounds.min_feet_y),
                float(bounds.max_feet_y + 1),
            )
            complete = complete and result.status is not QueryStatus.NEEDS_INFORMATION
            has_unsupported = has_unsupported or result.status is QueryStatus.UNSUPPORTED
            if result.status is not QueryStatus.FEASIBLE:
                continue
            for surface in result.surfaces:
                material_supported = (
                    not ground_profile.support_materials
                    or set(surface.materials).issubset(ground_profile.support_materials)
                )
                catalog_supported = not (
                    ground_profile.motion_catalog is not None
                    and ground_profile.ground_model_id is not None
                    and unsupported_motion_cells(
                        ground_profile.motion_catalog, world, surface.dependencies,
                        ground_profile.ground_model_id,
                    )
                )
                if material_supported and catalog_supported:
                    nodes.append(SurfaceNode(surface))
                else:
                    has_unsupported = True

    by_id = {node.node_id: node for node in nodes}
    by_column: dict[tuple[int, int], list[SurfaceNode]] = {}
    for node in nodes:
        by_column.setdefault(
            (node.node_id.column_x, node.node_id.column_z), []
        ).append(node)
    edges: list[SurfaceEdge] = []
    offsets = set(((0, 0),) + _DIRECTIONS)
    for profile in air_profiles:
        if profile.mode is MovementMode.JUMP_GAP:
            offsets.update((dx * profile.horizontal_cells, dz * profile.horizontal_cells)
                           for dx, dz in _DIRECTIONS)
    for start in sorted(nodes, key=lambda item: item.node_id):
        for dx, dz in sorted(offsets):
            for end in by_column.get((
                    start.node_id.column_x + dx,
                    start.node_id.column_z + dz,
            ), ()):
                if end.node_id == start.node_id:
                    continue
                delta_y = end.position[1] - start.position[1]
                distance = abs(dx) + abs(dz)
                if abs(delta_y) <= 1.0e-6 and distance == 1:
                    status, dependencies = _surface_walk_query(
                        world, start.surface, end.surface,
                    )
                    complete = complete and status is not QueryStatus.NEEDS_INFORMATION
                    has_unsupported = has_unsupported or status is QueryStatus.UNSUPPORTED
                    if status is QueryStatus.FEASIBLE:
                        cost = math.dist(start.position, end.position) / (
                            ground_profile.maximum_speed_blocks_per_second
                        )
                        edges.append(SurfaceWalkEdge(
                            start.node_id, end.node_id, cost, dependencies,
                            _walk_transition(ground_profile, cost, dependencies),
                        ))
                elif abs(delta_y) > 1.0e-6 and distance <= 1:
                    result = query_step(world, start.surface, end.surface, step_profile)
                    complete = complete and result.status is not QueryStatus.NEEDS_INFORMATION
                    has_unsupported = has_unsupported or result.status is QueryStatus.UNSUPPORTED
                    if result.status is QueryStatus.FEASIBLE:
                        edges.append(StepEdge(
                            start.node_id, end.node_id, step_profile.profile_id,
                            result.direction, step_profile.cost_seconds,
                            result.dependencies,
                            _step_transition(step_profile, result.dependencies),
                        ))
                    elif (jump_profile is not None
                          and result.status is QueryStatus.UNSUPPORTED
                          and abs(delta_y - 1.0) <= 1.0e-6
                          and abs(dx) + abs(dz) == 1
                          and abs(start.position[0] - (start.node_id.column_x + .5)) <= 1.0e-6
                          and abs(start.position[2] - (start.node_id.column_z + .5)) <= 1.0e-6
                          and abs(end.position[0] - (end.node_id.column_x + .5)) <= 1.0e-6
                          and abs(end.position[2] - (end.node_id.column_z + .5)) <= 1.0e-6
                          and abs(start.position[1] - round(start.position[1])) <= 1.0e-6
                          and abs(end.position[1] - round(end.position[1])) <= 1.0e-6):
                        start_id = (
                            start.node_id.column_x, round(start.position[1]),
                            start.node_id.column_z,
                        )
                        end_id = (
                            end.node_id.column_x, round(end.position[1]),
                            end.node_id.column_z,
                        )
                        jump = query_jump_up(world, start_id, end_id, jump_profile)
                        complete = complete and jump.status is not QueryStatus.NEEDS_INFORMATION
                        has_unsupported = has_unsupported or jump.status is QueryStatus.UNSUPPORTED
                        if jump.status is QueryStatus.FEASIBLE:
                            jump_edge = JumpUpEdge(
                                start_id, end_id, jump_profile.profile_id,
                                (dx, dz), jump_profile.cost_seconds,
                                jump.dependencies,
                                _jump_transition(jump_profile, jump.dependencies),
                            )
                            edges.append(SurfaceJumpUpEdge(
                                start.node_id, end.node_id, jump_edge,
                            ))
                    elif (result.status is QueryStatus.UNSUPPORTED
                          and delta_y < -1.0e-6
                          and MovementMode.CONTROLLED_DROP in air_by_mode):
                        profile = air_by_mode[MovementMode.CONTROLLED_DROP]
                        drop = query_controlled_drop(
                            world, start.surface, end.surface, profile,
                        )
                        complete = complete and drop.status is not QueryStatus.NEEDS_INFORMATION
                        has_unsupported = has_unsupported or drop.status is QueryStatus.UNSUPPORTED
                        if drop.status is QueryStatus.FEASIBLE:
                            transition = _air_transition(profile, drop.dependencies)
                            air_edge = ControlledDropEdge(
                                start.node_id, end.node_id, profile.profile_id,
                                profile.cost_seconds, drop.dependencies, transition,
                            )
                            edges.append(SurfaceControlledDropEdge(
                                start.node_id, end.node_id, air_edge,
                            ))
                elif (abs(delta_y) <= 1.0e-6
                      and MovementMode.JUMP_GAP in air_by_mode):
                    profile = air_by_mode[MovementMode.JUMP_GAP]
                    jump = query_jump_gap(world, start.surface, end.surface, profile)
                    complete = complete and jump.status is not QueryStatus.NEEDS_INFORMATION
                    has_unsupported = has_unsupported or jump.status is QueryStatus.UNSUPPORTED
                    if jump.status is QueryStatus.FEASIBLE:
                        transition = _air_transition(profile, jump.dependencies)
                        air_edge = JumpGapEdge(
                            start.node_id, end.node_id, profile.profile_id,
                            profile.cost_seconds, jump.dependencies, transition,
                        )
                        edges.append(SurfaceJumpGapEdge(
                            start.node_id, end.node_id, air_edge,
                        ))
    actual_bounds = KnownMapBounds(
        bounds.min_x, bounds.max_x, bounds.min_feet_y, bounds.max_feet_y,
        bounds.min_z, bounds.max_z, complete, bounds.extra_top_clearance_cells,
    )
    return SurfaceGraph(
        world.session.value, world.geometry_revision, actual_bounds,
        tuple(sorted(nodes, key=lambda item: item.node_id)),
        tuple(sorted(edges, key=lambda edge: (edge.start, edge.end))),
        has_unsupported,
    )


def _surface_candidate(request: SurfacePlanningRequest, graph: SurfaceGraph,
                       status: SurfacePlanningStatus,
                       path_ids: tuple[SurfaceNodeId, ...] = (),
                       segments: tuple[SurfaceEdge, ...] = (),
                       cost: float | None = None,
                       expanded: int = 0) -> SurfaceRouteCandidate:
    by_id = {node.node_id: node for node in graph.nodes}
    path = tuple(by_id[node_id] for node_id in path_ids)
    dependencies = tuple(sorted(
        {cell for node in path for cell in node.dependencies}
        | {cell for edge in segments for cell in edge.dependencies}
    ))
    return SurfaceRouteCandidate(
        request.sequence, request.request_id, request.goal_id,
        request.goal_revision, request.world_session, graph.geometry_revision,
        request.start, request.goal, status, path, segments, cost,
        dependencies, expanded,
    )


def astar_surface_plan(graph: SurfaceGraph,
                       request: SurfacePlanningRequest) -> SurfaceRouteCandidate:
    if type(graph) is not SurfaceGraph or type(request) is not SurfacePlanningRequest:
        raise ContractViolation("surface A* requires a surface graph and request")
    if graph.world_session != request.world_session:
        return _surface_candidate(request, graph, SurfacePlanningStatus.UNSUPPORTED)
    positions = {node.node_id: node.position for node in graph.nodes}
    if request.start not in positions or request.goal not in positions:
        status = (SurfacePlanningStatus.UNSUPPORTED if graph.has_unsupported else
                  SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
                  if graph.complete_scope else SurfacePlanningStatus.NO_KNOWN_ROUTE)
        return _surface_candidate(request, graph, status)
    adjacency: dict[SurfaceNodeId, list[SurfaceEdge]] = {
        node_id: [] for node_id in positions
    }
    for edge in graph.edges:
        adjacency[edge.start].append(edge)
    maximum_edge_speed = max((
        math.dist(positions[edge.start], positions[edge.end]) / edge.cost_seconds
        for edge in graph.edges
    ), default=1.0)
    queue = [(0.0, 0, 0.0, request.start)]
    costs = {request.start: 0.0}
    previous: dict[SurfaceNodeId, tuple[SurfaceNodeId, SurfaceEdge]] = {}
    serial = expanded = 0
    while queue:
        _, _, cost, current = heapq.heappop(queue)
        if cost != costs.get(current):
            continue
        if current == request.goal:
            path = [current]
            segments: list[SurfaceEdge] = []
            while path[-1] != request.start:
                prior, edge = previous[path[-1]]
                segments.append(edge)
                path.append(prior)
            path.reverse(); segments.reverse()
            return _surface_candidate(
                request, graph, SurfacePlanningStatus.COMPLETE,
                tuple(path), tuple(segments), cost, expanded,
            )
        expanded += 1
        if expanded > request.maximum_expansions:
            return _surface_candidate(
                request, graph, SurfacePlanningStatus.TIMEOUT,
                expanded=expanded,
            )
        for edge in adjacency[current]:
            candidate = cost + edge.cost_seconds
            if candidate + 1.0e-12 >= costs.get(edge.end, math.inf):
                continue
            costs[edge.end] = candidate
            previous[edge.end] = current, edge
            serial += 1
            heuristic = math.dist(
                positions[edge.end], positions[request.goal],
            ) / max(1.0e-12, maximum_edge_speed)
            heapq.heappush(queue, (candidate + heuristic, serial, candidate, edge.end))
    status = (SurfacePlanningStatus.UNSUPPORTED if graph.has_unsupported else
              SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
              if graph.complete_scope else SurfacePlanningStatus.NO_KNOWN_ROUTE)
    return _surface_candidate(request, graph, status, expanded=expanded)


def plan_known_surface_snapshot(
    snapshot: KnownMapSnapshot,
    ground_profile: GroundMotionProfile,
    step_profile: StepProfile,
    request: SurfacePlanningRequest,
    jump_profile: JumpUpProfile | None = None,
    *,
    air_profiles: tuple[AirMotionProfile, ...] = (),
) -> SurfaceRouteCandidate:
    """Build and search a surface graph from a detached snapshot."""
    if (type(snapshot) is not KnownMapSnapshot
            or type(ground_profile) is not GroundMotionProfile
            or type(step_profile) is not StepProfile
            or type(request) is not SurfacePlanningRequest):
        raise ContractViolation(
            "surface snapshot planning requires snapshot, profiles and request"
        )
    if jump_profile is not None and type(jump_profile) is not JumpUpProfile:
        raise ContractViolation("surface snapshot planning requires a JumpUp profile or None")
    if (type(air_profiles) is not tuple
            or any(type(profile) is not AirMotionProfile for profile in air_profiles)):
        raise ContractViolation("surface snapshot planning requires typed air profiles")
    graph = build_surface_graph(
        snapshot.world, snapshot.bounds, ground_profile, step_profile, jump_profile,
        air_profiles=air_profiles,
    )
    return astar_surface_plan(graph, request)


def dijkstra_surface_reference(graph: SurfaceGraph, start: SurfaceNodeId,
                               goal: SurfaceNodeId) -> float | None:
    if type(graph) is not SurfaceGraph:
        raise ContractViolation("surface reference requires a surface graph")
    nodes = {node.node_id for node in graph.nodes}
    if start not in nodes or goal not in nodes:
        return None
    adjacency: dict[SurfaceNodeId, list[SurfaceEdge]] = {node: [] for node in nodes}
    for edge in graph.edges:
        adjacency[edge.start].append(edge)
    queue = [(0.0, start)]
    costs = {start: 0.0}
    while queue:
        cost, current = heapq.heappop(queue)
        if cost != costs.get(current):
            continue
        if current == goal:
            return cost
        for edge in adjacency[current]:
            candidate = cost + edge.cost_seconds
            if candidate < costs.get(edge.end, math.inf):
                costs[edge.end] = candidate
                heapq.heappush(queue, (candidate, edge.end))
    return None
