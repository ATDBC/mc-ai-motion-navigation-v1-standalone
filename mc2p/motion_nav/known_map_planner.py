"""Known ordinary-ground graph construction and deterministic B04 planning."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import heapq
import math
import time

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.motion_nav.block_motion_traits import unsupported_motion_cells
from mc2p.motion_nav.air_motion import AirMotionProfile
from mc2p.motion_nav.controlled_drop import ControlledDropEdge, query_controlled_drop
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.ground_modes import GroundModeProfile
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
    maximum_planning_seconds: float = .5

    def __post_init__(self) -> None:
        require_nonnegative_int(self.sequence, "planning request sequence")
        require_identifier(self.request_id, "planning request id")
        require_identifier(self.goal_id, "planning goal id")
        require_nonnegative_int(self.goal_revision, "planning goal revision")
        require_identifier(self.world_session, "planning world session")
        _node_id(self.start, "planning start");_node_id(self.goal, "planning goal")
        if type(self.maximum_expansions) is not int or not 1<=self.maximum_expansions<=1_000_000:
            raise ContractViolation("planning expansion budget must be within 1..1000000")
        if (type(self.maximum_planning_seconds) not in (int, float)
                or not math.isfinite(float(self.maximum_planning_seconds))
                or not 0 < self.maximum_planning_seconds <= 60):
            raise ContractViolation("planning wall-clock budget must be within (0, 60] seconds")
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
    maximum_planning_seconds: float = .5,
    *,
    goal_test=None,
    next_states=None,
) -> _SearchResult:
    """A* over nondominated (node, remaining-resources) labels."""
    deadline_ns = time.perf_counter_ns() + int(maximum_planning_seconds * 1e9)
    serial = 0
    frontier = [(round(heuristic(start), 12), -0.0, serial, 0.0,
                 start, initial_resources)]
    labels: dict[WalkNodeId, list[tuple[float, ResourceState]]] = {
        start: [(0.0, initial_resources)],
    }
    previous: dict[
        tuple[WalkNodeId, ResourceState],
        tuple[tuple[WalkNodeId, ResourceState], WalkEdge | JumpUpEdge],
    ] = {}
    expanded = 0
    while frontier:
        if time.perf_counter_ns() >= deadline_ns:
            return _SearchResult((), (), None, None, expanded, True)
        _, _, _, cost, current, resources = heapq.heappop(frontier)
        if not any(abs(cost - known_cost) <= 1.0e-12 and resources == known_resources
                   for known_cost, known_resources in labels.get(current, ())):
            continue
        if (goal_test(current) if goal_test is not None else current == goal):
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
            transition = getattr(segment, "transition", None)
            if (transition is not None
                    and not resources.at_least(transition.minimum_entry_resources)):
                continue
            updated = resources.apply(
                segment.resource_change, minimum_resources, initial_resources,
            )
            if updated is None:
                continue
            candidate = cost + segment.cost_seconds
            successors = (
                next_states(current, segment) if next_states is not None
                else (segment.end,)
            )
            for successor in successors:
                existing = labels.setdefault(successor, [])
                if any(
                    known_cost <= candidate + 1.0e-12
                    and known_resources.dominates(updated)
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
                previous[(successor, updated)] = ((current, resources), segment)
                serial += 1
                heapq.heappush(
                    frontier,
                    (round(candidate + heuristic(successor), 12), -candidate,
                     serial, candidate, successor, updated),
                )
    return _SearchResult((), (), None, None, expanded)


def _plain_search(
    start,
    goal,
    initial_resources: ResourceState,
    maximum_expansions: int,
    heuristic,
    outgoing,
    maximum_planning_seconds: float = .5,
    *,
    goal_test=None,
    next_states=None,
) -> _SearchResult:
    """A* for paths whose transitions neither require nor change resources."""
    deadline_ns = time.perf_counter_ns() + int(maximum_planning_seconds * 1e9)
    serial = 0
    frontier = [(round(heuristic(start), 12), -0.0, serial, 0.0, start)]
    costs = {start: 0.0}
    previous = {}
    expanded = 0
    while frontier:
        if time.perf_counter_ns() >= deadline_ns:
            return _SearchResult((), (), None, None, expanded, True)
        _, _, _, cost, current = heapq.heappop(frontier)
        if cost > costs.get(current, math.inf) + 1.0e-12:
            continue
        if (goal_test(current) if goal_test is not None else current == goal):
            path = [current]
            segments = []
            while path[-1] != start:
                prior, segment = previous[path[-1]]
                segments.append(segment)
                path.append(prior)
            path.reverse()
            segments.reverse()
            return _SearchResult(
                tuple(path), tuple(segments), cost, initial_resources, expanded,
            )
        expanded += 1
        if expanded > maximum_expansions:
            return _SearchResult((), (), None, None, expanded, True)
        for segment in outgoing(current):
            candidate = cost + segment.cost_seconds
            successors = (
                next_states(current, segment) if next_states is not None
                else (segment.end,)
            )
            for successor in successors:
                if candidate + 1.0e-12 >= costs.get(successor, math.inf):
                    continue
                costs[successor] = candidate
                previous[successor] = current, segment
                serial += 1
                heapq.heappush(
                    frontier,
                    (round(candidate + heuristic(successor), 12), -candidate,
                     serial, candidate, successor),
                )
    return _SearchResult((), (), None, None, expanded)


def _body_at(node_id: WalkNodeId) -> Aabb:
    x,y,z=node_id
    return Aabb(x+.2,float(y),z+.2,x+.8,float(y)+1.8,z+.8)


def _walk_transition(
    profile: GroundMotionProfile,
    cost_seconds: float,
    dependencies: tuple[BlockPos, ...],
    *,
    mode_profile: GroundModeProfile | None = None,
) -> MovementTransition:
    mode = mode_profile.mode if mode_profile is not None else MovementMode.WALK
    pose = (sorted(mode_profile.poses)[0]
            if mode_profile is not None else "standing")
    state = MovementStateClass(
        mode, pose, 0.0,
        profile.maximum_speed_blocks_per_second,
    )
    return MovementTransition(
        f"{profile.profile_id}/{mode.value}", profile.environment_id,
        mode, state, (state,), cost_seconds, dependencies,
        ResourceChange(), CancellationMode.GROUND_STOP,
        CancellationMode.GROUND_STOP,
        trajectory_profile_id=profile.profile_id,
        risk_tags=frozenset(),
        minimum_entry_resources=(
            ResourceState((("food_points", float(mode_profile.minimum_food_points)),))
            if mode_profile is not None and mode_profile.minimum_food_points > 0
            else ResourceState()
        ),
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
    """Materialize the B04 walk graph for regression and diagnostics only.

    Live planning uses :func:`plan_known_snapshot` and expands only reached
    nodes. New reference callers should import this function through
    :mod:`mc2p.motion_nav.planning_reference`.
    """
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
    horizontal_cost_lower_bound = min(
        step_cost,
        jump_profile.cost_seconds if jump_profile is not None else math.inf,
    )
    def heuristic(node_id: WalkNodeId) -> float:
        return (abs(node_id[0] - request.goal[0])
                + abs(node_id[2] - request.goal[2])) * horizontal_cost_lower_bound
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

    if not request.initial_resources.values and not request.minimum_resources.values:
        search = _plain_search(
            request.start, request.goal, request.initial_resources,
            request.maximum_expansions, heuristic, outgoing,
            request.maximum_planning_seconds,
        )
    else:
        search = _resource_aware_search(
            request.start, request.goal,
            request.initial_resources, request.minimum_resources,
            request.maximum_expansions, heuristic, outgoing,
            request.maximum_planning_seconds,
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
    """Search a materialized B04 graph; retained as a reference path."""
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
    """Independent zero-heuristic reference used by B04 acceptance only."""
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
class PlannerStateKey:
    """Finite search distinctions that must survive at one surface node."""

    node_id: SurfaceNodeId
    movement_mode: MovementMode | None
    pose: str | None
    heading: tuple[int, int] | None = None
    speed_interval: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if type(self.node_id) is not SurfaceNodeId:
            raise ContractViolation("planner state requires a surface node")
        if self.movement_mode is not None and type(self.movement_mode) is not MovementMode:
            raise ContractViolation("planner state movement mode must be typed")
        if self.pose is not None:
            require_identifier(self.pose, "planner state pose")
        if (self.heading is not None
                and (type(self.heading) is not tuple or len(self.heading) != 2
                     or any(value not in (-1, 0, 1) for value in self.heading)
                     or sum(abs(value) for value in self.heading) != 1)):
            raise ContractViolation("planner state heading must be cardinal")
        if (self.speed_interval is not None
                and (type(self.speed_interval) is not tuple
                     or len(self.speed_interval) != 2
                     or any(type(value) not in (int, float)
                            or not math.isfinite(float(value))
                            for value in self.speed_interval)
                     or not 0 <= self.speed_interval[0] <= self.speed_interval[1])):
            raise ContractViolation("planner state speed interval is invalid")


def _surface_edge_identity(edge: SurfaceEdge) -> tuple:
    transition = edge.transition
    transition_id = transition.transition_id if transition is not None else ""
    if type(edge) is SurfaceJumpUpEdge:
        profile_id = edge.jump_edge.profile_id
    elif type(edge) in (SurfaceJumpGapEdge, SurfaceControlledDropEdge):
        profile_id = edge.air_edge.profile_id
    elif type(edge) is StepEdge:
        profile_id = edge.profile_id
    else:
        profile_id = (
            transition.trajectory_profile_id
            if transition is not None else ""
        )
    return (
        edge.start, edge.end, type(edge).__name__,
        profile_id or "", transition_id,
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
        edge_keys = tuple(_surface_edge_identity(edge) for edge in self.edges)
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
    initial_resources: ResourceState = ResourceState()
    minimum_resources: ResourceState = ResourceState()
    goal_state: GoalState | None = None
    maximum_planning_seconds: float = .5

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
        if (type(self.maximum_planning_seconds) not in (int, float)
                or not math.isfinite(float(self.maximum_planning_seconds))
                or not 0 < self.maximum_planning_seconds <= 60):
            raise ContractViolation(
                "surface planning wall-clock budget must be within (0, 60] seconds"
            )
        if (type(self.initial_resources) is not ResourceState
                or type(self.minimum_resources) is not ResourceState):
            raise ContractViolation("surface planning resources must use resource states")
        if not self.initial_resources.at_least(self.minimum_resources):
            raise ContractViolation("surface planning initial resources are below the minimum")
        if self.goal_state is not None:
            if type(self.goal_state) is not GoalState:
                raise ContractViolation("surface planning goal state must be typed")
            if not self.minimum_resources.at_least(self.goal_state.minimum_resources):
                raise ContractViolation("surface planning minimum omits the goal requirement")


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
    final_resources: ResourceState | None = None
    goal_state: GoalState | None = None
    initial_resources: ResourceState = ResourceState()
    minimum_resources: ResourceState = ResourceState()
    planner_states: tuple[PlannerStateKey, ...] = ()


def _surface_walk_query(
    world: WorldView,
    start: SupportSurface,
    end: SupportSurface,
    *,
    body_height_blocks: float = 1.8,
) -> tuple[QueryStatus, tuple[BlockPos, ...]]:
    if abs(end.position[1] - start.position[1]) > 1.0e-6:
        return QueryStatus.UNSUPPORTED, tuple(sorted(
            set(start.dependencies) | set(end.dependencies)
        ))
    body = Aabb(start.position[0] - .3, start.position[1], start.position[2] - .3,
                start.position[0] + .3, start.position[1] + body_height_blocks,
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
        minimum_entry_resources=(
            ResourceState((("food_points", float(profile.minimum_food_points)),))
            if profile.sprint_input else ResourceState()
        ),
    )


class _SurfaceExpander:
    """Query surface columns and outgoing edges only when search reaches them."""

    def __init__(
        self,
        world: WorldView,
        bounds: KnownMapBounds,
        ground_profile: GroundMotionProfile,
        step_profile: StepProfile,
        jump_profile: JumpUpProfile | None = None,
        *,
        air_profiles: tuple[AirMotionProfile, ...] = (),
        ground_mode_profile: GroundModeProfile | None = None,
    ) -> None:
        if (type(world) is not WorldView or type(bounds) is not KnownMapBounds
                or type(ground_profile) is not GroundMotionProfile
                or type(step_profile) is not StepProfile):
            raise ContractViolation("surface expansion requires typed inputs")
        if jump_profile is not None and type(jump_profile) is not JumpUpProfile:
            raise ContractViolation("surface expansion JumpUp profile must be typed")
        if ground_mode_profile is not None:
            if type(ground_mode_profile) is not GroundModeProfile:
                raise ContractViolation("surface expansion ground mode profile must be typed")
            if (ground_mode_profile.motion.profile_id != ground_profile.profile_id
                    or ground_mode_profile.motion.environment_id
                    != ground_profile.environment_id):
                raise ContractViolation("surface expansion ground mode and motion profile differ")
        if (type(air_profiles) is not tuple
                or any(type(profile) is not AirMotionProfile for profile in air_profiles)
                or len({profile.mode for profile in air_profiles}) != len(air_profiles)):
            raise ContractViolation("surface expansion air profiles must be typed and unique")
        self.world = world
        self.bounds = bounds
        self.ground_profile = ground_profile
        self.step_profile = step_profile
        self.jump_profile = jump_profile
        self.air_profiles = air_profiles
        self.air_by_mode = {profile.mode: profile for profile in air_profiles}
        self.ground_mode_profile = ground_mode_profile
        self.movement_mode = (
            ground_mode_profile.mode
            if ground_mode_profile is not None else MovementMode.WALK
        )
        pose = (sorted(ground_mode_profile.poses)[0]
                if ground_mode_profile is not None else "standing")
        self.body_height = {
            "standing": 1.8,
            "crouching": 1.5,
            "swimming": .6,
        }.get(pose)
        if self.body_height is None:
            raise ContractViolation("surface expansion ground pose has no body height")
        offsets = set(((0, 0),) + _DIRECTIONS)
        for profile in air_profiles:
            if profile.mode is MovementMode.JUMP_GAP:
                offsets.update(
                    (dx * profile.horizontal_cells, dz * profile.horizontal_cells)
                    for dx, dz in _DIRECTIONS
                )
        self.offsets = tuple(sorted(offsets))
        self.columns: dict[tuple[int, int], tuple[SurfaceNode, ...]] = {}
        self.nodes: dict[SurfaceNodeId, SurfaceNode] = {}
        self.edge_cache: dict[
            tuple[SurfaceNodeId, SurfaceNodeId], SurfaceEdge | None
        ] = {}
        self.outgoing_cache: dict[SurfaceNodeId, tuple[SurfaceEdge, ...]] = {}
        self.complete = bounds.complete_scope
        self.has_unsupported = False

    def _inside_column(self, x: int, z: int) -> bool:
        return (self.bounds.min_x <= x <= self.bounds.max_x
                and self.bounds.min_z <= z <= self.bounds.max_z)

    def column(self, x: int, z: int) -> tuple[SurfaceNode, ...]:
        key = x, z
        if not self._inside_column(x, z):
            return ()
        if key in self.columns:
            return self.columns[key]
        result = query_support_surfaces(
            self.world, x, z, float(self.bounds.min_feet_y),
            float(self.bounds.max_feet_y + 1), body_height=self.body_height,
        )
        self.complete = self.complete and result.status is not QueryStatus.NEEDS_INFORMATION
        self.has_unsupported = (
            self.has_unsupported or result.status is QueryStatus.UNSUPPORTED
        )
        found: list[SurfaceNode] = []
        if result.status is QueryStatus.FEASIBLE:
            for surface in result.surfaces:
                material_supported = (
                    not self.ground_profile.support_materials
                    or set(surface.materials).issubset(
                        self.ground_profile.support_materials
                    )
                )
                catalog_supported = not (
                    self.ground_profile.motion_catalog is not None
                    and self.ground_profile.ground_model_id is not None
                    and unsupported_motion_cells(
                        self.ground_profile.motion_catalog,
                        self.world,
                        surface.dependencies,
                        self.ground_profile.ground_model_id,
                    )
                )
                if material_supported and catalog_supported:
                    node = SurfaceNode(surface)
                    found.append(node)
                    self.nodes[node.node_id] = node
                else:
                    self.has_unsupported = True
        value = tuple(sorted(found, key=lambda item: item.node_id))
        self.columns[key] = value
        return value

    def node(self, node_id: SurfaceNodeId) -> SurfaceNode | None:
        if node_id not in self.nodes:
            self.column(node_id.column_x, node_id.column_z)
        return self.nodes.get(node_id)

    def _remember_status(self, status: QueryStatus) -> None:
        self.complete = self.complete and status is not QueryStatus.NEEDS_INFORMATION
        self.has_unsupported = self.has_unsupported or status is QueryStatus.UNSUPPORTED

    def _build_edge(self, start: SurfaceNode, end: SurfaceNode) -> SurfaceEdge | None:
        key = start.node_id, end.node_id
        if key in self.edge_cache:
            return self.edge_cache[key]
        edge: SurfaceEdge | None = None
        dx = end.node_id.column_x - start.node_id.column_x
        dz = end.node_id.column_z - start.node_id.column_z
        delta_y = end.position[1] - start.position[1]
        distance = abs(dx) + abs(dz)
        if abs(delta_y) <= 1.0e-6 and distance == 1:
            status, dependencies = _surface_walk_query(
                self.world, start.surface, end.surface,
                body_height_blocks=self.body_height,
            )
            self._remember_status(status)
            if status is QueryStatus.FEASIBLE:
                cost = math.dist(start.position, end.position) / (
                    self.ground_profile.maximum_speed_blocks_per_second
                )
                edge = SurfaceWalkEdge(
                    start.node_id, end.node_id, cost, dependencies,
                    _walk_transition(
                        self.ground_profile, cost, dependencies,
                        mode_profile=self.ground_mode_profile,
                    ),
                )
        elif (self.movement_mode is MovementMode.WALK
              and abs(delta_y) > 1.0e-6 and distance <= 1):
            result = query_step(
                self.world, start.surface, end.surface, self.step_profile,
            )
            self._remember_status(result.status)
            if result.status is QueryStatus.FEASIBLE:
                edge = StepEdge(
                    start.node_id, end.node_id, self.step_profile.profile_id,
                    result.direction, self.step_profile.cost_seconds,
                    result.dependencies,
                    _step_transition(self.step_profile, result.dependencies),
                )
            elif (self.jump_profile is not None
                  and result.status is QueryStatus.UNSUPPORTED
                  and abs(delta_y - 1.0) <= 1.0e-6
                  and distance == 1
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
                jump = query_jump_up(
                    self.world, start_id, end_id, self.jump_profile,
                )
                self._remember_status(jump.status)
                if jump.status is QueryStatus.FEASIBLE:
                    jump_edge = JumpUpEdge(
                        start_id, end_id, self.jump_profile.profile_id,
                        (dx, dz), self.jump_profile.cost_seconds,
                        jump.dependencies,
                        _jump_transition(self.jump_profile, jump.dependencies),
                    )
                    edge = SurfaceJumpUpEdge(
                        start.node_id, end.node_id, jump_edge,
                    )
            elif (result.status is QueryStatus.UNSUPPORTED
                  and delta_y < -1.0e-6
                  and MovementMode.CONTROLLED_DROP in self.air_by_mode):
                profile = self.air_by_mode[MovementMode.CONTROLLED_DROP]
                drop = query_controlled_drop(
                    self.world, start.surface, end.surface, profile,
                )
                self._remember_status(drop.status)
                if drop.status is QueryStatus.FEASIBLE:
                    transition = _air_transition(profile, drop.dependencies)
                    air_edge = ControlledDropEdge(
                        start.node_id, end.node_id, profile.profile_id,
                        profile.cost_seconds, drop.dependencies, transition,
                    )
                    edge = SurfaceControlledDropEdge(
                        start.node_id, end.node_id, air_edge,
                    )
        elif (abs(delta_y) <= 1.0e-6
              and MovementMode.JUMP_GAP in self.air_by_mode):
            profile = self.air_by_mode[MovementMode.JUMP_GAP]
            jump = query_jump_gap(
                self.world, start.surface, end.surface, profile,
            )
            self._remember_status(jump.status)
            if jump.status is QueryStatus.FEASIBLE:
                transition = _air_transition(profile, jump.dependencies)
                air_edge = JumpGapEdge(
                    start.node_id, end.node_id, profile.profile_id,
                    profile.cost_seconds, jump.dependencies, transition,
                )
                edge = SurfaceJumpGapEdge(
                    start.node_id, end.node_id, air_edge,
                )
        self.edge_cache[key] = edge
        return edge

    def outgoing(self, node_id: SurfaceNodeId) -> tuple[SurfaceEdge, ...]:
        if node_id in self.outgoing_cache:
            return self.outgoing_cache[node_id]
        start = self.node(node_id)
        if start is None:
            self.outgoing_cache[node_id] = ()
            return ()
        found: list[SurfaceEdge] = []
        for dx, dz in self.offsets:
            for end in self.column(
                node_id.column_x + dx, node_id.column_z + dz,
            ):
                if end.node_id == node_id:
                    continue
                edge = self._build_edge(start, end)
                if edge is not None:
                    found.append(edge)
        value = tuple(sorted(found, key=lambda item: (item.end, type(item).__name__)))
        self.outgoing_cache[node_id] = value
        return value

    def actual_bounds(self) -> KnownMapBounds:
        return KnownMapBounds(
            self.bounds.min_x, self.bounds.max_x,
            self.bounds.min_feet_y, self.bounds.max_feet_y,
            self.bounds.min_z, self.bounds.max_z, self.complete,
            self.bounds.extra_top_clearance_cells,
        )

    def horizontal_cost_lower_bound(self) -> float:
        candidates = [1.0 / self.ground_profile.maximum_speed_blocks_per_second]
        candidates.append(self.step_profile.cost_seconds)
        if self.jump_profile is not None:
            candidates.append(self.jump_profile.cost_seconds)
        for profile in self.air_profiles:
            horizontal = max(1, profile.horizontal_cells)
            candidates.append(profile.cost_seconds / horizontal)
        return min(candidates)

    def resource_neutral(self, request: SurfacePlanningRequest) -> bool:
        return (
            not request.initial_resources.values
            and not request.minimum_resources.values
            and (self.ground_mode_profile is None
                 or self.ground_mode_profile.minimum_food_points <= 0)
            and all(not profile.sprint_input for profile in self.air_profiles)
        )


def build_surface_graph(world: WorldView, bounds: KnownMapBounds,
                        ground_profile: GroundMotionProfile,
                        step_profile: StepProfile,
                        jump_profile: JumpUpProfile | None = None,
                        *, air_profiles: tuple[AirMotionProfile, ...] = (),
                        ground_mode_profile: GroundModeProfile | None = None,
                        ) -> SurfaceGraph:
    """Materialize known surfaces for reference and diagnostics only.

    Live planning uses :func:`plan_known_surface_snapshot`. New reference
    callers should import this function through
    :mod:`mc2p.motion_nav.planning_reference`.
    """
    expander = _SurfaceExpander(
        world, bounds, ground_profile, step_profile, jump_profile,
        air_profiles=air_profiles, ground_mode_profile=ground_mode_profile,
    )
    for x in range(bounds.min_x, bounds.max_x + 1):
        for z in range(bounds.min_z, bounds.max_z + 1):
            expander.column(x, z)
    for node_id in tuple(sorted(expander.nodes)):
        expander.outgoing(node_id)
    edges = tuple(sorted(
        (edge for edge in expander.edge_cache.values() if edge is not None),
        key=_surface_edge_identity,
    ))
    return SurfaceGraph(
        world.session.value, world.geometry_revision, expander.actual_bounds(),
        tuple(expander.nodes[node_id] for node_id in sorted(expander.nodes)),
        edges, expander.has_unsupported,
    )

def _surface_candidate(request: SurfacePlanningRequest, graph: SurfaceGraph,
                       status: SurfacePlanningStatus,
                       path_ids: tuple[SurfaceNodeId, ...] = (),
                       segments: tuple[SurfaceEdge, ...] = (),
                       cost: float | None = None,
                       expanded: int = 0,
                       final_resources: ResourceState | None = None,
                       planner_states: tuple[PlannerStateKey, ...] = (),
                       ) -> SurfaceRouteCandidate:
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
        dependencies, expanded, final_resources, request.goal_state,
        request.initial_resources, request.minimum_resources,
        planner_states,
    )


def _surface_successor_states(
        current: PlannerStateKey, edge: SurfaceEdge,
) -> tuple[PlannerStateKey, ...]:
    dx = edge.end.column_x - edge.start.column_x
    dz = edge.end.column_z - edge.start.column_z
    heading = None
    if dx or dz:
        heading = (
            0 if dx == 0 else (1 if dx > 0 else -1),
            0 if dz == 0 else (1 if dz > 0 else -1),
        )
    transition = edge.transition
    if transition is None:
        return (PlannerStateKey(
            edge.end, current.movement_mode, current.pose, heading,
            current.speed_interval,
        ),)
    return tuple(
        PlannerStateKey(
            edge.end, state.mode, state.pose, heading,
            (state.minimum_speed_blocks_per_second,
             state.maximum_speed_blocks_per_second),
        )
        for state in transition.exits
    )


def astar_surface_plan(graph: SurfaceGraph,
                       request: SurfacePlanningRequest) -> SurfaceRouteCandidate:
    """Search a materialized surface graph; retained as a reference path."""
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
    if request.goal_state is not None:
        goal_position = positions[request.goal]
        region = request.goal_state.region
        if not (
            region.min_x <= goal_position[0] <= region.max_x
            and region.min_y <= goal_position[1] <= region.max_y
            and region.min_z <= goal_position[2] <= region.max_z
            and request.goal_state.support in {GoalSupport.SOLID, GoalSupport.ANY}
        ):
            return _surface_candidate(
                request, graph, SurfacePlanningStatus.UNSUPPORTED,
            )
    adjacency: dict[SurfaceNodeId, list[SurfaceEdge]] = {
        node_id: [] for node_id in positions
    }
    for edge in graph.edges:
        adjacency[edge.start].append(edge)
    maximum_edge_speed = max((
        math.dist(positions[edge.start], positions[edge.end]) / edge.cost_seconds
        for edge in graph.edges
    ), default=1.0)
    start_state = PlannerStateKey(request.start, None, None)

    def heuristic(state: PlannerStateKey) -> float:
        return math.dist(
            positions[state.node_id], positions[request.goal],
        ) / max(1.0e-12, maximum_edge_speed)

    def goal_test(state: PlannerStateKey) -> bool:
        return state.node_id == request.goal

    def outgoing(state: PlannerStateKey) -> tuple[SurfaceEdge, ...]:
        return tuple(adjacency[state.node_id])

    resource_neutral = (
        not request.initial_resources.values
        and not request.minimum_resources.values
        and all(
            not edge.resource_change.deltas
            and (edge.transition is None
                 or not edge.transition.minimum_entry_resources.values)
            for edge in graph.edges
        )
    )
    if resource_neutral:
        search = _plain_search(
            start_state, request.goal, request.initial_resources,
            request.maximum_expansions, heuristic,
            outgoing,
            request.maximum_planning_seconds,
            goal_test=goal_test, next_states=_surface_successor_states,
        )
    else:
        search = _resource_aware_search(
            start_state, request.goal,
            request.initial_resources, request.minimum_resources,
            request.maximum_expansions, heuristic,
            outgoing,
            request.maximum_planning_seconds,
            goal_test=goal_test, next_states=_surface_successor_states,
        )
    if search.timed_out:
        return _surface_candidate(
            request, graph, SurfacePlanningStatus.TIMEOUT,
            expanded=search.expanded,
        )
    if search.path:
        if request.goal_state is not None and search.segments:
            terminal = search.segments[-1].transition
            if (terminal is None or not any(
                    state.mode in request.goal_state.allowed_modes
                    and state.pose in request.goal_state.allowed_poses
                    for state in terminal.exits)):
                return _surface_candidate(
                    request, graph, SurfacePlanningStatus.UNSUPPORTED,
                    expanded=search.expanded,
                )
        return _surface_candidate(
            request, graph, SurfacePlanningStatus.COMPLETE,
            tuple(state.node_id for state in search.path),
            search.segments, search.cost_seconds,
            search.expanded, search.final_resources,
            tuple(search.path),
        )
    status = (SurfacePlanningStatus.UNSUPPORTED if graph.has_unsupported else
              SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
              if graph.complete_scope else SurfacePlanningStatus.NO_KNOWN_ROUTE)
    return _surface_candidate(request, graph, status, expanded=search.expanded)


def plan_known_surface_snapshot(
    snapshot: KnownMapSnapshot,
    ground_profile: GroundMotionProfile,
    step_profile: StepProfile,
    request: SurfacePlanningRequest,
    jump_profile: JumpUpProfile | None = None,
    *,
    air_profiles: tuple[AirMotionProfile, ...] = (),
    ground_mode_profile: GroundModeProfile | None = None,
) -> SurfaceRouteCandidate:
    """Search a detached snapshot while expanding only reached surface columns."""
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
    expander = _SurfaceExpander(
        snapshot.world, snapshot.bounds, ground_profile, step_profile,
        jump_profile, air_profiles=air_profiles,
        ground_mode_profile=ground_mode_profile,
    )

    def discovered_graph() -> SurfaceGraph:
        edges = tuple(sorted(
            (edge for edge in expander.edge_cache.values() if edge is not None),
            key=_surface_edge_identity,
        ))
        return SurfaceGraph(
            snapshot.world.session.value, snapshot.world.geometry_revision,
            expander.actual_bounds(),
            tuple(expander.nodes[node_id] for node_id in sorted(expander.nodes)),
            edges, expander.has_unsupported,
        )

    if snapshot.world.session.value != request.world_session:
        return _surface_candidate(
            request, discovered_graph(), SurfacePlanningStatus.UNSUPPORTED,
        )
    start = expander.node(request.start)
    goal = expander.node(request.goal)
    if start is None or goal is None:
        graph = discovered_graph()
        status = (
            SurfacePlanningStatus.UNSUPPORTED if graph.has_unsupported else
            SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
            if graph.complete_scope else SurfacePlanningStatus.NO_KNOWN_ROUTE
        )
        return _surface_candidate(request, graph, status)
    if request.goal_state is not None:
        position = goal.position
        region = request.goal_state.region
        if not (
            region.min_x <= position[0] <= region.max_x
            and region.min_y <= position[1] <= region.max_y
            and region.min_z <= position[2] <= region.max_z
            and request.goal_state.support in {GoalSupport.SOLID, GoalSupport.ANY}
        ):
            return _surface_candidate(
                request, discovered_graph(), SurfacePlanningStatus.UNSUPPORTED,
            )

    unit_cost = expander.horizontal_cost_lower_bound()

    start_state = PlannerStateKey(request.start, None, None)

    def heuristic(state: PlannerStateKey) -> float:
        return (
            abs(state.node_id.column_x - request.goal.column_x)
            + abs(state.node_id.column_z - request.goal.column_z)
        ) * unit_cost

    def goal_test(state: PlannerStateKey) -> bool:
        return state.node_id == request.goal

    def outgoing(state: PlannerStateKey) -> tuple[SurfaceEdge, ...]:
        return expander.outgoing(state.node_id)

    if expander.resource_neutral(request):
        search = _plain_search(
            start_state, request.goal, request.initial_resources,
            request.maximum_expansions, heuristic, outgoing,
            request.maximum_planning_seconds,
            goal_test=goal_test, next_states=_surface_successor_states,
        )
    else:
        search = _resource_aware_search(
            start_state, request.goal,
            request.initial_resources, request.minimum_resources,
            request.maximum_expansions, heuristic, outgoing,
            request.maximum_planning_seconds,
            goal_test=goal_test, next_states=_surface_successor_states,
        )
    graph = discovered_graph()
    if search.timed_out:
        return _surface_candidate(
            request, graph, SurfacePlanningStatus.TIMEOUT,
            expanded=search.expanded,
        )
    if search.path:
        if request.goal_state is not None and search.segments:
            terminal = search.segments[-1].transition
            if (terminal is None or not any(
                    state.mode in request.goal_state.allowed_modes
                    and state.pose in request.goal_state.allowed_poses
                    for state in terminal.exits)):
                return _surface_candidate(
                    request, graph, SurfacePlanningStatus.UNSUPPORTED,
                    expanded=search.expanded,
                )
        return _surface_candidate(
            request, graph, SurfacePlanningStatus.COMPLETE,
            tuple(state.node_id for state in search.path),
            search.segments, search.cost_seconds,
            search.expanded, search.final_resources,
            tuple(search.path),
        )
    status = (
        SurfacePlanningStatus.UNSUPPORTED if graph.has_unsupported else
        SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE
        if graph.complete_scope else SurfacePlanningStatus.NO_KNOWN_ROUTE
    )
    return _surface_candidate(request, graph, status, expanded=search.expanded)


def dijkstra_surface_reference(graph: SurfaceGraph, start: SurfaceNodeId,
                               goal: SurfaceNodeId) -> float | None:
    """Independent small-graph reference; never a live planning hot path."""
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
