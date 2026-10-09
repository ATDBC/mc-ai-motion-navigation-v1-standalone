"""Finite standable surfaces derived from known collision geometry."""
from __future__ import annotations

from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.geometry import (
    QueryStatus, SupportResult, query_support, sweep, required_cells_for_sweep,
    unknown_shape_owner_is_fully_covered,
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


@dataclass(slots=True)
class StandableRegionDiagnostics:
    """Optional bounded grid counters for performance evidence."""

    x_boundaries: int = 0
    z_boundaries: int = 0
    cell_count: int = 0
    valid_cell_count: int = 0


@dataclass(frozen=True, slots=True)
class StandableRegionResult:
    status: QueryStatus
    completion_region: GroundCompletionRegion | None = None
    dependencies: tuple[BlockPos, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()

    @property
    def position(self) -> tuple[float, float, float] | None:
        return None if self.completion_region is None else self.completion_region.reference_point


def _largest_valid_subrectangle(
    xs: tuple[float, ...],
    zs: tuple[float, ...],
    valid_cells: tuple[tuple[bool, ...], ...],
) -> tuple[float, float, float, float] | None:
    """Return the largest all-valid grid rectangle with a stable tie break."""
    nx, nz = len(xs) - 1, len(zs) - 1
    if (nx <= 0 or nz <= 0 or len(valid_cells) != nx
            or any(len(column) != nz for column in valid_cells)):
        return None
    prefix = [[0] * (nz + 1) for _ in range(nx + 1)]
    for x in range(nx):
        for z in range(nz):
            prefix[x + 1][z + 1] = (
                prefix[x][z + 1] + prefix[x + 1][z] - prefix[x][z]
                + int(valid_cells[x][z])
            )
    best: tuple[float, float, float, float, float] | None = None
    for x0 in range(nx):
        for x1 in range(x0 + 1, nx + 1):
            for z0 in range(nz):
                for z1 in range(z0 + 1, nz + 1):
                    count = (prefix[x1][z1] - prefix[x0][z1]
                             - prefix[x1][z0] + prefix[x0][z0])
                    if count != (x1 - x0) * (z1 - z0):
                        continue
                    rectangle = (xs[x0], zs[z0], xs[x1], zs[z1])
                    area = (rectangle[2] - rectangle[0]) * (rectangle[3] - rectangle[1])
                    key = (-area, *rectangle)
                    if best is None or key < best:
                        best = key
    return None if best is None else best[1:]


def standable_region_in_goal(world: WorldView, surface: SupportSurface, region: Aabb,
                             *, body_width: float = .6, body_height: float = 1.8,
                             minimum_support_fraction: float = .5,
                             connection_from: tuple[float, float, float] | None = None,
                             allowed_materials: frozenset[str] | None = None,
                             query_cache: WorldQueryCache | None = None,
                             diagnostics: StandableRegionDiagnostics | None = None,
                             ) -> StandableRegionResult:
    """Select one exact positive-area region from a unified geometry grid."""
    if query_cache is not None and type(query_cache) is not WorldQueryCache:
        raise ContractViolation("surface query cache must use WorldQueryCache")
    if query_cache is not None:
        query_cache.validate_for(world)
    if diagnostics is not None and type(diagnostics) is not StandableRegionDiagnostics:
        raise ContractViolation("standable region diagnostics must use the typed counter")
    if diagnostics is not None:
        diagnostics.x_boundaries = 0
        diagnostics.z_boundaries = 0
        diagnostics.cell_count = 0
        diagnostics.valid_cell_count = 0
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
    missing: set[BlockPos] = set()
    unsupported = False
    support_boxes: list[tuple[Aabb, BlockPos]] = []
    blockers: list[tuple[float, float, float, float, BlockPos, QueryStatus]] = []
    for owner in sorted(selected):
        fact = world.cell(owner) if query_cache is None else query_cache.cell(owner)
        if fact.knowledge is CellKnowledge.UNKNOWN:
            if unknown_shape_owner_is_fully_covered(
                    world, owner, query_cache=query_cache):
                continue
            missing.add(owner)
            # The bounded owner may reach one cell above itself.
            boxes = (Aabb(owner[0], owner[1], owner[2],
                          owner[0]+1, owner[1]+2, owner[2]+1),)
            blocker_status = QueryStatus.NEEDS_INFORMATION
        elif fact.knowledge is CellKnowledge.AIR:
            continue
        else:
            assert fact.block is not None
            if (allowed_materials is not None
                    and fact.block.material_key not in allowed_materials):
                return StandableRegionResult(
                    QueryStatus.UNSUPPORTED,
                    dependencies=tuple(sorted(set(surface.dependencies) | selected)),
                )
            if fact.block.fluid or fact.block.collision_kind == 'unsupported':
                if allowed_materials is not None:
                    return StandableRegionResult(
                        QueryStatus.UNSUPPORTED,
                        dependencies=tuple(sorted(set(surface.dependencies) | selected)),
                    )
                unsupported = True
                boxes = (Aabb(owner[0], owner[1], owner[2],
                              owner[0]+1, owner[1]+2, owner[2]+1),)
                blocker_status = QueryStatus.UNSUPPORTED
            else:
                boxes = fact.block.world_boxes(owner)
                blocker_status = QueryStatus.BLOCKED
        for box in boxes:
            if abs(box.max_y-y) <= .05:
                support_boxes.append((box, owner))
            if box.min_y >= y+body_height-_EPSILON or box.max_y <= y+_EPSILON:
                continue
            blocker = (box.min_x-half, box.min_z-half,
                       box.max_x+half, box.max_z+half, owner, blocker_status)
            if (blocker[0] < hx-_EPSILON and blocker[2] > lx+_EPSILON
                    and blocker[1] < hz-_EPSILON and blocker[3] > lz+_EPSILON):
                blockers.append(blocker)
    dependencies = set(surface.dependencies) | selected
    def rejected(status):
        return StandableRegionResult(status, dependencies=tuple(sorted(dependencies)),
                                     missing_cells=tuple(sorted(missing)))
    xs = {lx, hx}
    zs = {lz, hz}
    for min_x, min_z, max_x, max_z, _, _ in blockers:
        xs.update(value for value in (min_x, max_x) if lx < value < hx)
        zs.update(value for value in (min_z, max_z) if lz < value < hz)
    for box, _ in support_boxes:
        xs.update(value for edge in (box.min_x, box.max_x)
                  for value in (edge-half, edge+half) if lx < value < hx)
        zs.update(value for edge in (box.min_z, box.max_z)
                  for value in (edge-half, edge+half) if lz < value < hz)
    ordered_xs, ordered_zs = tuple(sorted(xs)), tuple(sorted(zs))
    if diagnostics is not None:
        diagnostics.x_boundaries = len(ordered_xs)
        diagnostics.z_boundaries = len(ordered_zs)
        diagnostics.cell_count = (len(ordered_xs)-1) * (len(ordered_zs)-1)

    threshold_cut = False
    clearance_at = {}
    support_at: dict[tuple[float, float], SupportResult] = {}
    valid_columns: list[tuple[bool, ...]] = []
    for x0, x1 in zip(ordered_xs, ordered_xs[1:]):
        column = []
        for z0, z1 in zip(ordered_zs, ordered_zs[1:]):
            center_x, center_z = (x0+x1)/2, (z0+z1)/2
            direct = tuple(item for item in blockers
                           if item[0]+_EPSILON < center_x < item[2]-_EPSILON
                           and item[1]+_EPSILON < center_z < item[3]-_EPSILON)
            direct_statuses = {item[5] for item in direct}
            if QueryStatus.NEEDS_INFORMATION in direct_statuses:
                missing.update(item[4] for item in direct
                               if item[5] is QueryStatus.NEEDS_INFORMATION)
            unsupported |= QueryStatus.UNSUPPORTED in direct_statuses
            center_body = Aabb(center_x-half, y, center_z-half,
                               center_x+half, y+body_height, center_z+half)
            clearance = sweep(center_body, (0., 0., 0.), world, query_cache=query_cache)
            clearance_at[(center_x, center_z)] = clearance
            missing.update(clearance.missing_cells)
            unsupported |= clearance.status is QueryStatus.UNSUPPORTED
            fractions = []
            corner_statuses = []
            for x in (x0, x1):
                for z in (z0, z1):
                    point = (x, z)
                    support = support_at.get(point)
                    if support is None:
                        body = Aabb(x-half, y, z-half, x+half, y+body_height, z+half)
                        support = query_support(body, world, query_cache=query_cache)
                        support_at[point] = support
                    missing.update(support.missing_cells)
                    status = support.status
                    if (status is QueryStatus.FEASIBLE and allowed_materials is not None
                            and not set(support.support_materials) <= allowed_materials):
                        status = QueryStatus.UNSUPPORTED
                    unsupported |= status is QueryStatus.UNSUPPORTED
                    corner_statuses.append(status)
                    fractions.append(support.support_fraction)
            support_known = all(status is QueryStatus.FEASIBLE for status in corner_statuses)
            supported = (support_known
                         and min(fractions)+_EPSILON >= minimum_support_fraction)
            if (support_known and min(fractions)+_EPSILON < minimum_support_fraction
                    <= max(fractions)+_EPSILON):
                threshold_cut = True
            column.append(not direct and clearance.status is QueryStatus.FEASIBLE and supported)
        valid_columns.append(tuple(column))
    valid_cells = tuple(valid_columns)
    if diagnostics is not None:
        diagnostics.valid_cell_count = sum(sum(column) for column in valid_cells)
    selected_rectangle = _largest_valid_subrectangle(ordered_xs, ordered_zs, valid_cells)
    if selected_rectangle is None:
        return rejected(QueryStatus.NEEDS_INFORMATION if missing else
                        QueryStatus.UNSUPPORTED if unsupported or threshold_cut
                        else QueryStatus.BLOCKED)
    lx, lz, hx, hz = selected_rectangle

    # Re-run exact queries only over the bound rectangle.  This prevents facts
    # from rejected candidate areas leaking into the execution contract.
    # ``surface_key`` below carries the surface identity.  Do not copy the
    # surface discovery dependencies: they cover rejected points as well.
    exact_dependencies: set[BlockPos] = set()
    selected_xs = tuple(value for value in ordered_xs if lx-_EPSILON <= value <= hx+_EPSILON)
    selected_zs = tuple(value for value in ordered_zs if lz-_EPSILON <= value <= hz+_EPSILON)
    exact_points = {(x, z) for x in selected_xs for z in selected_zs}
    exact_points.update(((x0+x1)/2, (z0+z1)/2)
                        for x0, x1 in zip(selected_xs, selected_xs[1:])
                        for z0, z1 in zip(selected_zs, selected_zs[1:]))
    for x, z in sorted(exact_points):
        body = Aabb(x-half, y, z-half, x+half, y+body_height, z+half)
        clearance = clearance_at.get((x, z))
        if clearance is None:
            clearance = sweep(
                body, (0., 0., 0.), world, query_cache=query_cache,
            )
        support = support_at.get((x, z))
        if support is None:
            support = query_support(body, world, query_cache=query_cache)
        exact_dependencies.update((*clearance.dependencies, *support.dependencies))
        missing.update((*clearance.missing_cells, *support.missing_cells))
        material_ok = (allowed_materials is None
                       or set(support.support_materials) <= allowed_materials)
        if (clearance.status is not QueryStatus.FEASIBLE
                or support.status is not QueryStatus.FEASIBLE
                or support.support_fraction+_EPSILON < minimum_support_fraction
                or not material_ok):
            exact_unsupported = (not material_ok or QueryStatus.UNSUPPORTED in
                                 (clearance.status, support.status))
            return rejected(QueryStatus.NEEDS_INFORMATION if missing else
                            QueryStatus.UNSUPPORTED if exact_unsupported else QueryStatus.BLOCKED)
    # Full rectangle envelope records clearance and support owners between
    # vertices, including facts which select its exact shape boundaries.
    exact_envelope = Aabb(lx-half, y, lz-half, hx+half, y+body_height, hz+half)
    exact_dependencies.update(required_cells_for_sweep(exact_envelope, (0., 0., 0.)))
    # Keep only owners which define an accepted rectangle boundary.
    for min_x, min_z, max_x, max_z, owner, _ in blockers:
        touches_x = ((abs(lx-min_x) <= _EPSILON or abs(lx-max_x) <= _EPSILON
                      or abs(hx-min_x) <= _EPSILON or abs(hx-max_x) <= _EPSILON)
                     and min_z < hz-_EPSILON and max_z > lz+_EPSILON)
        touches_z = ((abs(lz-min_z) <= _EPSILON or abs(lz-max_z) <= _EPSILON
                      or abs(hz-min_z) <= _EPSILON or abs(hz-max_z) <= _EPSILON)
                     and min_x < hx-_EPSILON and max_x > lx+_EPSILON)
        if (touches_x or touches_z) and world.cell(owner).knowledge is CellKnowledge.BLOCK:
            exact_dependencies.add(owner)
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


def validate_standable_region(
    world: WorldView,
    surface: SupportSurface,
    goal_region: Aabb,
    expected: GroundCompletionRegion,
    *,
    body_width: float = .6,
    body_height: float = 1.8,
    minimum_support_fraction: float = .5,
    allowed_materials: frozenset[str] | None = None,
    query_cache: WorldQueryCache | None = None,
) -> StandableRegionResult:
    """Validate one bound completion region without selecting a replacement."""
    if (type(world) is not WorldView or type(surface) is not SupportSurface
            or type(goal_region) is not Aabb
            or type(expected) is not GroundCompletionRegion):
        raise ContractViolation("standable region validation requires typed inputs")
    if query_cache is not None:
        query_cache.validate_for(world)
    if (not math.isfinite(body_width) or not math.isfinite(body_height)
            or body_width <= 0 or body_height <= 0
            or not 0 < minimum_support_fraction <= 1):
        raise ContractViolation("standable region validation limits are invalid")
    if allowed_materials is not None and type(allowed_materials) is not frozenset:
        raise ContractViolation("standable region materials must be immutable")

    bounds = expected.bounds
    node = surface.node_id
    identity = (node.column_x, node.column_z, node.vertical_band, node.surface_index)
    if (expected.surface_identity != identity
            or abs(expected.support_height - surface.position[1]) > _EPSILON
            or expected.reference_point[1] != expected.support_height):
        return StandableRegionResult(QueryStatus.BLOCKED)
    goal_values, bound_values = goal_region.as_tuple(), bounds.as_tuple()
    if (any(goal_values[index] > bound_values[index] + _EPSILON for index in range(3))
            or any(bound_values[index] > goal_values[index] + _EPSILON
                   for index in range(3, 6))
            or bounds.max_x <= bounds.min_x + _EPSILON
            or bounds.max_z <= bounds.min_z + _EPSILON
            or not expected.contains(expected.reference_point)):
        return StandableRegionResult(QueryStatus.BLOCKED)
    if (bounds.min_x < surface.region.min_x - _EPSILON
            or bounds.max_x > surface.region.max_x + _EPSILON
            or bounds.min_z < surface.region.min_z - _EPSILON
            or bounds.max_z > surface.region.max_z + _EPSILON):
        return StandableRegionResult(QueryStatus.BLOCKED)

    y, half = expected.support_height, body_width / 2.0
    envelope = Aabb(
        bounds.min_x-half, y, bounds.min_z-half,
        bounds.max_x+half, y+body_height, bounds.max_z+half,
    )
    owners = set(required_cells_for_sweep(envelope, (0., 0., 0.)))
    xs, zs = {bounds.min_x, bounds.max_x}, {bounds.min_z, bounds.max_z}
    for owner in sorted(owners):
        fact = world.cell(owner) if query_cache is None else query_cache.cell(owner)
        if fact.knowledge is CellKnowledge.UNKNOWN:
            if unknown_shape_owner_is_fully_covered(
                    world, owner, query_cache=query_cache):
                continue
            boxes = (Aabb(owner[0], owner[1], owner[2],
                          owner[0]+1, owner[1]+2, owner[2]+1),)
        elif fact.knowledge is CellKnowledge.AIR:
            continue
        else:
            assert fact.block is not None
            boxes = fact.block.world_boxes(owner)
        for box in boxes:
            if abs(box.max_y-y) <= .05:
                xs.update(value for edge in (box.min_x, box.max_x)
                          for value in (edge-half, edge+half)
                          if bounds.min_x < value < bounds.max_x)
                zs.update(value for edge in (box.min_z, box.max_z)
                          for value in (edge-half, edge+half)
                          if bounds.min_z < value < bounds.max_z)
            if box.min_y < y+body_height-_EPSILON and box.max_y > y+_EPSILON:
                xs.update(value for value in (box.min_x-half, box.max_x+half)
                          if bounds.min_x < value < bounds.max_x)
                zs.update(value for value in (box.min_z-half, box.max_z+half)
                          if bounds.min_z < value < bounds.max_z)

    ordered_xs, ordered_zs = tuple(sorted(xs)), tuple(sorted(zs))
    points = {
        (x, z) for x in ordered_xs for z in ordered_zs
    }
    points.update(
        ((x0+x1)/2.0, (z0+z1)/2.0)
        for x0, x1 in zip(ordered_xs, ordered_xs[1:])
        for z0, z1 in zip(ordered_zs, ordered_zs[1:])
    )
    dependencies: set[BlockPos] = set()
    missing: set[BlockPos] = set()
    blocked = False
    unsupported = False
    for x, z in sorted(points):
        body = Aabb(x-half, y, z-half, x+half, y+body_height, z+half)
        clearance = sweep(body, (0., 0., 0.), world, query_cache=query_cache)
        support = query_support(body, world, query_cache=query_cache)
        dependencies.update((*clearance.dependencies, *support.dependencies))
        missing.update((*clearance.missing_cells, *support.missing_cells))
        material_ok = (
            allowed_materials is None
            or set(support.support_materials) <= allowed_materials
        )
        unsupported |= (
            not material_ok
            or clearance.status is QueryStatus.UNSUPPORTED
            or support.status is QueryStatus.UNSUPPORTED
        )
        blocked |= (
            clearance.status is QueryStatus.BLOCKED
            or support.status is QueryStatus.BLOCKED
            or (support.status is QueryStatus.FEASIBLE
                and support.support_fraction + _EPSILON < minimum_support_fraction)
        )
    dependencies.update(required_cells_for_sweep(envelope, (0., 0., 0.)))
    dependencies_tuple = tuple(sorted(dependencies))
    if missing:
        return StandableRegionResult(
            QueryStatus.NEEDS_INFORMATION,
            dependencies=dependencies_tuple,
            missing_cells=tuple(sorted(missing)),
        )
    if unsupported:
        return StandableRegionResult(
            QueryStatus.UNSUPPORTED,
            dependencies=dependencies_tuple,
        )
    if blocked:
        return StandableRegionResult(
            QueryStatus.BLOCKED,
            dependencies=dependencies_tuple,
        )
    refreshed = GroundCompletionRegion(
        bounds,
        expected.reference_point,
        expected.support_height,
        expected.surface_identity,
        dependencies_tuple,
    )
    return StandableRegionResult(
        QueryStatus.FEASIBLE,
        refreshed,
        dependencies_tuple,
    )


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
    query_cache: WorldQueryCache | None = None,
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
    if query_cache is not None and type(query_cache) is not WorldQueryCache:
        raise ContractViolation("surface query cache must use WorldQueryCache")
    if query_cache is not None:
        query_cache.validate_for(world)

    owner_cells = _candidate_owner_cells(
        column_x, column_z, minimum_feet_y, maximum_feet_y,
    )
    missing = tuple(
        position for position in owner_cells
        if ((world.cell(position) if query_cache is None
             else query_cache.cell(position)).knowledge is CellKnowledge.UNKNOWN
            and not unknown_shape_owner_is_fully_covered(
                world, position, query_cache=query_cache,
            ))
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
        fact = world.cell(owner) if query_cache is None else query_cache.cell(owner)
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
                clearance = sweep(
                    body, (0.0, 0.0, 0.0), world,
                    query_cache=query_cache,
                )
                support = query_support(body, world, query_cache=query_cache)
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
