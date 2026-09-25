"""Bounded B11 planner for the next confirmed full-block bridge placement."""
from __future__ import annotations

from dataclasses import dataclass
import heapq

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.known_map_planner import KnownMapSnapshot, SurfacePlanningRequest
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.world_interaction import InteractionKind, RequiredInteraction
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge


_DIRECTIONS = ((-1, 0, "west"), (0, -1, "north"), (0, 1, "south"), (1, 0, "east"))


@dataclass(frozen=True, slots=True)
class BridgePlacementPolicy:
    expected_item_id: str = "minecraft:dirt"
    expected_block_id: str = "minecraft:dirt"
    maximum_blocks: int = 3
    maximum_search_nodes: int = 4096

    def __post_init__(self) -> None:
        require_identifier(self.expected_item_id, "bridge item id")
        require_identifier(self.expected_block_id, "bridge block id")
        if type(self.maximum_blocks) is not int or not 1 <= self.maximum_blocks <= 3:
            raise ContractViolation("bridge block limit must be within 1..3")
        if (type(self.maximum_search_nodes) is not int
                or not 1 <= self.maximum_search_nodes <= 65_536):
            raise ContractViolation("bridge search budget is invalid")


@dataclass(frozen=True, slots=True)
class BridgeInteractionPlan:
    requirement: RequiredInteraction
    work_node: SurfaceNodeId
    path: tuple[tuple[int, int, int], ...]
    required_placements: int
    expanded_nodes: int

    def __post_init__(self) -> None:
        if type(self.requirement) is not RequiredInteraction:
            raise ContractViolation("bridge plan requires an interaction")
        if type(self.work_node) is not SurfaceNodeId:
            raise ContractViolation("bridge plan requires a work surface")
        if type(self.path) is not tuple or len(self.path) < 2:
            raise ContractViolation("bridge plan requires a bounded path")
        if type(self.required_placements) is not int or self.required_placements < 1:
            raise ContractViolation("bridge plan requires at least one placement")
        if type(self.expanded_nodes) is not int or self.expanded_nodes < 1:
            raise ContractViolation("bridge plan expansion count is invalid")


def _cell_kind(snapshot: KnownMapSnapshot, x: int, feet_y: int, z: int) -> str:
    ground = snapshot.world.cell((x, feet_y - 1, z))
    lower = snapshot.world.cell((x, feet_y, z))
    upper = snapshot.world.cell((x, feet_y + 1, z))
    if (lower.knowledge is not CellKnowledge.AIR
            or upper.knowledge is not CellKnowledge.AIR):
        return "blocked"
    if (ground.knowledge is CellKnowledge.BLOCK
            and ground.block is not None
            and ground.block.collision_kind == "full_cube"):
        return "existing"
    if ground.knowledge is CellKnowledge.AIR:
        return "placeable"
    return "blocked"


def plan_next_bridge_interaction(
    snapshot: KnownMapSnapshot,
    request: SurfacePlanningRequest,
    policy: BridgePlacementPolicy,
) -> BridgeInteractionPlan | None:
    """Return one next placement; never inserts a hypothetical world fact."""
    if (type(snapshot) is not KnownMapSnapshot
            or type(request) is not SurfacePlanningRequest
            or type(policy) is not BridgePlacementPolicy):
        raise ContractViolation("bridge planning requires typed inputs")
    if snapshot.world.session.value != request.world_session:
        raise ContractViolation("bridge planning world session changed")
    if (request.start.vertical_band != request.goal.vertical_band
            or request.start.surface_index != 0
            or request.goal.surface_index != 0):
        return None
    feet_y = request.start.vertical_band
    start = (request.start.column_x, request.start.column_z)
    goal = (request.goal.column_x, request.goal.column_z)
    bounds = snapshot.bounds
    if _cell_kind(snapshot, start[0], feet_y, start[1]) != "existing":
        return None
    if _cell_kind(snapshot, goal[0], feet_y, goal[1]) != "existing":
        return None

    # Cost is lexicographic: avoid changing the world first, then minimize
    # path length.  The normal surface planner handles the zero-placement path;
    # this function only returns when at least one confirmed placement is needed.
    queue: list[tuple[int, int, int, int]] = [(0, 0, start[0], start[1])]
    best: dict[tuple[int, int], tuple[int, int]] = {start: (0, 0)}
    previous: dict[tuple[int, int], tuple[int, int]] = {}
    expanded = 0
    while queue and expanded < policy.maximum_search_nodes:
        placements, steps, x, z = heapq.heappop(queue)
        current = (x, z)
        if best.get(current) != (placements, steps):
            continue
        expanded += 1
        if current == goal:
            break
        for dx, dz, _ in _DIRECTIONS:
            other = (x + dx, z + dz)
            if not (bounds.min_x <= other[0] <= bounds.max_x
                    and bounds.min_z <= other[1] <= bounds.max_z):
                continue
            kind = _cell_kind(snapshot, other[0], feet_y, other[1])
            if kind == "blocked":
                continue
            candidate = (placements + (kind == "placeable"), steps + 1)
            if candidate[0] > policy.maximum_blocks:
                continue
            if candidate >= best.get(other, (policy.maximum_blocks + 1, 2**31 - 1)):
                continue
            best[other] = candidate
            previous[other] = current
            heapq.heappush(queue, (candidate[0], candidate[1], other[0], other[1]))
    if goal not in best or best[goal][0] == 0:
        return None
    required_placements = best[goal][0]
    path_2d = [goal]
    while path_2d[-1] != start:
        path_2d.append(previous[path_2d[-1]])
    path_2d.reverse()
    path = tuple((x, feet_y, z) for x, z in path_2d)
    first_index = next(
        index for index, (x, _, z) in enumerate(path)
        if _cell_kind(snapshot, x, feet_y, z) == "placeable"
    )
    if first_index == 0:
        raise ContractViolation("bridge plan cannot place under its start state")
    work_x, _, work_z = path[first_index - 1]
    destination_x, _, destination_z = path[first_index]
    dx, dz = destination_x - work_x, destination_z - work_z
    face = next(face for direction_x, direction_z, face in _DIRECTIONS
                if (direction_x, direction_z) == (dx, dz))
    support = (work_x, feet_y - 1, work_z)
    destination = (destination_x, feet_y - 1, destination_z)
    work_position = (
        # The centre must finish on the far side of the clicked face.  A
        # smaller offset combined with the endpoint tolerance can accept a
        # point that is still above the support block; its top face then
        # occludes the side face needed for placement.
        work_x + 0.5 + dx * 0.62,
        float(feet_y),
        work_z + 0.5 + dz * 0.62,
    )
    dependencies: tuple[BlockPos, ...] = tuple(sorted({
        support,
        destination,
        (destination_x, feet_y, destination_z),
        (destination_x, feet_y + 1, destination_z),
    }))
    requirement = RequiredInteraction(
        interaction_id=(
            f"{request.request_id}/place/"
            f"{destination_x}/{feet_y - 1}/{destination_z}"
        ),
        request_id=request.request_id,
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        world_session=request.world_session,
        kind=InteractionKind.PLACE_BLOCK,
        support=support,
        face=face,
        destination=destination,
        expected_item_id=policy.expected_item_id,
        expected_block_id=policy.expected_block_id,
        work_position=work_position,
        dependencies=dependencies,
        requires_sneak=True,
        work_position_tolerance=0.08,
    )
    return BridgeInteractionPlan(
        requirement=requirement,
        work_node=SurfaceNodeId(work_x, work_z, feet_y, 0),
        path=path,
        required_placements=required_placements,
        expanded_nodes=expanded,
    )
