"""Shared goal, movement-transition and path-resource contracts."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.motion_nav.world_model import Aabb, BlockPos


def _finite(value: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ContractViolation(f"{name} must be finite")
    return float(value)


class MovementMode(StrEnum):
    WALK = "walk"
    SPRINT = "sprint"
    CROUCH = "crouch"
    CRAWL = "crawl"
    JUMP_UP = "jump_up"
    JUMP_GAP = "jump_gap"
    CONTROLLED_DROP = "controlled_drop"
    CLIMB = "climb"
    SWIM = "swim"


class GoalSupport(StrEnum):
    SOLID = "solid"
    FLUID = "fluid"
    ATTACHED = "attached"
    ANY = "any"


class CancellationMode(StrEnum):
    GROUND_STOP = "ground_stop"
    SAFE_LANDING = "safe_landing"
    HOLD_ATTACHMENT = "hold_attachment"
    REACH_BREATHABLE_STATE = "reach_breathable_state"


@dataclass(frozen=True, slots=True)
class ResourceState:
    """Remaining capacities. Larger values are always at least as capable."""

    values: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if type(self.values) is not tuple:
            raise ContractViolation("resource state must be immutable")
        names = []
        for item in self.values:
            if type(item) is not tuple or len(item) != 2:
                raise ContractViolation("resource state entries must be name/value pairs")
            name, value = item
            require_identifier(name, "resource name")
            if _finite(value, "resource value") < 0:
                raise ContractViolation("resource values must be nonnegative")
            names.append(name)
        if tuple(names) != tuple(sorted(set(names))):
            raise ContractViolation("resource state names must be sorted and unique")

    def as_dict(self) -> dict[str, float]:
        return dict(self.values)

    def at_least(self, minimum: ResourceState) -> bool:
        if type(minimum) is not ResourceState:
            raise ContractViolation("minimum resources require a resource state")
        available = self.as_dict()
        return all(name in available and available[name] >= value - 1.0e-12
                   for name, value in minimum.values)

    def dominates(self, other: ResourceState) -> bool:
        if type(other) is not ResourceState:
            raise ContractViolation("resource dominance requires a resource state")
        mine, theirs = self.as_dict(), other.as_dict()
        if mine.keys() != theirs.keys():
            return False
        return all(mine[name] >= theirs[name] - 1.0e-12 for name in mine)

    def apply(
        self,
        change: ResourceChange,
        minimum: ResourceState,
        capacity: ResourceState | None = None,
    ) -> ResourceState | None:
        if (type(change) is not ResourceChange or type(minimum) is not ResourceState
                or (capacity is not None and type(capacity) is not ResourceState)):
            raise ContractViolation("resource update requires typed change, minimum and capacity")
        current = self.as_dict()
        capacities = capacity.as_dict() if capacity is not None else None
        for name, delta in change.deltas:
            if name not in current:
                return None
            current[name] += delta
            if current[name] < -1.0e-12:
                return None
            current[name] = max(0.0, current[name])
            if capacities is not None:
                if name not in capacities:
                    return None
                current[name] = min(current[name], capacities[name])
        updated = ResourceState(tuple(sorted(current.items())))
        return updated if updated.at_least(minimum) else None


@dataclass(frozen=True, slots=True)
class ResourceChange:
    """Change to remaining capacity: negative consumes, positive restores."""

    deltas: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if type(self.deltas) is not tuple:
            raise ContractViolation("resource change must be immutable")
        names = []
        for item in self.deltas:
            if type(item) is not tuple or len(item) != 2:
                raise ContractViolation("resource changes must be name/value pairs")
            name, delta = item
            require_identifier(name, "resource change name")
            _finite(delta, "resource delta")
            names.append(name)
        if tuple(names) != tuple(sorted(set(names))):
            raise ContractViolation("resource change names must be sorted and unique")


@dataclass(frozen=True, slots=True)
class MovementStateClass:
    mode: MovementMode
    pose: str
    minimum_speed_blocks_per_second: float
    maximum_speed_blocks_per_second: float

    def __post_init__(self) -> None:
        if type(self.mode) is not MovementMode:
            raise ContractViolation("movement state requires a mode")
        require_identifier(self.pose, "movement pose")
        minimum = _finite(self.minimum_speed_blocks_per_second, "minimum movement speed")
        maximum = _finite(self.maximum_speed_blocks_per_second, "maximum movement speed")
        if minimum < 0 or maximum < minimum:
            raise ContractViolation("movement speed interval is invalid")


@dataclass(frozen=True, slots=True)
class MovementTransition:
    transition_id: str
    environment_id: str
    mode: MovementMode
    entry: MovementStateClass
    exits: tuple[MovementStateClass, ...]
    duration_seconds: float
    dependencies: tuple[BlockPos, ...]
    resource_change: ResourceChange
    cancellation: CancellationMode
    input_loss: CancellationMode
    trajectory_profile_id: str | None = None
    risk_tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        require_identifier(self.transition_id, "movement transition id")
        require_identifier(self.environment_id, "movement transition environment")
        if type(self.mode) is not MovementMode or type(self.entry) is not MovementStateClass:
            raise ContractViolation("movement transition requires typed mode and entry")
        if (type(self.exits) is not tuple or not self.exits
                or any(type(state) is not MovementStateClass for state in self.exits)):
            raise ContractViolation("movement transition requires immutable exits")
        if _finite(self.duration_seconds, "movement transition duration") <= 0:
            raise ContractViolation("movement transition duration must be positive")
        if type(self.dependencies) is not tuple:
            raise ContractViolation("movement transition dependencies must be immutable")
        if type(self.resource_change) is not ResourceChange:
            raise ContractViolation("movement transition requires a resource change")
        if (type(self.cancellation) is not CancellationMode
                or type(self.input_loss) is not CancellationMode):
            raise ContractViolation("movement transition requires cancellation modes")
        if self.trajectory_profile_id is not None:
            require_identifier(self.trajectory_profile_id, "movement trajectory profile id")
        if type(self.risk_tags) is not frozenset:
            raise ContractViolation("movement transition risk tags must be immutable")
        for risk_tag in self.risk_tags:
            require_identifier(risk_tag, "movement risk tag")


@dataclass(frozen=True, slots=True)
class GoalState:
    region: Aabb
    support: GoalSupport
    allowed_modes: frozenset[MovementMode]
    allowed_poses: frozenset[str]
    maximum_terminal_speed_blocks_per_second: float
    minimum_resources: ResourceState = ResourceState()
    required_yaw_radians: float | None = None
    maximum_yaw_error_radians: float | None = None
    risk_policy_id: str = "no_expected_damage"

    def __post_init__(self) -> None:
        if type(self.region) is not Aabb or type(self.support) is not GoalSupport:
            raise ContractViolation("goal requires a region and support kind")
        if (type(self.allowed_modes) is not frozenset or not self.allowed_modes
                or any(type(mode) is not MovementMode for mode in self.allowed_modes)):
            raise ContractViolation("goal requires allowed movement modes")
        if (type(self.allowed_poses) is not frozenset or not self.allowed_poses):
            raise ContractViolation("goal requires allowed poses")
        for pose in self.allowed_poses:
            require_identifier(pose, "goal pose")
        if _finite(self.maximum_terminal_speed_blocks_per_second,
                   "goal terminal speed") < 0:
            raise ContractViolation("goal terminal speed must be nonnegative")
        if type(self.minimum_resources) is not ResourceState:
            raise ContractViolation("goal minimum resources must be a resource state")
        if self.required_yaw_radians is None:
            if self.maximum_yaw_error_radians is not None:
                raise ContractViolation("goal yaw tolerance requires a required yaw")
        else:
            _finite(self.required_yaw_radians, "goal required yaw")
            if (self.maximum_yaw_error_radians is None
                    or not 0 <= _finite(self.maximum_yaw_error_radians,
                                        "goal yaw tolerance") <= math.pi):
                raise ContractViolation("goal yaw tolerance must be within 0..pi")
        require_identifier(self.risk_policy_id, "goal risk policy id")

    def accepts(
        self,
        *,
        position: tuple[float, float, float],
        support: GoalSupport,
        mode: MovementMode,
        pose: str,
        speed_blocks_per_second: float,
        resources: ResourceState,
        yaw_radians: float | None = None,
        applied_risk_policy_id: str = "no_expected_damage",
    ) -> bool:
        if (type(position) is not tuple or len(position) != 3
                or any(type(value) not in (int, float) or not math.isfinite(float(value))
                       for value in position)):
            raise ContractViolation("goal position must be a finite triple")
        speed = _finite(speed_blocks_per_second, "goal observed speed")
        inside = (
            self.region.min_x <= position[0] <= self.region.max_x
            and self.region.min_y <= position[1] <= self.region.max_y
            and self.region.min_z <= position[2] <= self.region.max_z
        )
        support_matches = self.support is GoalSupport.ANY or support is self.support
        heading_matches = self.required_yaw_radians is None
        if self.required_yaw_radians is not None and yaw_radians is not None:
            yaw = _finite(yaw_radians, "goal observed yaw")
            delta = math.atan2(
                math.sin(yaw - self.required_yaw_radians),
                math.cos(yaw - self.required_yaw_radians),
            )
            heading_matches = abs(delta) <= self.maximum_yaw_error_radians + 1.0e-12
        return (
            inside and support_matches and mode in self.allowed_modes
            and pose in self.allowed_poses
            and speed <= self.maximum_terminal_speed_blocks_per_second + 1.0e-12
            and resources.at_least(self.minimum_resources)
            and heading_matches and applied_risk_policy_id == self.risk_policy_id
        )


def compose_movement_transitions(
    transition_id: str,
    transitions: tuple[MovementTransition, ...],
) -> MovementTransition:
    require_identifier(transition_id, "composed movement transition id")
    if (type(transitions) is not tuple or not transitions
            or any(type(item) is not MovementTransition for item in transitions)):
        raise ContractViolation("movement transition composition requires typed transitions")
    first, last = transitions[0], transitions[-1]
    if any(abs(delta) > 1.0e-12
           for item in transitions for _, delta in item.resource_change.deltas):
        raise ContractViolation("resource-changing transitions must preserve their order")
    if any(
        item.environment_id != first.environment_id
        or item.mode is not first.mode
        or item.cancellation is not first.cancellation
        or item.input_loss is not first.input_loss
        for item in transitions
    ):
        raise ContractViolation("incompatible movement transitions cannot be composed")
    return MovementTransition(
        transition_id=transition_id,
        environment_id=first.environment_id,
        mode=first.mode,
        entry=first.entry,
        exits=last.exits,
        duration_seconds=sum(item.duration_seconds for item in transitions),
        dependencies=tuple(sorted({
            dependency for item in transitions for dependency in item.dependencies
        })),
        resource_change=ResourceChange(),
        cancellation=first.cancellation,
        input_loss=first.input_loss,
        trajectory_profile_id=(
            first.trajectory_profile_id
            if all(item.trajectory_profile_id == first.trajectory_profile_id
                   for item in transitions)
            else None
        ),
        risk_tags=frozenset(
            risk for item in transitions for risk in item.risk_tags
        ),
    )
