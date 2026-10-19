"""Immutable completion geometry for ordinary ground execution."""
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class GroundRouteExecutionContract:
    dependencies: tuple[BlockPos, ...]
    profile_id: str
    ruleset_id: str = JAVA_1_21_RULESET.ruleset_id
    completion_region: GroundCompletionRegion | None = None

    @classmethod
    def for_completion(cls, region: GroundCompletionRegion, dependencies: tuple[BlockPos, ...],
                       profile_id: str) -> 'GroundRouteExecutionContract':
        return cls(tuple(sorted(set(dependencies) | set(region.dependencies))),
                   profile_id, completion_region=region)

    def __post_init__(self) -> None:
        require_identifier(self.profile_id, "route execution profile id")
        require_identifier(self.ruleset_id, "route execution ruleset id")
        if self.ruleset_id != JAVA_1_21_RULESET.ruleset_id:
            raise ContractViolation("route execution ruleset is unsupported")
        _validate_dependencies(self.dependencies)
        if self.completion_region is not None:
            if type(self.completion_region) is not GroundCompletionRegion:
                raise ContractViolation("route completion region must be immutable and typed")
            if not set(self.completion_region.dependencies).issubset(self.dependencies):
                raise ContractViolation("route contract omits completion dependencies")
