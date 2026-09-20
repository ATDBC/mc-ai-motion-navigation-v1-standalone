"""Finite standable surfaces derived from known collision geometry."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.world_model import Aabb, BlockPos, CellKnowledge, WorldView


_EPSILON = 1.0e-9


@dataclass(frozen=True, order=True, slots=True)
class SurfaceNodeId:
    column_x: int
    column_z: int
    vertical_band: int
    surface_index: int

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (
                self.column_x, self.column_z, self.vertical_band,
                self.surface_index)):
            raise ContractViolation("surface node id must use integers")
        if self.surface_index < 0:
            raise ContractViolation("surface index must be nonnegative")


@dataclass(frozen=True, slots=True)
class HorizontalRegion:
    min_x: float
    min_z: float
    max_x: float
    max_z: float

    def __post_init__(self) -> None:
        values = (self.min_x, self.min_z, self.max_x, self.max_z)
        if any(type(value) not in (int, float) or not math.isfinite(float(value))
               for value in values):
            raise ContractViolation("surface region coordinates must be finite")
        if not self.min_x < self.max_x or not self.min_z < self.max_z:
            raise ContractViolation("surface region must have positive area")


@dataclass(frozen=True, slots=True)
class SupportSurface:
    node_id: SurfaceNodeId
    position: tuple[float, float, float]
    region: HorizontalRegion
    support_fraction: float
    materials: tuple[str, ...]
    dependencies: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        if type(self.node_id) is not SurfaceNodeId:
            raise ContractViolation("support surface requires a typed node id")
        if (type(self.position) is not tuple or len(self.position) != 3
                or any(type(value) not in (int, float)
                       or not math.isfinite(float(value))
                       for value in self.position)):
            raise ContractViolation("support surface position must be finite")
        if type(self.region) is not HorizontalRegion:
            raise ContractViolation("support surface requires a horizontal region")
        if not 0.0 <= self.support_fraction <= 1.0:
            raise ContractViolation("support fraction must be within 0..1")
        if type(self.materials) is not tuple or type(self.dependencies) is not tuple:
            raise ContractViolation("support surface facts must be immutable")


@dataclass(frozen=True, slots=True)
class SupportSurfaceResult:
    status: QueryStatus
    surfaces: tuple[SupportSurface, ...]
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...] = ()


def _candidate_owner_cells(column_x: int, column_z: int,
                           minimum_y: float, maximum_y: float) -> tuple[BlockPos, ...]:
    low = math.floor(minimum_y) - 2
    high = math.ceil(maximum_y) - 1
    return tuple((column_x, y, column_z) for y in range(low, high + 1))


def _surface_components(
    rectangles: list[tuple[float, float, float, float, BlockPos]],
) -> tuple[tuple[float, float, float, float, frozenset[BlockPos]], ...]:
    remaining = list(rectangles)
    components = []
    while remaining:
        component = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for rectangle in tuple(remaining):
                joined = False
                for current in component:
                    x_overlap = min(rectangle[2], current[2]) - max(rectangle[0], current[0])
                    z_overlap = min(rectangle[3], current[3]) - max(rectangle[1], current[1])
                    if (x_overlap >= -_EPSILON and z_overlap >= -_EPSILON
                            and (x_overlap > _EPSILON or z_overlap > _EPSILON)):
                        joined = True
                        break
                if joined:
                    component.append(rectangle)
                    remaining.remove(rectangle)
                    changed = True
        components.append((
            min(item[0] for item in component),
            min(item[1] for item in component),
            max(item[2] for item in component),
            max(item[3] for item in component),
            frozenset(item[4] for item in component),
        ))
    return tuple(sorted(components, key=lambda item: (
        item[0], item[1], item[2], item[3], tuple(sorted(item[4])),
    )))


def _subtract_footprint(
    rectangle: tuple[float, float, float, float],
    blocker: Aabb,
) -> tuple[tuple[float, float, float, float], ...]:
    min_x, min_z, max_x, max_z = rectangle
    overlap_min_x = max(min_x, blocker.min_x)
    overlap_max_x = min(max_x, blocker.max_x)
    overlap_min_z = max(min_z, blocker.min_z)
    overlap_max_z = min(max_z, blocker.max_z)
    if (overlap_min_x >= overlap_max_x - _EPSILON
            or overlap_min_z >= overlap_max_z - _EPSILON):
        return (rectangle,)
    parts = (
        (min_x, min_z, overlap_min_x, max_z),
        (overlap_max_x, min_z, max_x, max_z),
        (overlap_min_x, min_z, overlap_max_x, overlap_min_z),
        (overlap_min_x, overlap_max_z, overlap_max_x, max_z),
    )
    return tuple(part for part in parts
                 if part[0] < part[2] - _EPSILON
                 and part[1] < part[3] - _EPSILON)


def query_support_surfaces(
    world: WorldView,
    column_x: int,
    column_z: int,
    minimum_feet_y: float,
    maximum_feet_y: float,
    *,
    body_width: float = 0.6,
    body_height: float = 1.8,
    minimum_support_fraction: float = 0.5,
) -> SupportSurfaceResult:
    """Return stable standable representatives for one horizontal column."""
    if type(world) is not WorldView or type(column_x) is not int or type(column_z) is not int:
        raise ContractViolation("surface query requires a world view and integer column")
    values = (minimum_feet_y, maximum_feet_y, body_width,
              body_height, minimum_support_fraction)
    if any(type(value) not in (int, float) or not math.isfinite(float(value))
           for value in values):
        raise ContractViolation("surface query values must be finite")
    if (minimum_feet_y > maximum_feet_y or body_width <= 0 or body_height <= 0
            or not 0 < minimum_support_fraction <= 1):
        raise ContractViolation("surface query range and body dimensions are invalid")

    owner_cells = _candidate_owner_cells(
        column_x, column_z, minimum_feet_y, maximum_feet_y,
    )
    missing = tuple(position for position in owner_cells
                    if world.cell(position).knowledge is CellKnowledge.UNKNOWN)
    if missing:
        return SupportSurfaceResult(
            QueryStatus.NEEDS_INFORMATION, (), owner_cells, missing,
        )

    unsupported = False
    top_rectangles: list[tuple[float, float, float, float, float, BlockPos]] = []
    collision_boxes: list[Aabb] = []
    column_min_x, column_max_x = float(column_x), float(column_x + 1)
    column_min_z, column_max_z = float(column_z), float(column_z + 1)
    for owner in owner_cells:
        fact = world.cell(owner)
        if fact.knowledge is not CellKnowledge.BLOCK:
            continue
        assert fact.block is not None
        if fact.block.fluid or fact.block.collision_kind == "unsupported":
            unsupported = True
            continue
        for box in fact.block.world_boxes(owner):
            collision_boxes.append(box)
            top_y = box.max_y
            if top_y < minimum_feet_y - _EPSILON or top_y > maximum_feet_y + _EPSILON:
                continue
            min_x, max_x = max(column_min_x, box.min_x), min(column_max_x, box.max_x)
            min_z, max_z = max(column_min_z, box.min_z), min(column_max_z, box.max_z)
            if min_x < max_x - _EPSILON and min_z < max_z - _EPSILON:
                top_rectangles.append((top_y, min_x, min_z, max_x, max_z, owner))

    exposed_rectangles = []
    for top_y, min_x, min_z, max_x, max_z, owner in top_rectangles:
        parts = ((min_x, min_z, max_x, max_z),)
        for blocker in collision_boxes:
            if not (blocker.min_y <= top_y + _EPSILON
                    and blocker.max_y > top_y + _EPSILON):
                continue
            parts = tuple(
                reduced
                for part in parts
                for reduced in _subtract_footprint(part, blocker)
            )
            if not parts:
                break
        exposed_rectangles.extend(
            (top_y, part[0], part[1], part[2], part[3], owner)
            for part in parts
        )

    half = body_width / 2.0
    candidates: list[tuple[float, HorizontalRegion, frozenset[BlockPos]]] = []
    by_height: dict[float, list[tuple[float, float, float, float, BlockPos]]] = {}
    for top_y, min_x, min_z, max_x, max_z, owner in exposed_rectangles:
        by_height.setdefault(top_y, []).append((min_x, min_z, max_x, max_z, owner))
    for top_y, rectangles in sorted(by_height.items()):
        for min_x, min_z, max_x, max_z, owners in _surface_components(rectangles):
            center_x = (min_x + max_x) / 2.0
            center_z = (min_z + max_z) / 2.0
            region = HorizontalRegion(min_x, min_z, max_x, max_z)
            candidates.append((top_y, region, owners))

    provisional: list[tuple[tuple[float, float, float], HorizontalRegion,
                            float, tuple[str, ...], tuple[BlockPos, ...]]] = []
    query_missing: set[BlockPos] = set()
    query_dependencies: set[BlockPos] = set(owner_cells)
    def axis_candidates(minimum: float, maximum: float) -> tuple[float, ...]:
        center = (minimum + maximum) / 2.0
        values = (center, minimum + half, maximum - half, minimum, maximum)
        ordered = []
        for value in values:
            value = min(max(value, minimum), maximum)
            if not any(abs(value - known) <= _EPSILON for known in ordered):
                ordered.append(value)
        return tuple(ordered)

    for top_y, region, owners in sorted(candidates, key=lambda item: (
            item[0], item[1].min_x, item[1].min_z,
            item[1].max_x, item[1].max_z, tuple(sorted(item[2])),
    )):
        accepted = False
        for x in axis_candidates(region.min_x, region.max_x):
            if accepted:
                break
            for z in axis_candidates(region.min_z, region.max_z):
                position = (x, top_y, z)
                body = Aabb(x - half, top_y, z - half,
                            x + half, top_y + body_height, z + half)
                clearance = sweep(body, (0.0, 0.0, 0.0), world)
                support = query_support(body, world)
                dependencies = tuple(sorted(
                    set(clearance.dependencies) | set(support.dependencies) | owners
                ))
                query_dependencies.update(dependencies)
                query_missing.update(clearance.missing_cells)
                query_missing.update(support.missing_cells)
                if (clearance.status is QueryStatus.UNSUPPORTED
                        or support.status is QueryStatus.UNSUPPORTED):
                    unsupported = True
                    continue
                if (clearance.status is QueryStatus.NEEDS_INFORMATION
                        or support.status is QueryStatus.NEEDS_INFORMATION):
                    continue
                if clearance.status is QueryStatus.BLOCKED:
                    continue
                if (support.status is QueryStatus.FEASIBLE
                        and support.support_fraction + _EPSILON
                        >= minimum_support_fraction):
                    provisional.append((
                        position, region, support.support_fraction,
                        support.support_materials, dependencies,
                    ))
                    accepted = True
                    break

    if query_missing:
        return SupportSurfaceResult(
            QueryStatus.NEEDS_INFORMATION, (), tuple(sorted(query_dependencies)),
            tuple(sorted(query_missing)),
        )

    ordered = sorted(provisional, key=lambda item: (
        item[0][1], item[0][0], item[0][2], item[4],
    ))
    band_counts: dict[int, int] = {}
    surfaces = []
    for position, region, fraction, materials, dependencies in ordered:
        band = math.floor(position[1] + _EPSILON)
        index = band_counts.get(band, 0)
        band_counts[band] = index + 1
        surfaces.append(SupportSurface(
            SurfaceNodeId(column_x, column_z, band, index),
            position, region, fraction, materials, dependencies,
        ))
    if surfaces:
        return SupportSurfaceResult(
            QueryStatus.FEASIBLE, tuple(surfaces),
            tuple(sorted(query_dependencies)),
        )
    if unsupported:
        return SupportSurfaceResult(
            QueryStatus.UNSUPPORTED, (), tuple(sorted(query_dependencies)),
        )
    return SupportSurfaceResult(
        QueryStatus.BLOCKED, (), tuple(sorted(query_dependencies)),
    )
