"""Shared support and continuous swept-AABB queries over three-state knowledge."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.world_model import (
    Aabb, BlockPos, COLLISION_OWNER_BELOW_REACH_CELLS, CellKnowledge, WorldView,
)


_EPSILON = 1.0e-9


class QueryStatus(StrEnum):
    FEASIBLE = "feasible"
    BLOCKED = "blocked"
    NEEDS_INFORMATION = "needs_information"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class SweepResult:
    status: QueryStatus
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...] = ()
    first_collision_fraction: float | None = None


@dataclass(frozen=True, slots=True)
class SupportResult:
    status: QueryStatus
    dependencies: tuple[BlockPos, ...]
    support_area: float
    support_fraction: float
    missing_cells: tuple[BlockPos, ...] = ()
    support_materials: tuple[str, ...] = ()


def _axis_cells(minimum: float, maximum: float) -> range:
    return range(math.floor(minimum + _EPSILON), math.floor(maximum - _EPSILON) + 1)


def _possible_owner_cells(cells: tuple[BlockPos, ...]) -> tuple[BlockPos, ...]:
    """Include lower owners whose bounded shape may enter queried cells."""
    reach = COLLISION_OWNER_BELOW_REACH_CELLS
    return tuple(sorted({
        (x, y - dy, z)
        for x, y, z in cells
        for dy in range(reach + 1)
    }))


def required_cells_for_sweep(body: Aabb, delta: tuple[float, float, float]) -> tuple[BlockPos, ...]:
    if type(body) is not Aabb or type(delta) is not tuple or len(delta) != 3:
        raise ContractViolation("sweep requires an AABB and a three-axis displacement")
    if any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in delta):
        raise ContractViolation("sweep displacement must be finite")
    dx, dy, dz = delta
    end = body.moved(dx, dy, dz)
    broad = Aabb(min(body.min_x, end.min_x), min(body.min_y, end.min_y), min(body.min_z, end.min_z),
                 max(body.max_x, end.max_x), max(body.max_y, end.max_y), max(body.max_z, end.max_z))
    occupied = tuple((x, y, z) for x in _axis_cells(broad.min_x, broad.max_x)
                     for y in _axis_cells(broad.min_y, broad.max_y)
                     for z in _axis_cells(broad.min_z, broad.max_z))
    return _possible_owner_cells(occupied)


def _sweep_fraction(body: Aabb, delta: tuple[float, float, float], obstacle: Aabb) -> float | None:
    initially_overlapping = (
        body.max_x > obstacle.min_x + _EPSILON
        and body.min_x < obstacle.max_x - _EPSILON
        and body.max_y > obstacle.min_y + _EPSILON
        and body.min_y < obstacle.max_y - _EPSILON
        and body.max_z > obstacle.min_z + _EPSILON
        and body.min_z < obstacle.max_z - _EPSILON
    )
    entries, exits = [], []
    for body_min, body_max, obstacle_min, obstacle_max, movement in (
            (body.min_x, body.max_x, obstacle.min_x, obstacle.max_x, delta[0]),
            (body.min_y, body.max_y, obstacle.min_y, obstacle.max_y, delta[1]),
            (body.min_z, body.max_z, obstacle.min_z, obstacle.max_z, delta[2])):
        # Contact within the geometry tolerance is not penetration.  If the
        # requested movement goes farther away on any separating axis, the
        # boxes cannot collide during this sweep.  Checking this before time
        # division also keeps the result translation invariant at negative
        # integer coordinates, where the same contact can carry a few extra
        # floating-point bits.
        if (movement > _EPSILON and body_min >= obstacle_max - _EPSILON):
            return None
        if (movement < -_EPSILON and body_max <= obstacle_min + _EPSILON):
            return None
        if abs(movement) <= _EPSILON:
            if body_max <= obstacle_min + _EPSILON or body_min >= obstacle_max - _EPSILON:
                return None
            entries.append(float("-inf")); exits.append(float("inf"))
        elif movement > 0:
            entries.append((obstacle_min - body_max) / movement)
            exits.append((obstacle_max - body_min) / movement)
        else:
            entries.append((obstacle_max - body_min) / movement)
            exits.append((obstacle_min - body_max) / movement)
    entry, exit_ = max(entries), min(exits)
    if entry > exit_ + _EPSILON or exit_ < -_EPSILON or entry > 1 + _EPSILON:
        return None
    if not initially_overlapping and exit_ <= _EPSILON:
        return None
    if entry >= 1.0 - _EPSILON:
        return None
    return max(0.0, entry)


def sweep(body: Aabb, delta: tuple[float, float, float], world: WorldView) -> SweepResult:
    if type(world) is not WorldView:
        raise ContractViolation("sweep requires a world view")
    cells = required_cells_for_sweep(body, delta)
    missing = []
    unsupported = False
    first: float | None = None
    for position in cells:
        fact = world.cell(position)
        if fact.knowledge is CellKnowledge.UNKNOWN:
            missing.append(position)
            continue
        if fact.knowledge is CellKnowledge.AIR:
            continue
        assert fact.block is not None
        if fact.block.fluid or fact.block.collision_kind == "unsupported":
            unsupported = True
            continue
        for obstacle in fact.block.world_boxes(position):
            fraction = _sweep_fraction(body, delta, obstacle)
            if fraction is not None and (first is None or fraction < first):
                first = fraction
    if first is not None:
        return SweepResult(QueryStatus.BLOCKED, cells, tuple(missing), first)
    if unsupported:
        return SweepResult(QueryStatus.UNSUPPORTED, cells, tuple(missing))
    if missing:
        return SweepResult(QueryStatus.NEEDS_INFORMATION, cells, tuple(missing))
    return SweepResult(QueryStatus.FEASIBLE, cells)


def _rectangle_union_area(rectangles: list[tuple[float, float, float, float]]) -> float:
    if not rectangles:
        return 0.0
    xs = sorted({value for rectangle in rectangles for value in (rectangle[0], rectangle[2])})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        if right - left <= _EPSILON:
            continue
        intervals = sorted((bottom, top) for x0, bottom, x1, top in rectangles
                           if x0 < right - _EPSILON and x1 > left + _EPSILON)
        covered = 0.0
        if intervals:
            start, end = intervals[0]
            for next_start, next_end in intervals[1:]:
                if next_start <= end + _EPSILON:
                    end = max(end, next_end)
                else:
                    covered += end - start
                    start, end = next_start, next_end
            covered += end - start
        area += (right - left) * covered
    return area


def query_support(body: Aabb, world: WorldView, *, vertical_tolerance: float = 0.05) -> SupportResult:
    if type(body) is not Aabb or type(world) is not WorldView:
        raise ContractViolation("support query requires an AABB and world view")
    if (type(vertical_tolerance) not in (int, float) or not math.isfinite(float(vertical_tolerance))
            or vertical_tolerance < 0):
        raise ContractViolation("support tolerance must be finite and nonnegative")
    support_levels = sorted({
        math.floor(body.min_y - _EPSILON),
        math.floor(body.min_y - vertical_tolerance - _EPSILON),
    })
    contact_cells = tuple(
        (x, support_y, z)
        for x in _axis_cells(body.min_x, body.max_x)
        for support_y in support_levels
        for z in _axis_cells(body.min_z, body.max_z)
    )
    cells = _possible_owner_cells(contact_cells)
    missing = []
    unsupported = False
    rectangles: list[tuple[float, float, float, float]] = []
    materials: set[str] = set()
    for position in cells:
        fact = world.cell(position)
        if fact.knowledge is CellKnowledge.UNKNOWN:
            missing.append(position)
            continue
        if fact.knowledge is CellKnowledge.AIR:
            continue
        assert fact.block is not None
        if fact.block.fluid or fact.block.collision_kind == "unsupported":
            unsupported = True
            continue
        for box in fact.block.world_boxes(position):
            if abs(box.max_y - body.min_y) > vertical_tolerance:
                continue
            left, right = max(body.min_x, box.min_x), min(body.max_x, box.max_x)
            bottom, top = max(body.min_z, box.min_z), min(body.max_z, box.max_z)
            if left < right - _EPSILON and bottom < top - _EPSILON:
                rectangles.append((left, bottom, right, top))
                materials.add(fact.block.material_key)
    area = _rectangle_union_area(rectangles)
    footprint = (body.max_x - body.min_x) * (body.max_z - body.min_z)
    fraction = min(1.0, area / footprint)
    if area > _EPSILON:
        return SupportResult(QueryStatus.FEASIBLE, cells, area, fraction,
                             tuple(missing), tuple(sorted(materials)))
    if unsupported:
        return SupportResult(QueryStatus.UNSUPPORTED, cells, 0.0, 0.0, tuple(missing))
    if missing:
        return SupportResult(QueryStatus.NEEDS_INFORMATION, cells, 0.0, 0.0, tuple(missing))
    return SupportResult(QueryStatus.BLOCKED, cells, 0.0, 0.0)
