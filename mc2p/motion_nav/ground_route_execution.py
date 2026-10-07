"""Immutable route-progress permissions for ordinary ground execution."""
from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.world_model import Aabb, BlockPos


@dataclass(frozen=True, slots=True)
class GroundCompletionRegion:
    bounds: Aabb
    reference_point: tuple[float, float, float]
    support_height: float
    surface_identity: tuple[int, int, int, int]
    dependencies: tuple[BlockPos, ...]

    def __post_init__(self) -> None:
        if type(self.bounds) is not Aabb:
            raise ContractViolation("ground completion bounds must be typed")
        if (type(self.reference_point) is not tuple or len(self.reference_point) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       for v in self.reference_point)
                or not self.contains(self.reference_point)):
            raise ContractViolation("ground completion reference must be inside bounds")
        if (type(self.support_height) not in (int, float)
                or not math.isfinite(self.support_height)
                or self.reference_point[1] != self.support_height):
            raise ContractViolation("ground completion support height is invalid")
        if (type(self.surface_identity) is not tuple or len(self.surface_identity) != 4
                or any(type(v) is not int for v in self.surface_identity)):
            raise ContractViolation("ground completion surface identity must be typed")
        _validate_dependencies(self.dependencies)

    def contains(self, position: tuple[float, float, float]) -> bool:
        return all(lo <= v <= hi for lo, v, hi in
                   zip(self.bounds.as_tuple()[:3], position, self.bounds.as_tuple()[3:]))


def _validate_dependencies(dependencies: tuple[BlockPos, ...]) -> None:
    if (type(dependencies) is not tuple
            or any(type(p) is not tuple or len(p) != 3
                   or any(type(v) is not int for v in p) for p in dependencies)
            or dependencies != tuple(sorted(set(dependencies)))):
        raise ContractViolation("route execution dependencies must be sorted typed cells")


class GroundRouteCapability(StrEnum):
    SNEAK_EDGE_GUARD = "sneak_edge_guard"


class GroundRouteGuardPhase(StrEnum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    BRAKING = "braking"
    RELEASING = "releasing"
    OUTSIDE_STOPPING = "outside_stopping"
    OUTSIDE_RELEASING = "outside_releasing"


@dataclass(frozen=True, slots=True)
class GroundRouteCapabilityInterval:
    start_progress_blocks: float
    end_progress_blocks: float
    capabilities: frozenset[GroundRouteCapability]

    def __post_init__(self) -> None:
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in
               (self.start_progress_blocks, self.end_progress_blocks)):
            raise ContractViolation("route capability bounds must be finite")
        if not 0 <= self.start_progress_blocks < self.end_progress_blocks:
            raise ContractViolation("route capability bounds must be ordered and nonnegative")
        if (type(self.capabilities) is not frozenset or not self.capabilities
                or any(type(c) is not GroundRouteCapability for c in self.capabilities)):
            raise ContractViolation("route capabilities must be immutable and typed")

    def contains(self, progress: float) -> bool:
        return self.start_progress_blocks <= progress <= self.end_progress_blocks


@dataclass(frozen=True, slots=True)
class GroundRouteExecutionContract:
    capability_intervals: tuple[GroundRouteCapabilityInterval, ...]
    dependencies: tuple[BlockPos, ...]
    profile_id: str
    ruleset_id: str = JAVA_1_21_RULESET.ruleset_id
    completion_region: GroundCompletionRegion | None = None

    @classmethod
    def for_completion(cls, region: GroundCompletionRegion, dependencies: tuple[BlockPos, ...],
                       profile_id: str) -> 'GroundRouteExecutionContract':
        return cls((), tuple(sorted(set(dependencies) | set(region.dependencies))),
                   profile_id, completion_region=region)

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "route execution profile id")
        require_identifier(self.ruleset_id, "route execution ruleset id")
        if self.ruleset_id != JAVA_1_21_RULESET.ruleset_id:
            raise ContractViolation("route execution ruleset is unsupported")
        if (type(self.capability_intervals) is not tuple
                or any(type(i) is not GroundRouteCapabilityInterval for i in self.capability_intervals)):
            raise ContractViolation("route capability intervals must be immutable and typed")
        if any(b.start_progress_blocks <= a.end_progress_blocks
               for a, b in zip(self.capability_intervals, self.capability_intervals[1:])):
            raise ContractViolation("route capability intervals overlap or are unordered")
        _validate_dependencies(self.dependencies)
        if self.completion_region is not None:
            if type(self.completion_region) is not GroundCompletionRegion:
                raise ContractViolation("route completion region must be immutable and typed")
            if not set(self.completion_region.dependencies).issubset(self.dependencies):
                raise ContractViolation("route contract omits completion dependencies")

    def permits(self, capability: GroundRouteCapability, progress: float) -> bool:
        return any(capability in i.capabilities and i.contains(progress)
                   for i in self.capability_intervals)

    def validate_length(self, length: float) -> None:
        if any(i.end_progress_blocks > length + 1.e-9 for i in self.capability_intervals):
            raise ContractViolation("route capability interval exceeds route length")
