"""B09-R immutable contracts for deterministic Minecraft 1.21 motion calculation."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Mapping

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.motion_nav.world_model import Aabb, BlockPos, WorldSessionId


class StateBuildStatus(StrEnum):
    READY = "ready"
    NEEDS_STATE = "needs_state"
    INVALID_INPUT = "invalid_input"


class CalculationStatus(StrEnum):
    OK = "ok"
    NEEDS_WORLD = "needs_world"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"


class ResourceStatus(StrEnum):
    COMPLETE = "complete"
    CONDITIONAL = "conditional"


@dataclass(frozen=True, slots=True)
class PhysicsRuleset:
    ruleset_id: str
    state_schema: str
    input_schema: str
    tick_seconds: float
    numeric_semantics: str

    def __post_init__(self) -> None:
        for value, name in ((self.ruleset_id, "ruleset id"), (self.state_schema, "state schema"),
                            (self.input_schema, "input schema"),
                            (self.numeric_semantics, "numeric semantics")):
            require_identifier(value, name)
        if not math.isfinite(self.tick_seconds) or self.tick_seconds != 0.05:
            raise ContractViolation("B09-R ruleset requires a 0.05 second logic tick")


JAVA_1_21_RULESET = PhysicsRuleset(
    "minecraft-java-1_21-player-motion-r1",
    "mc2p.physics-state.v1",
    "mc2p.physics-tick-input.v1",
    0.05,
    "python-float64-java-operation-order-v1",
)


def _finite_tuple(value: tuple[float, ...], size: int, name: str) -> None:
    if (type(value) is not tuple or len(value) != size
            or any(type(item) not in (int, float) or not math.isfinite(float(item)) for item in value)):
        raise ContractViolation(f"{name} must be a finite {size}-tuple")


@dataclass(frozen=True, slots=True)
class PhysicsEffect:
    effect_id: str
    amplifier: int
    duration_ticks: int

    def __post_init__(self) -> None:
        require_identifier(self.effect_id, "effect id")
        require_nonnegative_int(self.amplifier, "effect amplifier")
        require_nonnegative_int(self.duration_ticks, "effect duration")


@dataclass(frozen=True, slots=True)
class PhysicsState:
    ruleset_id: str
    state_schema: str
    session: WorldSessionId
    movement_tick_id: int
    position: tuple[float, float, float]
    velocity_blocks_per_tick: tuple[float, float, float]
    yaw_radians: float
    pitch_radians: float
    pose: str
    body_width: float
    body_height: float
    on_ground: bool
    horizontal_collision: bool
    vertical_collision: bool
    sprinting: bool
    sneaking: bool
    jumping_cooldown_ticks: int
    fall_distance_blocks: float
    movement_speed_attribute: float
    step_height_blocks: float
    gravity_attribute: float
    jump_strength_attribute: float
    food_points: int
    saturation_points: float
    game_mode: str
    status_effects: tuple[PhysicsEffect, ...]
    swimming: bool
    submerged_in_water: bool
    climbing: bool
    fall_flying: bool
    flying: bool
    allow_flying: bool
    is_using_item: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.ruleset_id, "ruleset id")
        require_identifier(self.state_schema, "state schema")
        if type(self.session) is not WorldSessionId:
            raise ContractViolation("physics state requires a world session")
        require_nonnegative_int(self.movement_tick_id, "movement tick id")
        _finite_tuple(self.position, 3, "position")
        _finite_tuple(self.velocity_blocks_per_tick, 3, "velocity")
        for name in ("yaw_radians", "pitch_radians", "body_width", "body_height",
                     "fall_distance_blocks", "movement_speed_attribute", "step_height_blocks",
                     "gravity_attribute", "jump_strength_attribute", "saturation_points"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ContractViolation(f"{name} must be finite")
        if self.body_width <= 0 or self.body_height <= 0:
            raise ContractViolation("body dimensions must be positive")
        if (self.fall_distance_blocks < 0 or self.movement_speed_attribute < 0
                or self.step_height_blocks < 0 or self.gravity_attribute < 0
                or self.jump_strength_attribute < 0):
            raise ContractViolation("physics magnitudes must be nonnegative")
        require_nonnegative_int(self.jumping_cooldown_ticks, "jumping cooldown")
        require_nonnegative_int(self.food_points, "food points")
        if not 0 <= self.food_points <= 20 or self.saturation_points < 0:
            raise ContractViolation("invalid hunger state")
        if type(self.status_effects) is not tuple or any(type(v) is not PhysicsEffect for v in self.status_effects):
            raise ContractViolation("status effects must be immutable physics effects")
        if tuple((v.effect_id, v.amplifier, v.duration_ticks) for v in self.status_effects) != tuple(
                sorted((v.effect_id, v.amplifier, v.duration_ticks) for v in self.status_effects)):
            raise ContractViolation("status effects must be sorted")
        for name in ("on_ground", "horizontal_collision", "vertical_collision", "sprinting",
                     "sneaking", "swimming", "submerged_in_water", "climbing", "fall_flying",
                     "flying", "allow_flying", "is_using_item"):
            if type(getattr(self, name)) is not bool:
                raise ContractViolation(f"{name} must be boolean")

    @property
    def body_box(self) -> Aabb:
        x, y, z = self.position
        half = self.body_width / 2
        return Aabb(x - half, y, z - half, x + half, y + self.body_height, z + half)

    def to_mapping(self) -> dict:
        return {
            "ruleset_id": self.ruleset_id, "state_schema": self.state_schema,
            "session": self.session.value, "movement_tick_id": self.movement_tick_id,
            "position": list(self.position), "velocity_blocks_per_tick": list(self.velocity_blocks_per_tick),
            "yaw_radians": self.yaw_radians, "pitch_radians": self.pitch_radians,
            "pose": self.pose, "body_width": self.body_width, "body_height": self.body_height,
            "on_ground": self.on_ground, "horizontal_collision": self.horizontal_collision,
            "vertical_collision": self.vertical_collision, "sprinting": self.sprinting,
            "sneaking": self.sneaking, "jumping_cooldown_ticks": self.jumping_cooldown_ticks,
            "fall_distance_blocks": self.fall_distance_blocks,
            "movement_speed_attribute": self.movement_speed_attribute,
            "step_height_blocks": self.step_height_blocks, "food_points": self.food_points,
            "gravity_attribute": self.gravity_attribute,
            "jump_strength_attribute": self.jump_strength_attribute,
            "saturation_points": self.saturation_points, "game_mode": self.game_mode,
            "status_effects": [
                {"effect_id": v.effect_id, "amplifier": v.amplifier, "duration_ticks": v.duration_ticks}
                for v in self.status_effects
            ],
            "swimming": self.swimming, "submerged_in_water": self.submerged_in_water,
            "climbing": self.climbing, "fall_flying": self.fall_flying,
            "flying": self.flying, "allow_flying": self.allow_flying,
            "is_using_item": self.is_using_item,
        }

    @classmethod
    def from_mapping(cls, value: Mapping) -> "PhysicsState":
        if not isinstance(value, Mapping):
            raise ContractViolation("physics state mapping is required")
        data = dict(value)
        data["session"] = WorldSessionId(data["session"])
        data["position"] = tuple(data["position"])
        data["velocity_blocks_per_tick"] = tuple(data["velocity_blocks_per_tick"])
        data["status_effects"] = tuple(PhysicsEffect(**item) for item in data["status_effects"])
        try:
            return cls(**data)
        except TypeError as error:
            raise ContractViolation("invalid physics state fields") from error


@dataclass(frozen=True, slots=True)
class TickInput:
    forward: float
    strafe: float
    jump: bool
    sneak: bool
    sprint: bool
    movement_yaw_radians: float

    def __post_init__(self) -> None:
        for name in ("forward", "strafe", "movement_yaw_radians"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ContractViolation(f"{name} must be finite")
        if not -1 <= self.forward <= 1 or not -1 <= self.strafe <= 1:
            raise ContractViolation("sampled movement axes must be within -1..1")
        for name in ("jump", "sneak", "sprint"):
            if type(getattr(self, name)) is not bool:
                raise ContractViolation(f"{name} must be boolean")

    def to_mapping(self) -> dict:
        return {"schema_version": JAVA_1_21_RULESET.input_schema, "forward": self.forward,
                "strafe": self.strafe, "jump": self.jump, "sneak": self.sneak,
                "sprint": self.sprint, "movement_yaw_radians": self.movement_yaw_radians}

    @classmethod
    def from_mapping(cls, value: Mapping) -> "TickInput":
        if not isinstance(value, Mapping) or value.get("schema_version") != JAVA_1_21_RULESET.input_schema:
            raise ContractViolation("invalid tick input schema")
        return cls(*(value[name] for name in (
            "forward", "strafe", "jump", "sneak", "sprint", "movement_yaw_radians")))


@dataclass(frozen=True, slots=True)
class StateBuildResult:
    status: StateBuildStatus
    state: PhysicsState | None = None
    missing_fields: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    provenance: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not StateBuildStatus:
            raise ContractViolation("invalid state build status")
        ready = self.status is StateBuildStatus.READY
        if ready != (type(self.state) is PhysicsState):
            raise ContractViolation("only READY can carry a physics state")
        if ready and (self.missing_fields or self.conflicts):
            raise ContractViolation("ready state cannot carry build errors")
        if self.status is StateBuildStatus.NEEDS_STATE and (not self.missing_fields or self.conflicts):
            raise ContractViolation("NEEDS_STATE requires only missing fields")
        if self.status is StateBuildStatus.INVALID_INPUT and (not self.conflicts or self.missing_fields):
            raise ContractViolation("INVALID_INPUT requires only conflicts")

    def require_state(self) -> PhysicsState:
        if self.status is not StateBuildStatus.READY or self.state is None:
            raise ContractViolation("physics state is not ready")
        return self.state


@dataclass(frozen=True, slots=True)
class WorldShapeQuery:
    boxes: tuple[Aabb, ...]
    dependencies: tuple[BlockPos, ...]
    missing_cells: tuple[BlockPos, ...] = ()
    unsupported_cells: tuple[BlockPos, ...] = ()


@dataclass(frozen=True, slots=True)
class MotionSegment:
    kind: str
    requested_delta: tuple[float, float, float]
    applied_delta: tuple[float, float, float]

    def __post_init__(self) -> None:
        require_identifier(self.kind, "motion segment kind")
        _finite_tuple(self.requested_delta, 3, "requested delta")
        _finite_tuple(self.applied_delta, 3, "applied delta")


@dataclass(frozen=True, slots=True)
class ResourceUpdate:
    """Resource contribution known from this client-side movement tick.

    Food/saturation mutation is server-authoritative and depends on additional
    hunger-manager clocks, so this result reports exact known contributions and
    marks whether a complete next resource state was available.
    """

    status: ResourceStatus
    exhaustion_delta: float
    incomplete_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not ResourceStatus:
            raise ContractViolation("invalid resource status")
        if (type(self.exhaustion_delta) not in (int, float)
                or not math.isfinite(float(self.exhaustion_delta))
                or self.exhaustion_delta < 0):
            raise ContractViolation("resource exhaustion delta must be nonnegative and finite")
        if ((self.status is ResourceStatus.COMPLETE) == bool(self.incomplete_reasons)):
            raise ContractViolation("resource completeness and reasons disagree")


@dataclass(frozen=True, slots=True)
class StepResult:
    status: CalculationStatus
    next_state: PhysicsState | None = None
    events: tuple[str, ...] = ()
    segments: tuple[MotionSegment, ...] = ()
    dependencies: tuple[BlockPos, ...] = ()
    missing_cells: tuple[BlockPos, ...] = ()
    unsupported_reasons: tuple[str, ...] = ()
    invalid_reasons: tuple[str, ...] = ()
    resource_update: ResourceUpdate | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not CalculationStatus:
            raise ContractViolation("invalid calculation status")
        if (self.status is CalculationStatus.OK) != (type(self.next_state) is PhysicsState):
            raise ContractViolation("only a complete physics step can carry next_state")
        if self.status is CalculationStatus.NEEDS_WORLD and not self.missing_cells:
            raise ContractViolation("NEEDS_WORLD requires missing cells")
        if self.status is CalculationStatus.UNSUPPORTED and not self.unsupported_reasons:
            raise ContractViolation("UNSUPPORTED requires reasons")
        if self.status is CalculationStatus.INVALID_INPUT and not self.invalid_reasons:
            raise ContractViolation("INVALID_INPUT requires reasons")
        if self.status is CalculationStatus.OK and type(self.resource_update) is not ResourceUpdate:
            raise ContractViolation("complete physics step requires a resource update")
        if self.status is not CalculationStatus.OK and self.resource_update is not None:
            raise ContractViolation("incomplete physics step cannot carry a resource update")
