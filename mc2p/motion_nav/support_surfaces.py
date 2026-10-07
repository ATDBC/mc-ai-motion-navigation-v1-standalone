"""Finite standable surfaces derived from known collision geometry."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.geometry import (
    QueryStatus, query_support, sweep, required_cells_for_sweep,
    unknown_shape_owner_is_fully_covered, _rectangle_union_area,
)
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.world_model import (
    Aabb, BlockPos, COLLISION_OWNER_BELOW_REACH_CELLS, CellKnowledge,
    WorldQueryCache, WorldView,
)


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


@dataclass(frozen=True, slots=True)
class StandablePointResult:
    status: QueryStatus
    position: tuple[float, float, float] | None = None
    dependencies: tuple[BlockPos, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()


@dataclass(frozen=True, slots=True)
class StandableRegionResult:
    status: QueryStatus
    completion_region: GroundCompletionRegion | None = None
    dependencies: tuple[BlockPos, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()

    @property
    def position(self) -> tuple[float, float, float] | None:
        return None if self.completion_region is None else self.completion_region.reference_point


def standable_region_in_goal(world: WorldView, surface: SupportSurface, region: Aabb,
                             *, body_width: float = .6, body_height: float = 1.8,
                             minimum_support_fraction: float = .5,
                             connection_from: tuple[float, float, float] | None = None,
                             allowed_materials: frozenset[str] | None = None,
                             query_cache: WorldQueryCache | None = None) -> StandableRegionResult:
    """Construct a single exact clearance rectangle, never an outer envelope.

    Support is piecewise bilinear between finite shape edges. Its minimum in
    each piece occurs at a vertex. A threshold cutting a piece is explicitly
    unsupported in V1, rather than approximated with a sampled rectangle.
    """
    if query_cache is not None:
        query_cache.validate_for(world)
    if (not math.isfinite(body_width) or not math.isfinite(body_height)
            or body_width <= 0 or body_height <= 0
            or not 0 < minimum_support_fraction <= 1):
        raise ContractViolation("standable region body/support limits are invalid")
    # The historical overlap helper permits numerical contact. A completion
    # reference must belong to the original goal exactly, including height.
    if not region.min_y <= surface.position[1] <= region.max_y:
        return StandableRegionResult(QueryStatus.BLOCKED)
    overlap = _region_overlap(surface, region)
    if overlap is None:
        return StandableRegionResult(QueryStatus.BLOCKED)
    lx, hx, lz, hz = overlap
    if lx >= hx or lz >= hz:
        return StandableRegionResult(QueryStatus.BLOCKED)
    y, half = surface.position[1], body_width / 2
    envelope = Aabb(lx-half, y, lz-half, hx+half, y+body_height, hz+half)
    selected = set(required_cells_for_sweep(envelope, (0., 0., 0.)))
    selected.update(query_support(envelope, world, query_cache=query_cache).dependencies)
    parts = ((lx, lz, hx, hz),)
    missing, unsupported, support_boxes, clipping_owners = set(), False, [], set()
    for owner in sorted(selected):
        fact = world.cell(owner) if query_cache is None else query_cache.cell(owner)
        if fact.knowledge is CellKnowledge.UNKNOWN:
            if unknown_shape_owner_is_fully_covered(world, owner):
                continue
            missing.add(owner)
            # The bounded owner may reach one cell above itself.
            boxes = (Aabb(owner[0], owner[1], owner[2],
                          owner[0]+1, owner[1]+2, owner[2]+1),)
        elif fact.knowledge is CellKnowledge.AIR:
            continue
        else:
            assert fact.block is not None
            if (allowed_materials is not None and fact.block.material_key not in allowed_materials):
                return StandableRegionResult(QueryStatus.UNSUPPORTED,
                    dependencies=tuple(sorted(set(surface.dependencies) | selected)))
            if fact.block.fluid or fact.block.collision_kind == 'unsupported':
                unsupported = True
                boxes = (Aabb(owner[0], owner[1], owner[2],
                              owner[0]+1, owner[1]+2, owner[2]+1),)
            else:
                boxes = fact.block.world_boxes(owner)
        for box in boxes:
            if abs(box.max_y-y) <= .05:
                support_boxes.append(box)
            if box.min_y >= y+body_height-_EPSILON or box.max_y <= y+_EPSILON:
                continue
            blocker = Aabb(box.min_x-half, y-1, box.min_z-half,
                           box.max_x+half, y+body_height+1, box.max_z+half)
            reduced_parts = tuple(reduced for part in parts for reduced in _subtract_footprint(part, blocker))
            if reduced_parts != parts and fact.knowledge is CellKnowledge.BLOCK:
                clipping_owners.add(owner)
            parts = reduced_parts
    dependencies = set(surface.dependencies) | selected
    def rejected(status):
        return StandableRegionResult(status, dependencies=tuple(sorted(dependencies)),
                                     missing_cells=tuple(sorted(missing)))
    if not parts:
        return rejected(QueryStatus.NEEDS_INFORMATION if missing else
                        QueryStatus.UNSUPPORTED if unsupported else QueryStatus.BLOCKED)
    lx, lz = min(p[0] for p in parts), min(p[1] for p in parts)
    hx, hz = max(p[2] for p in parts), max(p[3] for p in parts)
    if abs(_rectangle_union_area(list(parts)) - (hx-lx)*(hz-lz)) > _EPSILON:
        return rejected(QueryStatus.UNSUPPORTED)
    xs = {lx, hx} | {v for box in support_boxes for edge in (box.min_x, box.max_x)
                         for v in (edge-half, edge+half) if lx < v < hx}
    zs = {lz, hz} | {v for box in support_boxes for edge in (box.min_z, box.max_z)
                         for v in (edge-half, edge+half) if lz < v < hz}
    exact_dependencies = set()
    fractions = []
    for x in sorted(xs):
        for z in sorted(zs):
            body = Aabb(x-half, y, z-half, x+half, y+body_height, z+half)
            clearance = sweep(body, (0., 0., 0.), world, query_cache=query_cache)
            support = query_support(body, world, query_cache=query_cache)
            exact_dependencies.update((*clearance.dependencies, *support.dependencies))
            if clearance.status is not QueryStatus.FEASIBLE or support.status is not QueryStatus.FEASIBLE:
                missing.update((*clearance.missing_cells, *support.missing_cells))
                return rejected(QueryStatus.NEEDS_INFORMATION if missing else
                                QueryStatus.UNSUPPORTED if QueryStatus.UNSUPPORTED in
                                (clearance.status, support.status) else QueryStatus.BLOCKED)
            fractions.append(support.support_fraction)
    if min(fractions)+_EPSILON < minimum_support_fraction:
        return rejected(QueryStatus.BLOCKED if max(fractions)+_EPSILON < minimum_support_fraction
                        else QueryStatus.UNSUPPORTED)
    # Full rectangle envelope records clearance and support owners between
    # vertices, including facts which select its exact shape boundaries.
    exact_envelope = Aabb(lx-half, y, lz-half, hx+half, y+body_height, hz+half)
    exact_dependencies.update(required_cells_for_sweep(exact_envelope, (0., 0., 0.)))
    # Keep boundary-defining owners so the typed region recipe can replay
    # shape changes. A contact wall becoming hazardous invalidates the region.
    exact_dependencies.update(clipping_owners)
    exact_dependencies.update(query_support(exact_envelope, world, query_cache=query_cache).dependencies)
    # Retain the supporting surface identity, but not unused point-selection
    # facts outside the accepted body envelope (D059/D060).
    center = ((lx+hx)/2, (lz+hz)/2)
    x, z = center
    if connection_from is not None:
        # Project the incoming axis-aligned leg into the rectangle. Equal
        # line distance chooses the largest interior margin, then coordinate.
        target_x, target_z = (region.min_x+region.max_x)/2, (region.min_z+region.max_z)/2
        dx, dz = target_x-connection_from[0], target_z-connection_from[2]
        low, high = 0., math.inf
        for origin, delta, minimum, maximum in (
                (connection_from[0], dx, lx, hx), (connection_from[2], dz, lz, hz)):
            if abs(delta) <= _EPSILON:
                if not minimum <= origin <= maximum:
                    high = -1.
            else:
                a, b = sorted(((minimum-origin)/delta, (maximum-origin)/delta))
                low, high = max(low, a), min(high, b)
        if low < high and math.isfinite(high):
            # On the projected incoming ray, choose the point maximizing the
            # minimum rectangular boundary margin. Piecewise-linear extrema
            # occur at intersections of these four affine distances.
            margins = ((connection_from[0]-lx, dx), (hx-connection_from[0], -dx),
                       (connection_from[2]-lz, dz), (hz-connection_from[2], -dz))
            values = {low, high, (low+high)/2}
            for a, da in margins:
                for b, db in margins:
                    if abs(da-db) > _EPSILON:
                        value = (b-a)/(da-db)
                        if low <= value <= high:
                            values.add(value)
            t = min(values, key=lambda v: (-min(a+da*v for a, da in margins),
                                          connection_from[0]+dx*v, connection_from[2]+dz*v))
            x, z = connection_from[0]+dx*t, connection_from[2]+dz*t
    bounds = Aabb(lx, max(region.min_y, y-.05), lz,
                  hx, min(region.max_y, y+.05), hz)
    node = surface.node_id
    completion = GroundCompletionRegion(bounds, (x, y, z), y,
        (node.column_x, node.column_z, node.vertical_band, node.surface_index),
        tuple(sorted(exact_dependencies)))
    return StandableRegionResult(QueryStatus.FEASIBLE, completion, tuple(sorted(dependencies)))


def _region_overlap(surface: SupportSurface, region: Aabb):
    top_y = surface.position[1]
    if not region.min_y - _EPSILON <= top_y <= region.max_y + _EPSILON:
        return None
    low_x, high_x = max(region.min_x, surface.region.min_x), min(region.max_x, surface.region.max_x)
    low_z, high_z = max(region.min_z, surface.region.min_z), min(region.max_z, surface.region.max_z)
    if low_x > high_x + _EPSILON or low_z > high_z + _EPSILON:
        return None
    return low_x, high_x, low_z, high_z


def surface_overlaps_region(surface: SupportSurface, region: Aabb) -> bool:
    """Cheap necessary condition; final admission must verify a real body."""
    return _region_overlap(surface, region) is not None


def standable_point_in_region(world: WorldView, surface: SupportSurface, region: Aabb,
                              *, body_width: float = .6, body_height: float = 1.8,
                              minimum_support_fraction: float = .5,
                              connection_from: tuple[float, float, float] | None = None,
                              query_cache: WorldQueryCache | None = None) -> StandablePointResult:
    """Select a concrete goal position with the existing clearance/support rules."""
    if query_cache is not None and (
            type(query_cache) is not WorldQueryCache
            or query_cache.world is not world):
        raise ContractViolation("standable point query cache belongs to another world view")
    overlap = _region_overlap(surface, region)
    if overlap is None:
        return StandablePointResult(QueryStatus.BLOCKED)
    low_x, high_x, low_z, high_z = overlap
    center_x = (region.min_x + region.max_x) / 2
    center_z = (region.min_z + region.max_z) / 2
    primary = (min(max(center_x, low_x), high_x), min(max(center_z, low_z), high_z))
    candidates = dict.fromkeys((primary, ((low_x + high_x) / 2, (low_z + high_z) / 2),
                               *((x, z) for x in (low_x, high_x) for z in (low_z, high_z))))
    dependencies, missing = set(surface.dependencies), set()
    unsupported = False
    y, half = surface.position[1], body_width / 2
    for x, z in candidates:
        body = Aabb(x - half, y, z - half, x + half, y + body_height, z + half)
        clearance = sweep(body, (0., 0., 0.), world, query_cache=query_cache)
        support = query_support(body, world, query_cache=query_cache)
        dependencies.update((*clearance.dependencies, *support.dependencies))
        missing.update((*clearance.missing_cells, *support.missing_cells))
        unsupported |= QueryStatus.UNSUPPORTED in (clearance.status, support.status)
        if (clearance.status is QueryStatus.FEASIBLE and support.status is QueryStatus.FEASIBLE
                and support.support_fraction + _EPSILON >= minimum_support_fraction):
            if connection_from is not None:
                dx, dy, dz = x - connection_from[0], y - connection_from[1], z - connection_from[2]
                start = body.moved(-dx, -dy, -dz)
                connection = sweep(
                    start, (dx, dy, dz), world, query_cache=query_cache,
                )
                dependencies.update(connection.dependencies)
                missing.update(connection.missing_cells)
                unsupported |= connection.status is QueryStatus.UNSUPPORTED
                if abs(dy) > _EPSILON or connection.status is not QueryStatus.FEASIBLE:
                    continue
                count = max(1, math.ceil(math.hypot(dx, dz) / .1))
                supported = True
                for index in range(count + 1):
                    checked = query_support(
                        start.moved(dx * index / count, 0., dz * index / count),
                        world,
                        query_cache=query_cache,
                    )
                    dependencies.update(checked.dependencies)
                    missing.update(checked.missing_cells)
                    unsupported |= checked.status is QueryStatus.UNSUPPORTED
                    supported &= checked.status is QueryStatus.FEASIBLE and checked.support_fraction + _EPSILON >= minimum_support_fraction
                if not supported:
                    continue
            return StandablePointResult(QueryStatus.FEASIBLE, (x, y, z), tuple(sorted(dependencies)))
    return StandablePointResult(QueryStatus.NEEDS_INFORMATION if missing else
                               QueryStatus.UNSUPPORTED if unsupported else QueryStatus.BLOCKED,
                               dependencies=tuple(sorted(dependencies)), missing_cells=tuple(sorted(missing)))


def query_standable_connection(world: WorldView, surface: SupportSurface,
                              position: tuple[float, float, float], connection_from,
                              *, body_width=.6, body_height=1.8,
                              query_cache: WorldQueryCache | None = None):
    """Check exactly this endpoint; never select a different point."""
    x, y, z = position
    tiny = 1.e-8
    result = standable_point_in_region(world, surface,
        Aabb(x-tiny, y-tiny, z-tiny, x+tiny, y+tiny, z+tiny),
        body_width=body_width, body_height=body_height,
        connection_from=connection_from, query_cache=query_cache)
    return StandablePointResult(result.status,
        position if result.status is QueryStatus.FEASIBLE else None,
        result.dependencies, result.missing_cells)


def _candidate_owner_cells(column_x: int, column_z: int,
                           minimum_y: float, maximum_y: float) -> tuple[BlockPos, ...]:
    low = math.floor(minimum_y) - COLLISION_OWNER_BELOW_REACH_CELLS
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
    collect_complete_missing: bool = False,
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
    if type(collect_complete_missing) is not bool:
        raise ContractViolation("complete missing selection must be boolean")

    owner_cells = _candidate_owner_cells(
        column_x, column_z, minimum_feet_y, maximum_feet_y,
    )
    missing = tuple(
        position for position in owner_cells
        if (world.cell(position).knowledge is CellKnowledge.UNKNOWN
            and not unknown_shape_owner_is_fully_covered(world, position))
    )
    if missing and not collect_complete_missing:
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
    query_missing: set[BlockPos] = set(missing)
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
