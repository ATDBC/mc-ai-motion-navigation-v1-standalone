"""Versioned structured-only observation values for live Player Runtime use."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from mc2p.contracts.common import (
    ContractViolation,
    FieldStatusV0,
    FieldValueV0,
    require_field_name,
    require_finite,
    require_identifier,
    require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0


T = TypeVar("T")
_GROUP_SOURCES = frozenset(
    {
        "client_player",
        "client_inventory",
        "client_screen_handler",
        "client_perception_filtered",
        "client_registered_entity",
    }
)
_GAME_MODES = frozenset({"survival", "creative", "adventure", "spectator"})
_POSES = frozenset(
    {
        "standing",
        "fall_flying",
        "sleeping",
        "swimming",
        "spin_attack",
        "crouching",
        "long_jumping",
        "dying",
        "croaking",
        "using_tongue",
        "sitting",
        "roaring",
        "sniffing",
        "emerging",
        "digging",
        "sliding",
        "shooting",
        "inhaling",
    }
)


def _require_bool(value: bool, name: str) -> None:
    if type(value) is not bool:
        raise ContractViolation(f"{name} must be bool")


def _require_nonnegative_number(value: float, name: str) -> None:
    require_finite(value, name)
    if float(value) < 0:
        raise ContractViolation(f"{name} cannot be negative")


@dataclass(frozen=True, slots=True)
class ObservationGroupV2(Generic[T]):
    status: FieldStatusV0
    sample_world_tick: int
    source_kind: str
    reason_code: str | None
    value: T | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, FieldStatusV0):
            raise ContractViolation("observation group status is invalid")
        require_nonnegative_int(self.sample_world_tick, "sample world tick")
        if self.source_kind not in _GROUP_SOURCES:
            raise ContractViolation("observation group source kind is invalid")
        if self.status is FieldStatusV0.VALID:
            if self.value is None:
                raise ContractViolation("valid observation group must carry data")
            if self.reason_code is not None:
                raise ContractViolation("valid observation group reason must be null")
        else:
            if self.value is not None:
                raise ContractViolation("non-valid observation group must not carry data")
            if self.reason_code is None:
                raise ContractViolation("non-valid observation group requires reason code")
            require_identifier(self.reason_code, "observation group reason code")

    @classmethod
    def valid(
        cls,
        sample_world_tick: int,
        source_kind: str,
        value: T,
    ) -> ObservationGroupV2[T]:
        return cls(FieldStatusV0.VALID, sample_world_tick, source_kind, None, value)

    @classmethod
    def missing(
        cls,
        sample_world_tick: int,
        source_kind: str,
        reason_code: str,
    ) -> ObservationGroupV2[T]:
        return cls(
            FieldStatusV0.MISSING,
            sample_world_tick,
            source_kind,
            reason_code,
            None,
        )


@dataclass(frozen=True, slots=True)
class ItemStackV2:
    empty: bool
    item_id: str | None
    count: int
    damage: int
    max_damage: int
    damageable: bool
    custom_name: str | None

    def __post_init__(self) -> None:
        _require_bool(self.empty, "item empty")
        _require_bool(self.damageable, "item damageable")
        for name, value in (
            ("item count", self.count),
            ("item damage", self.damage),
            ("item max damage", self.max_damage),
        ):
            require_nonnegative_int(value, name)
        if self.empty:
            if (
                self.item_id is not None
                or self.count != 0
                or self.damage != 0
                or self.max_damage != 0
                or self.damageable
                or self.custom_name is not None
            ):
                raise ContractViolation("empty item stack must not carry item data")
            return
        if self.item_id is None:
            raise ContractViolation("non-empty item stack requires item id")
        require_identifier(self.item_id, "item id")
        if self.count <= 0:
            raise ContractViolation("non-empty item stack count must be positive")
        if self.damageable != (self.max_damage > 0):
            raise ContractViolation("item damageable flag must match max damage")
        if self.damage > self.max_damage:
            raise ContractViolation("item damage cannot exceed max damage")
        if self.custom_name is not None and not self.custom_name:
            raise ContractViolation("custom item name cannot be empty")


@dataclass(frozen=True, slots=True)
class StatusEffectV2:
    effect_id: str
    amplifier: int
    duration_ticks: int
    ambient: bool
    show_particles: bool
    show_icon: bool

    def __post_init__(self) -> None:
        require_identifier(self.effect_id, "status effect id")
        require_nonnegative_int(self.amplifier, "status effect amplifier")
        require_nonnegative_int(self.duration_ticks, "status effect duration")
        _require_bool(self.ambient, "status effect ambient")
        _require_bool(self.show_particles, "status effect show particles")
        _require_bool(self.show_icon, "status effect show icon")


@dataclass(frozen=True, slots=True)
class SelfStateV2:
    position: Vec3V0
    velocity: Vec3V0
    yaw_degrees: float
    pitch_degrees: float
    head_yaw_degrees: float
    body_yaw_degrees: float
    is_dead: bool
    is_on_ground: bool
    horizontal_collision: bool
    vertical_collision: bool
    pose: str
    is_sprinting: bool
    is_sneaking: bool
    is_swimming: bool
    is_submerged_in_water: bool
    is_climbing: bool
    is_fall_flying: bool
    is_burning: bool
    fall_distance_blocks: float
    air_ticks: int
    max_air_ticks: int
    health_points: float
    max_health_points: float
    absorption_points: float
    armor_points: int
    food_points: int
    saturation_points: float
    experience_level: int
    experience_progress: float
    total_experience: int
    attack_cooldown: float
    active_hand: str | None
    is_using_item: bool
    item_use_ticks_remaining: int
    status_effects: tuple[StatusEffectV2, ...]
    game_mode: str
    is_flying: bool
    allow_flying: bool
    hurt_animation_ticks: int | None = None
    movement_tick_id: int | None = None
    eye_height_blocks: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.position, Vec3V0) or not isinstance(
            self.velocity, Vec3V0
        ):
            raise ContractViolation("self position and velocity must be Vec3V0")
        for name, value in (
            ("yaw", self.yaw_degrees),
            ("pitch", self.pitch_degrees),
            ("head yaw", self.head_yaw_degrees),
            ("body yaw", self.body_yaw_degrees),
            ("experience progress", self.experience_progress),
            ("attack cooldown", self.attack_cooldown),
        ):
            require_finite(value, name)
        for name, value in (
            ("fall distance", self.fall_distance_blocks),
            ("health", self.health_points),
            ("max health", self.max_health_points),
            ("absorption", self.absorption_points),
            ("saturation", self.saturation_points),
        ):
            _require_nonnegative_number(value, name)
        for name, value in (
            ("air", self.air_ticks),
            ("max air", self.max_air_ticks),
            ("armor", self.armor_points),
            ("food", self.food_points),
            ("experience level", self.experience_level),
            ("total experience", self.total_experience),
            ("item use ticks", self.item_use_ticks_remaining),
        ):
            require_nonnegative_int(value, name)
        for name in (
            "is_dead",
            "is_on_ground",
            "horizontal_collision",
            "vertical_collision",
            "is_sprinting",
            "is_sneaking",
            "is_swimming",
            "is_submerged_in_water",
            "is_climbing",
            "is_fall_flying",
            "is_burning",
            "is_using_item",
            "is_flying",
            "allow_flying",
        ):
            _require_bool(getattr(self, name), name)
        if self.pose not in _POSES:
            raise ContractViolation("self pose is invalid")
        if self.game_mode not in _GAME_MODES:
            raise ContractViolation("self game mode is invalid")
        if self.active_hand not in {None, "main_hand", "off_hand"}:
            raise ContractViolation("active hand is invalid")
        if not 0.0 <= self.experience_progress <= 1.0:
            raise ContractViolation("experience progress must be between zero and one")
        if not 0.0 <= self.attack_cooldown <= 1.0:
            raise ContractViolation("attack cooldown must be between zero and one")
        if self.air_ticks > self.max_air_ticks:
            raise ContractViolation("air cannot exceed max air")
        if self.health_points > self.max_health_points:
            raise ContractViolation("health cannot exceed max health")
        if type(self.status_effects) is not tuple:
            raise ContractViolation("status effects must be a tuple")
        if not all(isinstance(item, StatusEffectV2) for item in self.status_effects):
            raise ContractViolation("status effects contain an invalid entry")
        if (self.hurt_animation_ticks is None) != (self.movement_tick_id is None):
            raise ContractViolation(
                "self hurt animation and movement tick must be available together"
            )
        if self.hurt_animation_ticks is not None:
            require_nonnegative_int(
                self.hurt_animation_ticks, "self hurt animation ticks"
            )
            if self.hurt_animation_ticks > 20:
                raise ContractViolation("self hurt animation ticks cannot exceed 20")
            require_nonnegative_int(self.movement_tick_id, "self movement tick")
        if self.eye_height_blocks is not None:
            require_finite(self.eye_height_blocks, "self eye height")
            if not 0.0 < self.eye_height_blocks <= 3.0:
                raise ContractViolation("self eye height must be in (0,3]")


@dataclass(frozen=True, slots=True)
class InventoryStateV2:
    main: tuple[ItemStackV2, ...]
    selected_hotbar_slot: int
    head: ItemStackV2
    chest: ItemStackV2
    legs: ItemStackV2
    feet: ItemStackV2
    offhand: ItemStackV2
    main_hand: ItemStackV2

    def __post_init__(self) -> None:
        if type(self.main) is not tuple or len(self.main) != 36:
            raise ContractViolation("inventory main must contain exactly 36 slots")
        if not all(isinstance(item, ItemStackV2) for item in self.main):
            raise ContractViolation("inventory main entries must be item stacks")
        if type(self.selected_hotbar_slot) is not int or not 0 <= self.selected_hotbar_slot <= 8:
            raise ContractViolation("selected hotbar slot must be between zero and eight")
        for name in ("head", "chest", "legs", "feet", "offhand", "main_hand"):
            if not isinstance(getattr(self, name), ItemStackV2):
                raise ContractViolation(f"inventory {name} must be an item stack")
        if self.main_hand != self.main[self.selected_hotbar_slot]:
            raise ContractViolation("main hand must match selected hotbar slot")


@dataclass(frozen=True, slots=True)
class GuiSlotV2:
    slot_id: int
    x: int
    y: int
    source_kind: str
    source_index: int | None
    item: ItemStackV2
    enabled: bool
    can_take: bool

    def __post_init__(self) -> None:
        require_nonnegative_int(self.slot_id, "GUI slot id")
        if type(self.x) is not int or type(self.y) is not int:
            raise ContractViolation("GUI slot coordinates must be integers")
        if self.source_kind not in {
            "player_hotbar",
            "player_main",
            "player_armor",
            "player_offhand",
            "container",
            "input",
            "output",
            "unknown",
        }:
            raise ContractViolation("GUI slot source kind is invalid")
        if self.source_index is not None:
            require_nonnegative_int(self.source_index, "GUI slot source index")
        if not isinstance(self.item, ItemStackV2):
            raise ContractViolation("GUI slot item must be ItemStackV2")
        _require_bool(self.enabled, "GUI slot enabled")
        _require_bool(self.can_take, "GUI slot can take")


GUI_PUBLIC_PROPERTY_IDS: dict[str, tuple[int, ...]] = {
    **{kind: (0, 1, 2, 3) for kind in ("minecraft:furnace", "minecraft:blast_furnace", "minecraft:smoker")},
    **{kind: () for kind in ("minecraft:player", "minecraft:crafting", "minecraft:hopper", "minecraft:shulker_box",
                            "minecraft:generic_3x3", *(f"minecraft:generic_9x{i}" for i in range(1, 7)))},
}


@dataclass(frozen=True, slots=True)
class GuiStateV2:
    open: bool
    screen_kind: str
    handler_type: str | None
    sync_id: int | None
    revision: int | None
    title: str | None
    slots: tuple[GuiSlotV2, ...]
    cursor_stack: ItemStackV2
    properties: tuple[tuple[int, int], ...]
    focused_slot_id: int | None
    gui_session_id: str | None = None
    properties_status: str = "valid"
    properties_reason_code: str | None = None

    def __post_init__(self) -> None:
        _require_bool(self.open, "gui open")
        if self.properties_status not in ("valid", "unsupported"):
            raise ContractViolation("GUI properties status is invalid")
        if self.properties_status == "unsupported":
            if self.properties or self.properties_reason_code not in (
                    "unmapped_public_properties", "property_layout_mismatch") or not self.open:
                raise ContractViolation("unsupported GUI properties must be absent with a reason")
        elif self.properties_reason_code is not None:
            raise ContractViolation("valid GUI properties cannot carry a missing reason")
        if not self.open:
            if (
                self.screen_kind != "closed"
                or self.gui_session_id is not None
                or self.handler_type is not None
                or self.sync_id is not None
                or self.revision is not None
                or self.title is not None
                or self.slots
                or self.properties
                or self.focused_slot_id is not None
            ):
                raise ContractViolation("closed gui must not retain handler state")
            if not self.cursor_stack.empty:
                raise ContractViolation("closed gui cursor stack must be empty")
            return
        if self.screen_kind not in {
            "player_inventory",
            "generic_container",
            "crafting",
            "furnace",
            "merchant",
            "anvil",
            "enchanting",
            "beacon",
            "hopper",
            "shulker_box",
            "horse",
            "unknown",
        }:
            raise ContractViolation("open GUI screen kind is invalid")
        if self.handler_type is None:
            raise ContractViolation("open GUI requires handler type")
        require_identifier(self.gui_session_id, "GUI session id")
        require_identifier(self.handler_type, "GUI handler type")
        if self.sync_id is None or self.revision is None:
            raise ContractViolation("open GUI requires sync id and revision")
        require_nonnegative_int(self.sync_id, "GUI sync id")
        require_nonnegative_int(self.revision, "GUI revision")
        if self.title is None:
            raise ContractViolation("open GUI requires title")
        if type(self.slots) is not tuple or not all(
            isinstance(slot, GuiSlotV2) for slot in self.slots
        ):
            raise ContractViolation("GUI slots must be GuiSlotV2 values")
        slot_ids = tuple(slot.slot_id for slot in self.slots)
        if len(set(slot_ids)) != len(slot_ids):
            raise ContractViolation("GUI slot ids must be unique")
        if not isinstance(self.cursor_stack, ItemStackV2):
            raise ContractViolation("GUI cursor stack is invalid")
        if type(self.properties) is not tuple:
            raise ContractViolation("GUI properties must be a tuple")
        for item in self.properties:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not int
                or type(item[1]) is not int
                or item[0] < 0
            ):
                raise ContractViolation("GUI property entry is invalid")
        property_ids = tuple(item[0] for item in self.properties)
        if len(set(property_ids)) != len(property_ids):
            raise ContractViolation("GUI property ids must be unique")
        if self.properties_status == "valid":
            expected = GUI_PUBLIC_PROPERTY_IDS.get(self.handler_type)
            if expected is None or property_ids != expected:
                raise ContractViolation("GUI properties do not match the public handler whitelist")
            if any(not 0 <= value <= 32767 for _, value in self.properties):
                raise ContractViolation("GUI property value is outside the supported vanilla range")
        if self.focused_slot_id is not None:
            require_nonnegative_int(self.focused_slot_id, "focused slot id")
            if self.focused_slot_id not in set(slot_ids):
                raise ContractViolation("focused slot id is not present")

    @classmethod
    def closed(cls) -> GuiStateV2:
        return cls(
            open=False,
            screen_kind="closed",
            handler_type=None,
            sync_id=None,
            revision=None,
            title=None,
            slots=(),
            cursor_stack=ItemStackV2(True, None, 0, 0, 0, False, None),
            properties=(),
            focused_slot_id=None,
        )


@dataclass(frozen=True, slots=True)
class BlockRayV2:
    ray_id: int
    row: int
    column: int
    yaw_offset_degrees: float
    pitch_offset_degrees: float
    hit_kind: str
    distance_blocks: float
    relative_block_position: Vec3V0 | None = None
    relative_hit_position: Vec3V0 | None = None
    face: str | None = None
    block_id: str | None = None
    fluid_id: str | None = None
    state_properties: tuple[tuple[str, str], ...] = ()
    collision_shape: str | None = None
    block_light: int | None = None
    sky_light: int | None = None

    def __post_init__(self) -> None:
        require_nonnegative_int(self.ray_id, "ray id")
        require_nonnegative_int(self.row, "ray row")
        require_nonnegative_int(self.column, "ray column")
        require_finite(self.yaw_offset_degrees, "ray yaw offset")
        require_finite(self.pitch_offset_degrees, "ray pitch offset")
        _require_nonnegative_number(self.distance_blocks, "ray distance")
        if self.hit_kind not in {"miss", "block"}:
            raise ContractViolation("ray hit kind is invalid")
        if self.hit_kind == "miss":
            if any(
                value is not None
                for value in (
                    self.relative_block_position,
                    self.relative_hit_position,
                    self.face,
                    self.block_id,
                    self.fluid_id,
                    self.collision_shape,
                    self.block_light,
                    self.sky_light,
                )
            ) or self.state_properties:
                raise ContractViolation("miss ray must not carry hit data")
            return
        if not isinstance(self.relative_block_position, Vec3V0) or not isinstance(
            self.relative_hit_position, Vec3V0
        ):
            raise ContractViolation("block ray hit requires relative positions")
        if self.face not in {"down", "up", "north", "south", "west", "east"}:
            raise ContractViolation("block ray face is invalid")
        if self.block_id is None:
            raise ContractViolation("block ray hit requires block id")
        require_identifier(self.block_id, "block id")
        if self.fluid_id is not None:
            require_identifier(self.fluid_id, "fluid id")
        if type(self.state_properties) is not tuple:
            raise ContractViolation("block state properties must be a tuple")
        for name, value in self.state_properties:
            require_field_name(name, "block state property")
            if not isinstance(value, str) or not value:
                raise ContractViolation("block state property value is invalid")
        if self.collision_shape not in {"empty", "partial", "solid"}:
            raise ContractViolation("block collision shape summary is invalid")
        for name, value in (("block light", self.block_light), ("sky light", self.sky_light)):
            if type(value) is not int or not 0 <= value <= 15:
                raise ContractViolation(f"{name} must be between zero and fifteen")


@dataclass(frozen=True, slots=True)
class BodyContactV2:
    relative_block_position: Vec3V0
    block_id: str
    fluid_id: str | None
    collision_shape: str

    def __post_init__(self) -> None:
        if not isinstance(self.relative_block_position, Vec3V0):
            raise ContractViolation("body contact relative position is invalid")
        require_identifier(self.block_id, "body contact block id")
        if self.fluid_id is not None:
            require_identifier(self.fluid_id, "body contact fluid id")
        if self.collision_shape not in {"empty", "partial", "solid"}:
            raise ContractViolation("body contact collision shape is invalid")


@dataclass(frozen=True, slots=True)
class VisibleItemV2:
    """Appearance identity only; never an inspectable inventory stack."""

    empty: bool
    item_id: str | None

    def __post_init__(self) -> None:
        _require_bool(self.empty, "visible item empty")
        if self.empty:
            if self.item_id is not None:
                raise ContractViolation("empty visible item must not carry identity")
        else:
            require_identifier(self.item_id, "visible item id")


@dataclass(frozen=True, slots=True)
class VisibleEntityV2:
    track_id: str
    entity_type: str
    display_name: str | None
    relative_position: Vec3V0
    relative_velocity: Vec3V0
    relative_yaw_degrees: float
    pitch_degrees: float
    bounding_box_size: Vec3V0
    pose: str
    is_on_ground: bool
    equipment: tuple[tuple[str, VisibleItemV2], ...]
    hurt_animation_ticks: int | None = None

    def __post_init__(self) -> None:
        require_identifier(self.track_id, "entity track id")
        require_identifier(self.entity_type, "entity type")
        if self.display_name is not None and not self.display_name:
            raise ContractViolation("entity display name cannot be empty")
        for name in ("relative_position", "relative_velocity", "bounding_box_size"):
            if not isinstance(getattr(self, name), Vec3V0):
                raise ContractViolation(f"entity {name} is invalid")
        require_finite(self.relative_yaw_degrees, "entity relative yaw")
        require_finite(self.pitch_degrees, "entity pitch")
        if self.pose not in _POSES:
            raise ContractViolation("entity pose is invalid")
        _require_bool(self.is_on_ground, "entity on ground")
        if self.hurt_animation_ticks is not None:
            if (type(self.hurt_animation_ticks) is not int
                    or not 0 <= self.hurt_animation_ticks <= 20):
                raise ContractViolation("entity hurt animation ticks must be between zero and 20")
        if type(self.equipment) is not tuple:
            raise ContractViolation("entity equipment must be a tuple")
        valid_slots = {"main_hand", "off_hand", "head", "chest", "legs", "feet"}
        seen: set[str] = set()
        for slot, stack in self.equipment:
            if slot not in valid_slots or slot in seen or type(stack) is not VisibleItemV2:
                raise ContractViolation("entity equipment entry is invalid")
            seen.add(slot)


@dataclass(frozen=True, slots=True)
class PerceptionStateV2:
    horizontal_fov_degrees: float
    vertical_fov_degrees: float
    ray_columns: int
    ray_rows: int
    max_block_distance: float
    body_expansion_blocks: float
    block_epsilon_blocks: float
    entity_max_distance: float
    entity_occlusion_epsilon_blocks: float
    block_rays: tuple[BlockRayV2, ...]
    body_contacts: tuple[BodyContactV2, ...]
    visible_entities: tuple[VisibleEntityV2, ...]
    entities_truncated: bool
    truncated_entity_count: int
    sensor_profile_revision: int = 1  # Missing in historical 90x60 payloads only.

    @property
    def center_ray(self) -> BlockRayV2:
        return self.block_rays[(self.ray_rows // 2) * self.ray_columns + self.ray_columns // 2]

    def __post_init__(self) -> None:
        if type(self.sensor_profile_revision) is not int or self.sensor_profile_revision not in (1, 2, 3):
            raise ContractViolation("perception sensor profile revision is invalid")
        horizontal, vertical = (90.0, 60.0) if self.sensor_profile_revision == 1 else (120.0, 120.0)
        columns = 159 if self.sensor_profile_revision == 3 else 15
        ray_count = columns * 9
        expected = (horizontal, vertical, columns, 9, 16.0, 0.05, 0.001, 32.0, 0.05)
        actual = (
            self.horizontal_fov_degrees,
            self.vertical_fov_degrees,
            self.ray_columns,
            self.ray_rows,
            self.max_block_distance,
            self.body_expansion_blocks,
            self.block_epsilon_blocks,
            self.entity_max_distance,
            self.entity_occlusion_epsilon_blocks,
        )
        if actual != expected:
            raise ContractViolation("perception sensor profile is invalid")
        if type(self.block_rays) is not tuple or len(self.block_rays) != ray_count:
            raise ContractViolation(f"perception requires exactly {ray_count} block rays")
        expected_ids = tuple(range(ray_count))
        if tuple(ray.ray_id for ray in self.block_rays) != expected_ids:
            raise ContractViolation("block ray ids must be continuous and ordered")
        for ray in self.block_rays:
            if not isinstance(ray, BlockRayV2):
                raise ContractViolation("block rays must be BlockRayV2")
            expected_row, expected_column = divmod(ray.ray_id, columns)
            if (ray.row, ray.column) != (expected_row, expected_column):
                raise ContractViolation("block ray grid coordinates are invalid")
            expected_yaw = -horizontal / 2 + expected_column * (horizontal / (columns - 1))
            expected_pitch = -vertical / 2 + expected_row * (vertical / 8.0)
            if (
                abs(ray.yaw_offset_degrees - expected_yaw) > 1e-9
                or abs(ray.pitch_offset_degrees - expected_pitch) > 1e-9
            ):
                raise ContractViolation("block ray angle offsets are invalid")
            if ray.distance_blocks > 16.0:
                raise ContractViolation("block ray distance exceeds sensor range")
        if type(self.body_contacts) is not tuple:
            raise ContractViolation("body contacts must be a tuple")
        if not all(isinstance(item, BodyContactV2) for item in self.body_contacts):
            raise ContractViolation("body contact entry is invalid")
        if type(self.visible_entities) is not tuple or len(self.visible_entities) > 64:
            raise ContractViolation("visible entities cannot exceed 64")
        if not all(isinstance(item, VisibleEntityV2) for item in self.visible_entities):
            raise ContractViolation("visible entity entry is invalid")
        track_ids = tuple(item.track_id for item in self.visible_entities)
        if len(set(track_ids)) != len(track_ids):
            raise ContractViolation("visible entity track ids must be unique")
        entity_order = tuple(
            (
                item.relative_position.x * item.relative_position.x
                + item.relative_position.y * item.relative_position.y
                + item.relative_position.z * item.relative_position.z,
                item.entity_type,
                item.track_id,
            )
            for item in self.visible_entities
        )
        if entity_order != tuple(sorted(entity_order)):
            raise ContractViolation(
                "visible entities must be sorted by distance, type, and track id"
            )
        _require_bool(self.entities_truncated, "entities truncated")
        require_nonnegative_int(self.truncated_entity_count, "truncated entity count")
        if self.entities_truncated != (self.truncated_entity_count > 0):
            raise ContractViolation("entity truncation flag and count disagree")


@dataclass(frozen=True, slots=True)
class ClientSampleTimingV2:
    """JVM-local elapsed nanoseconds; never comparable to the controller clock."""

    clock_id: str
    started_at_monotonic_ns: int
    completed_at_monotonic_ns: int

    def __post_init__(self) -> None:
        require_identifier(self.clock_id, "client sample clock id")
        require_nonnegative_int(self.started_at_monotonic_ns, "client sample start")
        require_nonnegative_int(self.completed_at_monotonic_ns, "client sample completion")
        if self.completed_at_monotonic_ns < self.started_at_monotonic_ns:
            raise ContractViolation("client sample completion precedes start")


@dataclass(frozen=True, slots=True)
class ObservationSnapshotV2:
    episode_id: str
    sequence_id: int
    request_sequence_id: int | None
    request_started_at_monotonic_ns: int
    received_at_monotonic_ns: int
    controller_clock_id: str
    client_sample: ClientSampleTimingV2
    world_time_ticks: FieldValueV0[int]
    position: FieldValueV0[Vec3V0]
    yaw_degrees: FieldValueV0[float]
    pitch_degrees: FieldValueV0[float]
    is_on_ground: FieldValueV0[bool]
    is_dead: FieldValueV0[bool]
    health_points: FieldValueV0[float]
    food_points: FieldValueV0[float]
    self_state: ObservationGroupV2[SelfStateV2]
    inventory: ObservationGroupV2[InventoryStateV2]
    gui: ObservationGroupV2[GuiStateV2]
    perception: ObservationGroupV2[PerceptionStateV2]
    source_backend: str
    server_state_age_ns: FieldValueV0[int] = field(
        default_factory=lambda: FieldValueV0.missing("server_state_age_unknown")
    )
    privileged_fields_present: tuple[str, ...] = ()
    schema_version: str = field(default="mc2p.observation.v2", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.episode_id, "episode_id")
        require_identifier(self.source_backend, "source_backend")
        require_nonnegative_int(self.sequence_id, "sequence_id")
        if self.request_sequence_id is not None:
            require_nonnegative_int(self.request_sequence_id, "request_sequence_id")
        require_nonnegative_int(self.request_started_at_monotonic_ns, "request start time")
        require_nonnegative_int(self.received_at_monotonic_ns, "received time")
        if self.received_at_monotonic_ns < self.request_started_at_monotonic_ns:
            raise ContractViolation("received time cannot precede request start time")
        require_identifier(self.controller_clock_id, "controller clock id")
        if type(self.client_sample) is not ClientSampleTimingV2:
            raise ContractViolation("client sample must carry a typed JVM-local interval")
        if self.controller_clock_id == self.client_sample.clock_id:
            raise ContractViolation("controller and client clocks must have independent identities")
        if self.server_state_age_ns != FieldValueV0.missing("server_state_age_unknown"):
            raise ContractViolation("server state age is unknown in this contract revision")
        if (
            self.world_time_ticks.status is not FieldStatusV0.VALID
            or type(self.world_time_ticks.value) is not int
        ):
            raise ContractViolation("V2 world time must be valid")
        tick = self.world_time_ticks.value
        require_nonnegative_int(tick, "world time")
        groups = (self.self_state, self.inventory, self.gui, self.perception)
        if not all(isinstance(group, ObservationGroupV2) for group in groups):
            raise ContractViolation("V2 observation groups are invalid")
        if any(group.sample_world_tick != tick for group in groups):
            raise ContractViolation("observation group sample world tick must match snapshot")
        if self.self_state.status is FieldStatusV0.VALID:
            value = self.self_state.value
            if not isinstance(value, SelfStateV2):
                raise ContractViolation("valid self group must carry SelfStateV2")
            expected = (
                ("position", self.position, value.position),
                ("yaw", self.yaw_degrees, value.yaw_degrees),
                ("pitch", self.pitch_degrees, value.pitch_degrees),
                ("is_on_ground", self.is_on_ground, value.is_on_ground),
                ("is_dead", self.is_dead, value.is_dead),
                ("health", self.health_points, value.health_points),
                ("food", self.food_points, float(value.food_points)),
            )
            for name, wrapped, projected in expected:
                if wrapped.status is not FieldStatusV0.VALID or wrapped.value != projected:
                    raise ContractViolation(f"top-level {name} disagrees with self state")
        if type(self.privileged_fields_present) is not tuple:
            raise ContractViolation("privileged fields must be a tuple")
        for name in self.privileged_fields_present:
            require_field_name(name, "privileged field")
        if self.privileged_fields_present != tuple(
            sorted(set(self.privileged_fields_present))
        ):
            raise ContractViolation("privileged fields must be sorted and unique")
