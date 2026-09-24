"""Bounded candidates from observed blocks, not a complete occupancy map.

Allowing a short step means the declared checks passed, not that sparse rays have
proved an arbitrary body volume empty. New collision observations stop/replan.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping

from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.follow_types import MOVEMENT_FRESHNESS_NS, TARGET_TTL_NS, FollowView
from mc2p.contracts.observation_v3 import ObservedBlockV3, AabbV3
from mc2p.skills.block_geometry import SUPPORTED_FLOORS, world_boxes, is_supported_floor, overlaps

Cell = tuple[int, int]
Block = tuple[int, int, int]
RADIUS = 8.0
MAX_RECORDS = 512
MAX_EXPANSIONS = 256
BODY_MARGIN = .35  # Standing width .6 plus .05; other poses are unsupported.


@dataclass(frozen=True, slots=True)
class BlockRecord:
    block: ObservedBlockV3
    sequence_id: int
    last_seen_ns: int


@dataclass(frozen=True, slots=True)
class RouteDecision:
    cells: tuple[Cell, ...]
    reason: str
    expansions: int = 0


@dataclass(frozen=True, slots=True)
class MotionDecision:
    allowed: bool
    reason: str


class LocalBlockMemory:
    def __init__(self) -> None:
        self._records: dict[Block, BlockRecord] = {}
        self._view: FollowView | None = None

    @property
    def records(self) -> Mapping[Block, BlockRecord]:
        return MappingProxyType(self._records)

    def clear(self) -> None:
        self._records.clear()
        self._view = None

    def update(self, view: FollowView) -> None:
        if not view.available or view.own is None:
            self.clear()
            return
        old = self._view
        if old is not None:
            if (view.episode_id, view.controller_clock_id, view.client_clock_id) != (
                    old.episode_id, old.controller_clock_id, old.client_clock_id):
                self.clear()
            elif view.sequence_id <= old.sequence_id:
                if view.sequence_id == old.sequence_id and view == old:
                    return
                self.clear()
                raise ContractViolation("local map observation sequence regressed or changed")
            elif view.received_at_ns < old.received_at_ns:
                self.clear()
                raise ContractViolation("local map observation time regressed")
        self._view = view
        for block in view.observed_blocks:
            self._records[block.position] = BlockRecord(block, view.sequence_id, view.request_start_ns)
        self.prune(view.own.position, view.received_at_ns)

    def prune(self, position: Vec3V0, now_ns: int) -> None:
        require_nonnegative_int(now_ns, "map controller time")
        def distance(block: Block) -> float:
            return math.hypot(block[0] + .5 - position.x, block[2] + .5 - position.z)
        self._records = {block: record for block, record in self._records.items()
                         if 0 <= now_ns - record.last_seen_ns < TARGET_TTL_NS
                         and distance(block) <= RADIUS and abs(block[1] - position.y) <= RADIUS}
        if len(self._records) > MAX_RECORDS:
            ordered = sorted(self._records, key=lambda b: (-self._records[b].last_seen_ns, distance(b), b))
            self._records = {block: self._records[block] for block in ordered[:MAX_RECORDS]}


def _floor(view: FollowView) -> int | None:
    if view.own is None or abs(view.own.position.y - round(view.own.position.y)) > .001:
        return None
    return round(view.own.position.y) - 1


def _support(memory: LocalBlockMemory, cell: Cell, floor: int, now_ns: int, freshness: int) -> bool:
    record = memory.records.get((cell[0], floor, cell[1]))
    if record is None:
        return False
    return 0 <= now_ns - record.last_seen_ns < freshness and is_supported_floor(record.block)


def _intersects(minimum: float, maximum: float, low: float, high: float) -> bool:
    return minimum < high - 1e-7 and maximum > low + 1e-7


def prepare_block_collision(block: ObservedBlockV3) -> tuple[AabbV3, ...] | None:
    """Pure per-check geometry, not a permission or a cross-observation cache."""
    try:
        boxes = world_boxes(block)
    except ContractViolation:
        return None  # Unsupported geometry is never a clearance certificate.
    if block.fluid_id is not None:
        x,y,z = block.position
        boxes += (AabbV3(x,y,z,x+1,y+1,z+1),)  # Keep the existing unsupported-fluid boundary.
    return boxes


def collision_obstructs(boxes: tuple[AabbV3, ...] | None, body: AabbV3) -> bool:
    return boxes is None or any(overlaps(body,box) for box in boxes)


def block_obstructs(block: ObservedBlockV3, body: AabbV3) -> bool:
    return collision_obstructs(prepare_block_collision(block),body)


def prepare_obstacles(memory, feet, ceiling):
    """Per-query immutable geometry; no permission, pruning or time renewal."""
    obstacles=[]
    for block,record in memory.records.items():
        boxes=prepare_block_collision(record.block)
        if boxes is not None:
            boxes=tuple(box for box in boxes if feet < box.max_y-1e-7 and ceiling > box.min_y+1e-7)
            if not boxes:
                continue
        obstacles.append((block,record,boxes))
    return tuple(obstacles)


def _obstacle(memory: LocalBlockMemory, view: FollowView, x: float, z: float, floor: int, *, obstacles=None) -> bool:
    feet = floor + 1
    body = AabbV3(x-BODY_MARGIN,feet,z-BODY_MARGIN,x+BODY_MARGIN,feet+1.8,z+BODY_MARGIN)
    if obstacles is None:
        obstacles=prepare_obstacles(memory,feet,feet+1.8)
    for _,_,boxes in obstacles:
        if collision_obstructs(boxes,body):
            return True
    for entity in view.entities:
        if (_intersects(feet, feet + 1.8, entity.position.y, entity.position.y + entity.size.y)
                and _intersects(x-BODY_MARGIN, x+BODY_MARGIN,
                                entity.position.x-entity.size.x/2, entity.position.x+entity.size.x/2)
                and _intersects(z-BODY_MARGIN, z+BODY_MARGIN,
                                entity.position.z-entity.size.z/2, entity.position.z+entity.size.z/2)):
            return True
    return False


def plan_local_route(memory: LocalBlockMemory, view: FollowView, goal: Vec3V0, now_ns: int,
                     forbidden_edges: tuple[tuple[Cell, Cell], ...] = (), *, stop_distance: float = 0,
                     support_freshness_ns: int = TARGET_TTL_NS) -> RouteDecision:
    require_nonnegative_int(support_freshness_ns, "candidate support freshness")
    floor = _floor(view)
    if not view.available or view.own is None or floor is None or abs(goal.y - floor - 1) > .1:
        return RouteDecision((), "unsupported_height_or_observation")
    memory.prune(view.own.position, now_ns)
    obstacles=prepare_obstacles(memory,floor+1,floor+1+1.8)
    start = (math.floor(view.own.position.x), math.floor(view.own.position.z))
    target = (math.floor(goal.x), math.floor(goal.z))
    queue = deque([start])
    parents: dict[Cell, Cell | None] = {start: None}
    expansions = 0
    while queue and expansions < MAX_EXPANSIONS:
        cell = queue.popleft()
        expansions += 1
        if not _support(memory, cell, floor, now_ns, support_freshness_ns) or _obstacle(memory, view, cell[0]+.5, cell[1]+.5, floor,obstacles=obstacles):
            continue
        if (cell == target and stop_distance == 0) or (stop_distance > 0 and math.hypot(cell[0]+.5-goal.x, cell[1]+.5-goal.z) <= stop_distance):
            path = []
            cursor: Cell | None = cell
            while cursor is not None:
                path.append(cursor)
                cursor = parents[cursor]
            return RouteDecision(tuple(reversed(path)), "candidate_route", expansions)
        neighbors = [(cell[0]+1, cell[1]), (cell[0], cell[1]+1), (cell[0]-1, cell[1]), (cell[0], cell[1]-1)]
        for neighbor in neighbors:
            if neighbor in parents or (cell, neighbor) in forbidden_edges or (neighbor, cell) in forbidden_edges:
                continue
            if math.hypot(neighbor[0]+.5-view.own.position.x, neighbor[1]+.5-view.own.position.z) > RADIUS:
                continue
            parents[neighbor] = cell
            queue.append(neighbor)
    return RouteDecision((), "search_budget_exhausted" if queue else "no_known_route", expansions)


def guard_motion(memory: LocalBlockMemory, view: FollowView, waypoint: Vec3V0, now_ns: int) -> MotionDecision:
    floor = _floor(view)
    own = view.own
    if not view.available or own is None or floor is None:
        return MotionDecision(False, "unavailable_motion_state")
    if not 0 <= now_ns - view.received_at_ns <= MOVEMENT_FRESHNESS_NS:
        return MotionDecision(False, "stale_motion_state")
    if own.dead or not own.on_ground or own.unsupported_motion:
        return MotionDecision(False, "unsupported_motion_state")
    if own.horizontal_collision:
        return MotionDecision(False, "current_collision")
    if abs(waypoint.y - own.position.y) > .1:
        return MotionDecision(False, "unsupported_height")
    speed = math.hypot(own.velocity.x, own.velocity.z)
    if speed > .3 or abs(own.velocity.y) > .1:
        return MotionDecision(False, "unsupported_velocity")
    dx, dz = waypoint.x-own.position.x, waypoint.z-own.position.z
    distance = math.hypot(dx, dz)
    if distance < .01:
        return MotionDecision(False, "already_at_waypoint")
    # Conservative short-step plus release drift horizon; one input sample is not a fixed displacement.
    horizon = .45 + 2 * speed
    steps = math.ceil(horizon / .1)
    memory.prune(own.position, now_ns)
    for index in range(steps + 1):
        fraction = index / steps
        x, z = own.position.x + dx/distance*horizon*fraction, own.position.z + dz/distance*horizon*fraction
        drift_x, drift_z = 2 * own.velocity.x * fraction, 2 * own.velocity.z * fraction
        for bx in range(math.floor(x-BODY_MARGIN+min(0, drift_x)), math.floor(x+BODY_MARGIN+max(0, drift_x)) + 1):
            for bz in range(math.floor(z-BODY_MARGIN+min(0, drift_z)), math.floor(z+BODY_MARGIN+max(0, drift_z)) + 1):
                if not _support(memory, (bx, bz), floor, now_ns, MOVEMENT_FRESHNESS_NS + 1):
                    return MotionDecision(False, "unknown_footprint_support")
        if any(_obstacle(memory, view, x+offset_x, z+offset_z, floor)
               for offset_x in (0, drift_x) for offset_z in (0, drift_z)):
            return MotionDecision(False, "observed_body_obstacle")
    return MotionDecision(True, "bounded_step_checks_passed")
