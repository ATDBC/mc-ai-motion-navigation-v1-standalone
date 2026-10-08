"""Immutable proof metadata for bounded active-route revalidation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.async_work import AsyncWorkIdentity
from mc2p.motion_nav.block_motion_traits import unsupported_motion_cells
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.support_surfaces import (
    SupportSurface,
    query_standable_connection,
    query_support_surfaces,
    validate_standable_region,
)
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockPos,
    CellKnowledge,
    WorldQueryCache,
    WorldView,
)


def _sorted_positions(value: tuple[BlockPos, ...], name: str) -> None:
    if (type(value) is not tuple
            or value != tuple(sorted(set(value)))
            or any(type(position) is not tuple or len(position) != 3
                   or any(type(axis) is not int for axis in position)
                   for position in value)):
        raise ContractViolation(f"{name} must be sorted unique block positions")


def _point(value: tuple[float, float, float], name: str) -> None:
    if (type(value) is not tuple or len(value) != 3
            or any(type(axis) not in (int, float)
                   or not math.isfinite(float(axis)) for axis in value)):
        raise ContractViolation(f"{name} must be a finite three-axis point")


class WalkValidationQueryKind(StrEnum):
    SURFACE_EDGE = "surface_edge"
    STANDABLE_CONNECTION = "standable_connection"
    STANDABLE_REGION = "standable_region"


class DependencyOwnerKind(StrEnum):
    WALK_LEG = "walk_leg"
    INITIAL_CONNECTION = "initial_connection"
    STRICT_ACTION = "strict_action"
    NON_RECIPE = "non_recipe"
    COMPLETION_REGION = "completion_region"


class ActiveRouteValidationDisposition(StrEnum):
    UNAFFECTED = "unaffected"
    CONTINUE = "continue"
    STOP = "stop"


class ActiveRouteValidationReason(StrEnum):
    NO_INTERSECTION = "no_intersection"
    REVALIDATED = "revalidated"
    PLAN_UNAVAILABLE = "plan_unavailable"
    STRICT_OWNER_CHANGED = "strict_owner_changed"
    NON_RECIPE_OWNER_CHANGED = "non_recipe_owner_changed"
    OWNER_REFERENCE_INVALID = "owner_reference_invalid"
    QUERY_LIMIT_EXCEEDED = "query_limit_exceeded"
    NEEDS_INFORMATION = "needs_information"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    CAPABILITY_IDENTITY_CHANGED = "capability_identity_changed"
    ROUTE_IDENTITY_CHANGED = "route_identity_changed"
    PROGRESS_EVIDENCE_MISSING = "progress_evidence_missing"
    PROGRESS_EVIDENCE_STALE = "progress_evidence_stale"
    PROGRESS_IDENTITY_MISMATCH = "progress_identity_mismatch"
    PROGRESS_ACTION_INDEX_INVALID = "progress_action_index_invalid"
    PROGRESS_REWOUND = "progress_rewound"
    PROGRESS_RECORDED = "progress_recorded"


@dataclass(frozen=True, slots=True)
class RouteProgressEvidence:
    action_index: int
    fixed_route_id: str
    progress_blocks: float
    observation_sequence_id: int

    def __post_init__(self) -> None:
        if type(self.action_index) is not int or self.action_index < 0:
            raise ContractViolation("route progress action index must be nonnegative")
        require_identifier(self.fixed_route_id, "route progress fixed route")
        if (type(self.progress_blocks) not in (int, float)
                or not math.isfinite(float(self.progress_blocks))
                or self.progress_blocks < 0):
            raise ContractViolation("route progress must be finite and nonnegative")
        if (type(self.observation_sequence_id) is not int
                or self.observation_sequence_id < 0):
            raise ContractViolation(
                "route progress observation sequence must be nonnegative"
            )


@dataclass(frozen=True, slots=True)
class ActiveRouteValidationIdentity:
    world_session: str
    route_id: str
    route_revision: int
    source_request_id: str
    goal_id: str
    goal_revision: int
    planning_generation: int
    work_identity: AsyncWorkIdentity | None
    action_index: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.world_session, "validation world session"),
            (self.route_id, "validation route"),
            (self.source_request_id, "validation request"),
            (self.goal_id, "validation goal"),
        ):
            require_identifier(value, name)
        for value, name in (
            (self.route_revision, "validation route revision"),
            (self.goal_revision, "validation goal revision"),
            (self.planning_generation, "validation planning generation"),
            (self.action_index, "validation action index"),
        ):
            if type(value) is not int or value < 0:
                raise ContractViolation(f"{name} must be nonnegative")
        if (self.work_identity is not None
                and type(self.work_identity) is not AsyncWorkIdentity):
            raise ContractViolation("validation work identity must be typed")


@dataclass(frozen=True, slots=True)
class ActiveRouteValidation:
    disposition: ActiveRouteValidationDisposition
    reason: ActiveRouteValidationReason
    identity: ActiveRouteValidationIdentity
    affected_cells: tuple[BlockPos, ...]
    refreshed_dependencies: tuple[BlockPos, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()
    queries_used: int = 0

    def __post_init__(self) -> None:
        if (type(self.disposition) is not ActiveRouteValidationDisposition
                or type(self.reason) is not ActiveRouteValidationReason
                or type(self.identity) is not ActiveRouteValidationIdentity):
            raise ContractViolation("active route validation must be typed")
        _sorted_positions(self.affected_cells, "affected validation cells")
        _sorted_positions(
            self.refreshed_dependencies, "refreshed validation dependencies",
        )
        _sorted_positions(self.missing_cells, "missing validation cells")
        if type(self.queries_used) is not int or self.queries_used < 0:
            raise ContractViolation("validation query count must be nonnegative")


@dataclass(slots=True)
class RouteValidationBudget:
    maximum_queries: int = 4
    queries_used: int = 0

    def __post_init__(self) -> None:
        if (type(self.maximum_queries) is not int or self.maximum_queries < 0
                or type(self.queries_used) is not int
                or not 0 <= self.queries_used <= self.maximum_queries):
            raise ContractViolation("route validation query budget is invalid")

    def consume(self) -> bool:
        if self.queries_used >= self.maximum_queries:
            return False
        self.queries_used += 1
        return True


@dataclass(frozen=True, slots=True)
class GroundCapabilityIdentity:
    profile_id: str
    environment_id: str
    ground_model_id: str | None
    support_materials: frozenset[str]
    catalog_environment_id: str | None
    minecraft_version: str | None

    @classmethod
    def from_profile(cls, profile: GroundMotionProfile) -> GroundCapabilityIdentity:
        if type(profile) is not GroundMotionProfile:
            raise ContractViolation("ground capability identity requires a profile")
        catalog = profile.motion_catalog
        return cls(
            profile.profile_id,
            profile.environment_id,
            profile.ground_model_id,
            profile.support_materials,
            None if catalog is None else catalog.environment_id,
            None if catalog is None else catalog.minecraft_version,
        )

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "ground capability profile")
        require_identifier(self.environment_id, "ground capability environment")
        if self.ground_model_id is not None:
            require_identifier(self.ground_model_id, "ground capability model")
        if type(self.support_materials) is not frozenset:
            raise ContractViolation("ground capability materials must be immutable")
        for material in self.support_materials:
            require_identifier(material, "ground capability material")
        for value, name in (
            (self.catalog_environment_id, "ground capability catalog"),
            (self.minecraft_version, "ground capability Minecraft version"),
        ):
            if value is not None:
                require_identifier(value, name)
        if ((self.ground_model_id is None)
                != (self.catalog_environment_id is None)
                or (self.ground_model_id is None)
                != (self.minecraft_version is None)):
            raise ContractViolation("ground capability catalog identity is incomplete")


@dataclass(frozen=True, slots=True)
class SurfaceEdgeQueryArgs:
    start_surface: SupportSurface
    end_surface: SupportSurface
    body_height_blocks: float

    def __post_init__(self) -> None:
        if (type(self.start_surface) is not SupportSurface
                or type(self.end_surface) is not SupportSurface):
            raise ContractViolation("surface edge recipe requires endpoint surfaces")
        if (type(self.body_height_blocks) not in (int, float)
                or not math.isfinite(float(self.body_height_blocks))
                or self.body_height_blocks <= 0):
            raise ContractViolation("surface edge body height must be positive")


@dataclass(frozen=True, slots=True)
class StandableConnectionQueryArgs:
    surface: SupportSurface
    position: tuple[float, float, float]
    connection_from: tuple[float, float, float]
    body_width_blocks: float
    body_height_blocks: float

    def __post_init__(self) -> None:
        if type(self.surface) is not SupportSurface:
            raise ContractViolation("standable connection recipe requires a surface")
        _point(self.position, "standable connection endpoint")
        _point(self.connection_from, "standable connection start")
        for value, name in (
            (self.body_width_blocks, "standable connection body width"),
            (self.body_height_blocks, "standable connection body height"),
        ):
            if (type(value) not in (int, float)
                    or not math.isfinite(float(value)) or value <= 0):
                raise ContractViolation(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class StandableRegionQueryArgs:
    surface: SupportSurface
    goal_region: Aabb
    connection_from: tuple[float, float, float]
    expected_region: GroundCompletionRegion
    body_width_blocks: float = .6
    body_height_blocks: float = 1.8
    minimum_support_fraction: float = .5

    def __post_init__(self) -> None:
        if (type(self.surface) is not SupportSurface or type(self.goal_region) is not Aabb
                or type(self.expected_region) is not GroundCompletionRegion):
            raise ContractViolation("standable region recipe requires typed geometry")
        _point(self.connection_from, "standable region incoming point")
        if (any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in
                (self.body_width_blocks, self.body_height_blocks, self.minimum_support_fraction))
                or self.minimum_support_fraction > 1):
            raise ContractViolation("standable region recipe body/support limits are invalid")
        expected = self.expected_region
        if (not all(a <= b for a,b in zip(self.goal_region.as_tuple()[:3], expected.bounds.as_tuple()[:3]))
                or not all(a <= b for a,b in zip(expected.bounds.as_tuple()[3:], self.goal_region.as_tuple()[3:]))):
            raise ContractViolation("standable region recipe enlarges the goal")


@dataclass(frozen=True, slots=True)
class WalkValidationRecipe:
    recipe_id: str
    query_kind: WalkValidationQueryKind
    surface_edge: SurfaceEdgeQueryArgs | None
    standable_connection: StandableConnectionQueryArgs | None
    ground_profile: GroundMotionProfile
    capability: GroundCapabilityIdentity
    dependencies: tuple[BlockPos, ...]
    standable_region: StandableRegionQueryArgs | None = None

    def __post_init__(self) -> None:
        require_identifier(self.recipe_id, "walk validation recipe")
        if type(self.query_kind) is not WalkValidationQueryKind:
            raise ContractViolation("walk validation query kind must be typed")
        if type(self.ground_profile) is not GroundMotionProfile:
            raise ContractViolation("walk validation recipe requires its ground profile")
        if (type(self.capability) is not GroundCapabilityIdentity
                or self.capability != GroundCapabilityIdentity.from_profile(
                    self.ground_profile
                )):
            raise ContractViolation("walk validation capability differs from its profile")
        _sorted_positions(self.dependencies, "walk validation dependencies")
        surface = type(self.surface_edge) is SurfaceEdgeQueryArgs
        standable = type(self.standable_connection) is StandableConnectionQueryArgs
        region = type(self.standable_region) is StandableRegionQueryArgs
        if ((self.query_kind is WalkValidationQueryKind.SURFACE_EDGE
             and not (surface and not standable and not region))
                or (self.query_kind is WalkValidationQueryKind.STANDABLE_CONNECTION
                    and not (standable and not surface and not region))
                or (self.query_kind is WalkValidationQueryKind.STANDABLE_REGION
                    and not (region and not surface and not standable))):
            raise ContractViolation("walk validation query payload does not match its kind")


@dataclass(frozen=True, slots=True)
class DependencyOwner:
    owner_id: str
    kind: DependencyOwnerKind
    action_index: int
    fixed_route_id: str | None
    recipe_ref: str | None

    def __post_init__(self) -> None:
        require_identifier(self.owner_id, "dependency owner")
        if type(self.kind) is not DependencyOwnerKind:
            raise ContractViolation("dependency owner kind must be typed")
        if type(self.action_index) is not int or self.action_index < 0:
            raise ContractViolation("dependency owner action index must be nonnegative")
        if self.fixed_route_id is not None:
            require_identifier(self.fixed_route_id, "dependency owner fixed route")
        if self.recipe_ref is not None:
            require_identifier(self.recipe_ref, "dependency owner recipe reference")
        if self.kind in (
            DependencyOwnerKind.STRICT_ACTION,
            DependencyOwnerKind.NON_RECIPE,
        ) and self.recipe_ref is not None:
            raise ContractViolation("strict and non-recipe owners cannot reference recipes")
        if (self.kind is DependencyOwnerKind.WALK_LEG
                and (self.fixed_route_id is None or self.recipe_ref is None)):
            raise ContractViolation("walk leg owner requires route and recipe references")
        if (self.kind is DependencyOwnerKind.COMPLETION_REGION
                and self.recipe_ref is None):
            raise ContractViolation("completion owner requires a recipe reference")


@dataclass(frozen=True, slots=True)
class InitialConnectionValidation:
    owner_ref: str
    retire_after_progress_blocks: float

    def __post_init__(self) -> None:
        require_identifier(self.owner_ref, "initial connection owner reference")
        if (type(self.retire_after_progress_blocks) not in (int, float)
                or not math.isfinite(float(self.retire_after_progress_blocks))
                or self.retire_after_progress_blocks <= 0):
            raise ContractViolation("initial connection retirement must be positive")


@dataclass(frozen=True, slots=True)
class WalkLegValidationBinding:
    owner_ref: str
    recipe_ref: str
    start_point_index: int
    end_point_index: int
    start_progress_blocks: float
    end_progress_blocks: float

    def __post_init__(self) -> None:
        require_identifier(self.owner_ref, "walk leg owner reference")
        require_identifier(self.recipe_ref, "walk leg recipe reference")
        if (type(self.start_point_index) is not int
                or type(self.end_point_index) is not int
                or self.start_point_index < 0
                or self.end_point_index != self.start_point_index + 1):
            raise ContractViolation("walk leg point indices must be consecutive")
        for value, name in (
            (self.start_progress_blocks, "walk leg start progress"),
            (self.end_progress_blocks, "walk leg end progress"),
        ):
            if (type(value) not in (int, float)
                    or not math.isfinite(float(value)) or value < 0):
                raise ContractViolation(f"{name} must be finite and nonnegative")
        if self.end_progress_blocks <= self.start_progress_blocks:
            raise ContractViolation("walk leg progress must advance")


@dataclass(frozen=True, slots=True)
class WalkActionValidationPlan:
    action_index: int
    fixed_route_id: str
    legs: tuple[WalkLegValidationBinding, ...]

    def __post_init__(self) -> None:
        if type(self.action_index) is not int or self.action_index < 0:
            raise ContractViolation("walk action index must be nonnegative")
        require_identifier(self.fixed_route_id, "walk action fixed route")
        if (type(self.legs) is not tuple
                or any(type(leg) is not WalkLegValidationBinding
                       for leg in self.legs)):
            raise ContractViolation("walk action legs must be typed and immutable")
        if tuple(leg.start_point_index for leg in self.legs) != tuple(sorted(
                leg.start_point_index for leg in self.legs)):
            raise ContractViolation("walk action legs must follow route order")


@dataclass(frozen=True, slots=True)
class DependencyProvenance:
    position: BlockPos
    owner_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _sorted_positions((self.position,), "dependency provenance position")
        if (type(self.owner_refs) is not tuple or not self.owner_refs
                or self.owner_refs != tuple(sorted(set(self.owner_refs)))):
            raise ContractViolation("dependency owner references must be sorted and unique")
        for owner in self.owner_refs:
            require_identifier(owner, "dependency owner reference")


@dataclass(frozen=True, slots=True)
class ActiveRouteValidationPlan:
    action_plans: tuple[WalkActionValidationPlan, ...]
    recipes: tuple[WalkValidationRecipe, ...]
    owners: tuple[DependencyOwner, ...]
    initial_connection: InitialConnectionValidation | None
    dependency_provenance: tuple[DependencyProvenance, ...]

    def __post_init__(self) -> None:
        for values, expected, name in (
            (self.action_plans, WalkActionValidationPlan, "walk action plans"),
            (self.recipes, WalkValidationRecipe, "walk validation recipes"),
            (self.owners, DependencyOwner, "dependency owners"),
            (self.dependency_provenance, DependencyProvenance,
             "dependency provenance"),
        ):
            if type(values) is not tuple or any(type(value) is not expected
                                                for value in values):
                raise ContractViolation(f"{name} must be typed and immutable")
        if (self.initial_connection is not None
                and type(self.initial_connection) is not InitialConnectionValidation):
            raise ContractViolation("initial connection validation must be typed")
        if tuple(plan.action_index for plan in self.action_plans) != tuple(sorted(
                {plan.action_index for plan in self.action_plans})):
            raise ContractViolation("walk action plans must be sorted and unique")
        if tuple(recipe.recipe_id for recipe in self.recipes) != tuple(sorted(
                {recipe.recipe_id for recipe in self.recipes})):
            raise ContractViolation("walk validation recipes must be sorted and unique")
        if tuple(owner.owner_id for owner in self.owners) != tuple(sorted(
                {owner.owner_id for owner in self.owners})):
            raise ContractViolation("dependency owners must be sorted and unique")
        if tuple(item.position for item in self.dependency_provenance) != tuple(sorted(
                {item.position for item in self.dependency_provenance})):
            raise ContractViolation("dependency provenance must be sorted and unique")

        owners = {owner.owner_id: owner for owner in self.owners}
        recipes = {recipe.recipe_id: recipe for recipe in self.recipes}
        referenced_recipes: list[str] = []
        for owner in self.owners:
            if owner.recipe_ref is not None:
                if owner.recipe_ref not in recipes:
                    raise ContractViolation("dependency owner recipe reference is missing")
                referenced_recipes.append(owner.recipe_ref)
                is_region = recipes[owner.recipe_ref].query_kind is WalkValidationQueryKind.STANDABLE_REGION
                if is_region != (owner.kind is DependencyOwnerKind.COMPLETION_REGION):
                    raise ContractViolation("completion recipe requires exactly its typed owner")
        if tuple(sorted(referenced_recipes)) != tuple(sorted(recipes)):
            raise ContractViolation("walk validation recipes require one owning reference")

        binding_owner_refs: list[str] = []
        binding_recipe_refs: list[str] = []
        for action in self.action_plans:
            for leg in action.legs:
                owner = owners.get(leg.owner_ref)
                if (owner is None or owner.kind is not DependencyOwnerKind.WALK_LEG
                        or owner.action_index != action.action_index
                        or owner.fixed_route_id != action.fixed_route_id
                        or owner.recipe_ref != leg.recipe_ref):
                    raise ContractViolation("walk leg binding differs from its owner")
                binding_owner_refs.append(owner.owner_id)
                binding_recipe_refs.append(leg.recipe_ref)
        expected_walk_owners = tuple(
            owner for owner in self.owners
            if owner.kind is DependencyOwnerKind.WALK_LEG
        )
        if (tuple(sorted(binding_owner_refs))
                != tuple(owner.owner_id for owner in expected_walk_owners)
                or tuple(sorted(binding_recipe_refs))
                != tuple(sorted(owner.recipe_ref for owner in expected_walk_owners))
                or len(binding_owner_refs) != len(set(binding_owner_refs))
                or len(binding_recipe_refs) != len(set(binding_recipe_refs))):
            raise ContractViolation(
                "walk leg owner, binding, and recipe must be one-to-one"
            )

        initial_owners = tuple(
            owner for owner in self.owners
            if owner.kind is DependencyOwnerKind.INITIAL_CONNECTION
        )
        if self.initial_connection is not None:
            owner = owners.get(self.initial_connection.owner_ref)
            if (len(initial_owners) != 1 or owner is None
                    or owner.kind is not DependencyOwnerKind.INITIAL_CONNECTION):
                raise ContractViolation("initial connection owner is missing or mistyped")
        elif initial_owners:
            raise ContractViolation("initial connection owner lacks retirement metadata")

        provenance_by_owner: dict[str, set[BlockPos]] = {
            owner_id: set() for owner_id in owners
        }
        for item in self.dependency_provenance:
            for owner_ref in item.owner_refs:
                if owner_ref not in owners:
                    raise ContractViolation("dependency provenance owner is missing")
                provenance_by_owner[owner_ref].add(item.position)
        if any(not positions for positions in provenance_by_owner.values()):
            raise ContractViolation("every dependency owner must own provenance")
        for owner in self.owners:
            if owner.recipe_ref is not None:
                expected = set(recipes[owner.recipe_ref].dependencies)
                if expected != provenance_by_owner[owner.owner_id]:
                    raise ContractViolation(
                        "recipe owner provenance differs from recipe dependencies"
                    )

    def owner(self, owner_ref: str) -> DependencyOwner:
        require_identifier(owner_ref, "dependency owner reference")
        for owner in self.owners:
            if owner.owner_id == owner_ref:
                return owner
        raise ContractViolation("dependency owner reference is missing")

    def recipe(self, recipe_ref: str | None) -> WalkValidationRecipe:
        if recipe_ref is None:
            raise ContractViolation("dependency owner has no validation recipe")
        require_identifier(recipe_ref, "walk validation recipe reference")
        for recipe in self.recipes:
            if recipe.recipe_id == recipe_ref:
                return recipe
        raise ContractViolation("walk validation recipe reference is missing")

    def dependencies_for_owner(self, owner_ref: str) -> tuple[BlockPos, ...]:
        self.owner(owner_ref)
        return tuple(
            item.position for item in self.dependency_provenance
            if owner_ref in item.owner_refs
        )


def query_surface_walk_edge(
    world: WorldView,
    start: SupportSurface,
    end: SupportSurface,
    *,
    body_height_blocks: float = 1.8,
    query_cache: WorldQueryCache | None = None,
) -> tuple[QueryStatus, tuple[BlockPos, ...]]:
    """Replay the existing same-height edge proof with its four samples."""
    if abs(end.position[1] - start.position[1]) > 1.0e-6:
        return QueryStatus.UNSUPPORTED, tuple(sorted(
            set(start.dependencies) | set(end.dependencies)
        ))
    body = Aabb(
        start.position[0] - .3, start.position[1], start.position[2] - .3,
        start.position[0] + .3,
        start.position[1] + body_height_blocks,
        start.position[2] + .3,
    )
    dx = end.position[0] - start.position[0]
    dz = end.position[2] - start.position[2]
    movement = sweep(
        body, (dx, 0.0, dz), world, query_cache=query_cache,
    )
    dependencies = (
        set(start.dependencies) | set(end.dependencies)
        | set(movement.dependencies)
    )
    if movement.status is not QueryStatus.FEASIBLE:
        return movement.status, tuple(sorted(dependencies))
    for fraction in (.25, .5, .75, 1.0):
        support = query_support(
            body.moved(dx * fraction, 0.0, dz * fraction),
            world,
            query_cache=query_cache,
        )
        dependencies.update(support.dependencies)
        if support.status is not QueryStatus.FEASIBLE:
            return support.status, tuple(sorted(dependencies))
        if support.support_fraction < .5:
            return QueryStatus.BLOCKED, tuple(sorted(dependencies))
    return QueryStatus.FEASIBLE, tuple(sorted(dependencies))


def ground_profile_allows_dependency_blocks(
    profile: GroundMotionProfile,
    world: WorldView,
    dependencies: tuple[BlockPos, ...],
    query_cache: WorldQueryCache | None = None,
) -> bool:
    """Apply the shared material capability rules to known dependency blocks."""
    if profile.support_materials:
        for position in dependencies:
            fact = (
                world.cell(position)
                if query_cache is None else query_cache.cell(position)
            )
            if (fact.knowledge is CellKnowledge.BLOCK
                    and fact.block is not None
                    and fact.block.material_key not in profile.support_materials):
                return False
    if profile.motion_catalog is not None and profile.ground_model_id is not None:
        return not unsupported_motion_cells(
            profile.motion_catalog,
            world,
            dependencies,
            profile.ground_model_id,
            query_cache=query_cache,
        )
    return True


def replay_walk_validation_recipe(
    recipe: WalkValidationRecipe,
    world: WorldView,
    *,
    query_cache: WorldQueryCache | None = None,
) -> tuple[QueryStatus, tuple[BlockPos, ...]]:
    """Dispatch one recipe without merging the two geometry proofs."""
    if type(recipe) is not WalkValidationRecipe or type(world) is not WorldView:
        raise ContractViolation("walk validation replay requires recipe and world")
    cache = query_cache or WorldQueryCache(world)
    if type(cache) is not WorldQueryCache or cache.world is not world:
        raise ContractViolation("walk validation cache belongs to another world")
    if recipe.query_kind is WalkValidationQueryKind.SURFACE_EDGE:
        assert recipe.surface_edge is not None
        args = recipe.surface_edge
        status, dependencies = query_surface_walk_edge(
            world,
            args.start_surface,
            args.end_surface,
            body_height_blocks=args.body_height_blocks,
            query_cache=cache,
        )
    elif recipe.query_kind is WalkValidationQueryKind.STANDABLE_CONNECTION:
        assert recipe.standable_connection is not None
        args = recipe.standable_connection
        result = query_standable_connection(
            world,
            args.surface,
            args.position,
            args.connection_from,
            body_width=args.body_width_blocks,
            body_height=args.body_height_blocks,
            query_cache=cache,
        )
        status, dependencies = result.status, result.dependencies
    else:
        assert recipe.standable_region is not None
        args = recipe.standable_region
        node = args.surface.node_id
        current = query_support_surfaces(world, node.column_x, node.column_z,
            args.expected_region.support_height, args.expected_region.support_height,
            body_width=args.body_width_blocks, body_height=args.body_height_blocks,
            minimum_support_fraction=args.minimum_support_fraction)
        surface = next((s for s in current.surfaces if s.node_id == node), None)
        if current.status is not QueryStatus.FEASIBLE or surface is None:
            return (current.status if current.status is not QueryStatus.FEASIBLE else QueryStatus.BLOCKED,
                    current.dependencies)
        result = validate_standable_region(
            world, surface, args.goal_region, args.expected_region,
            body_width=args.body_width_blocks,
            body_height=args.body_height_blocks, minimum_support_fraction=args.minimum_support_fraction,
            allowed_materials=recipe.ground_profile.support_materials, query_cache=cache)
        status, dependencies = result.status, result.dependencies
    if (status is QueryStatus.FEASIBLE
            and not ground_profile_allows_dependency_blocks(
                recipe.ground_profile, world, dependencies, cache
            )):
        status = QueryStatus.UNSUPPORTED
    return status, dependencies
