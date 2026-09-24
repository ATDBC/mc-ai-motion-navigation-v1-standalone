"""Block-level observation values. A block is knowledge, not a visible surface.

These types validate representation and internal coherence, not the truth of a
client's visibility claim. The shared collector must enforce lawful discovery.
V2 value grammars for self, inventory, GUI and entities remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mc2p.contracts.common import (
    ContractViolation, FieldStatusV0, FieldValueV0, require_field_name,
    require_finite, require_identifier, require_nonnegative_int,
)
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_v2 import (
    ClientSampleTimingV2, GuiStateV2, InventoryStateV2, ObservationGroupV2,
    SelfStateV2, VisibleEntityV2, _POSES,
)


MAX_BLOCKS_V3 = 25000 + 512 + 512 + 1
_SOURCES = ("air_query", "body_contact", "current_target", "first_hit_ray", "surface_depth")
_FACES = ("down", "up", "north", "south", "west", "east")


def _finite(value: float, name: str) -> None:
    try:
        require_finite(value, name)
    except OverflowError as error:
        raise ContractViolation(name + " must be finite") from error


def _grid(value: tuple[int, int, int]) -> None:
    if type(value) is not tuple or len(value) != 3 or any(type(n) is not int for n in value):
        raise ContractViolation("block position must be an integer grid triple")


@dataclass(frozen=True, slots=True)
class AabbV3:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    def __post_init__(self) -> None:
        for n in _box_key(self):
            _finite(n, "collision coordinate")
        if not (self.min_x < self.max_x and self.min_y < self.max_y and self.min_z < self.max_z):
            raise ContractViolation("collision box must have positive volume")


def _box_key(box: AabbV3) -> tuple[float, ...]:
    return (box.min_x, box.min_y, box.min_z, box.max_x, box.max_y, box.max_z)


@dataclass(frozen=True, slots=True)
class CollisionShapeV3:
    kind: str
    boxes: tuple[AabbV3, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in ("empty", "full_cube", "boxes", "unsupported"):
            raise ContractViolation("invalid collision kind")
        if type(self.boxes) is not tuple or any(type(box) is not AabbV3 for box in self.boxes):
            raise ContractViolation("collision boxes must be a typed immutable tuple")
        if self.kind == "boxes":
            keys = tuple(_box_key(box) for box in self.boxes)
            if not keys or keys != tuple(sorted(set(keys))):
                raise ContractViolation("collision boxes must be nonempty, sorted and unique")
        elif self.boxes:
            raise ContractViolation("compact or unsupported shape cannot carry boxes")
        if self.kind == "unsupported":
            require_identifier(self.reason, "collision unavailable reason")
        elif self.reason is not None:
            raise ContractViolation("known collision shape cannot carry an unavailable reason")


@dataclass(frozen=True, slots=True)
class ObservedBlockV3:
    position: tuple[int, int, int]
    block_id: str
    collision: CollisionShapeV3
    fluid_id: str | None
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        _grid(self.position)
        require_identifier(self.block_id, "block id")
        if self.fluid_id is not None:
            require_identifier(self.fluid_id, "fluid id")
        if type(self.collision) is not CollisionShapeV3:
            raise ContractViolation("block collision must carry a typed shape")
        if (type(self.sources) is not tuple or not self.sources
                or any(type(s) is not str or s not in _SOURCES for s in self.sources)
                or self.sources != tuple(sorted(set(self.sources)))):
            raise ContractViolation("block sources must be lawful, sorted and unique")


@dataclass(frozen=True, slots=True)
class TargetingStateV3:
    hit_kind: str
    block_position: tuple[int, int, int] | None
    entity_ref: str | None
    face: str | None
    hit_position: Vec3V0 | None
    distance_blocks: float | None

    def __post_init__(self) -> None:
        if type(self.hit_kind) is not str or self.hit_kind not in ("block", "entity", "miss"):
            raise ContractViolation("invalid targeting hit kind")
        if self.hit_kind == "miss":
            if any(v is not None for v in (self.block_position, self.entity_ref, self.face,
                                          self.hit_position, self.distance_blocks)):
                raise ContractViolation("targeting miss cannot carry target parameters")
            return
        if type(self.hit_position) is not Vec3V0:
            raise ContractViolation("target hit must carry a typed world position")
        _finite(self.distance_blocks, "target distance")
        if self.distance_blocks < 0:
            raise ContractViolation("target distance cannot be negative")
        if self.hit_kind == "block":
            _grid(self.block_position)
            if self.entity_ref is not None or type(self.face) is not str or self.face not in _FACES:
                raise ContractViolation("block target must carry only block parameters")
        else:
            require_identifier(self.entity_ref, "target entity reference")
            if self.block_position is not None or self.face is not None:
                raise ContractViolation("entity target cannot carry block parameters")


@dataclass(frozen=True, slots=True)
class TrackedEntityStateV3:
    track_id: str
    entity_type: str
    relative_position: Vec3V0
    relative_velocity: Vec3V0
    relative_yaw_degrees: float
    pitch_degrees: float
    bounding_box_size: Vec3V0
    pose: str
    is_on_ground: bool
    is_loaded: bool
    is_dead: bool
    health_points: float
    max_health_points: float

    def __post_init__(self) -> None:
        require_identifier(self.track_id, "entity track id")
        require_identifier(self.entity_type, "entity type")
        for name in ("relative_position", "relative_velocity", "bounding_box_size"):
            if type(getattr(self, name)) is not Vec3V0:
                raise ContractViolation(f"tracked entity {name} is invalid")
        _finite(self.relative_yaw_degrees, "tracked entity relative yaw")
        _finite(self.pitch_degrees, "tracked entity pitch")
        if type(self.pose) is not str or self.pose not in _POSES:
            raise ContractViolation("tracked entity pose is invalid")
        for name in ("is_on_ground", "is_loaded", "is_dead"):
            if type(getattr(self, name)) is not bool:
                raise ContractViolation(f"tracked entity {name} must be boolean")
        if not self.is_loaded:
            raise ContractViolation("tracked entity must be loaded")
        _finite(self.health_points, "tracked entity health")
        _finite(self.max_health_points, "tracked entity max health")
        if self.max_health_points <= 0 or not 0 <= self.health_points <= self.max_health_points:
            raise ContractViolation("tracked entity health is outside its valid range")


@dataclass(frozen=True, slots=True)
class PerceptionStateV3:
    horizontal_fov_degrees: float
    vertical_fov_degrees: float
    ray_columns: int
    ray_rows: int
    max_block_distance: float
    body_expansion_blocks: float
    block_epsilon_blocks: float
    entity_max_distance: float
    entity_occlusion_epsilon_blocks: float
    blocks: tuple[ObservedBlockV3, ...]
    visible_entities: tuple[VisibleEntityV2, ...]
    entities_truncated: bool
    truncated_entity_count: int
    sensor_profile_revision: int = 3
    knowledge_model: str = "block_state_v1"

    def __post_init__(self) -> None:
        if type(self.sensor_profile_revision) is not int or self.sensor_profile_revision not in (3, 4):
            raise ContractViolation("invalid V3 sensor profile")
        surface = self.sensor_profile_revision == 4
        for name, expected in (("ray_columns", 0 if surface else 159), ("ray_rows", 0 if surface else 9)):
            if type(getattr(self, name)) is not int or getattr(self, name) != expected:
                raise ContractViolation("invalid V3 sensor grid/profile")
        for name, expected in (("horizontal_fov_degrees", 120.), ("vertical_fov_degrees", 120.),
                ("max_block_distance", 16.), ("body_expansion_blocks", .05), ("block_epsilon_blocks", .001),
                ("entity_max_distance", 32.), ("entity_occlusion_epsilon_blocks", .05)):
            value = getattr(self, name)
            _finite(value, name)
            if value != expected:
                raise ContractViolation("invalid V3 sensor geometry")
        if type(self.knowledge_model) is not str or self.knowledge_model != "block_state_v1":
            raise ContractViolation("invalid V3 knowledge model")
        if (type(self.blocks) is not tuple or len(self.blocks) > (MAX_BLOCKS_V3 if surface else 2456)
                or any(type(b) is not ObservedBlockV3 for b in self.blocks)):
            raise ContractViolation("invalid or excessive V3 blocks")
        positions = tuple(b.position for b in self.blocks)
        if positions != tuple(sorted(set(positions))):
            raise ContractViolation("block positions must be sorted and unique")
        for source, limit in (("first_hit_ray", 0 if surface else 1431), ("surface_depth", 25000 if surface else 0),
                              ("air_query", 512), ("body_contact", 512), ("current_target", 1)):
            if sum(source in b.sources for b in self.blocks) > limit:
                raise ContractViolation("block source budget exceeded")
        if (type(self.visible_entities) is not tuple or len(self.visible_entities) > 64
                or any(type(e) is not VisibleEntityV2 for e in self.visible_entities)):
            raise ContractViolation("invalid or excessive visible entities")
        ids = tuple(e.track_id for e in self.visible_entities)
        if len(set(ids)) != len(ids):
            raise ContractViolation("visible entity references must be unique")
        # Match the shared V2 ordering arithmetic (finite components may be very large).
        ordering = tuple((e.relative_position.x * e.relative_position.x
                          + e.relative_position.y * e.relative_position.y
                          + e.relative_position.z * e.relative_position.z,
                          e.entity_type, e.track_id) for e in self.visible_entities)
        if ordering != tuple(sorted(ordering)):
            raise ContractViolation("visible entities must be sorted by distance, type and reference")
        require_nonnegative_int(self.truncated_entity_count, "truncated entity count")
        if type(self.entities_truncated) is not bool or self.entities_truncated != (self.truncated_entity_count > 0):
            raise ContractViolation("invalid entity truncation metadata")


def validate_v3_groups(tick: int, field_profile: str, self_state: ObservationGroupV2[SelfStateV2],
                       inventory: ObservationGroupV2[InventoryStateV2], gui: ObservationGroupV2[GuiStateV2],
                       perception: ObservationGroupV2[PerceptionStateV3],
                       targeting: ObservationGroupV2[TargetingStateV3],
                       tracked_entity: ObservationGroupV2[TrackedEntityStateV3]) -> None:
    """Shared checks for decoded wire values and independently constructed snapshots."""
    require_nonnegative_int(tick, "sample world tick")
    ObservationRequestV3(field_profile)
    for group, source, cls in ((self_state, "client_player", SelfStateV2),
            (inventory, "client_inventory", InventoryStateV2), (gui, "client_screen_handler", GuiStateV2),
            (perception, "client_perception_filtered", PerceptionStateV3),
            (targeting, "client_perception_filtered", TargetingStateV3),
            (tracked_entity, "client_registered_entity", TrackedEntityStateV3)):
        if type(group) is not ObservationGroupV2 or group.source_kind != source or group.sample_world_tick != tick:
            raise ContractViolation("invalid observation group source or sample tick")
        if group.status is FieldStatusV0.VALID and type(group.value) is not cls:
            raise ContractViolation("invalid observation group value type")
    if field_profile == "navigation_v1":
        if targeting.status is not FieldStatusV0.MISSING or targeting.reason_code != "not_requested":
            raise ContractViolation("navigation targeting must be not_requested")
    elif (targeting.status not in (FieldStatusV0.VALID, FieldStatusV0.MISSING, FieldStatusV0.UNSUPPORTED)
          or targeting.reason_code == "not_requested"):
        raise ContractViolation("interaction targeting must report current query availability")
    blocks = () if perception.value is None else perception.value.blocks
    target_blocks = tuple(b.position for b in blocks if "current_target" in b.sources)
    target = targeting.value
    if target is not None and target.hit_kind == "block":
        if target_blocks != (target.block_position,):
            raise ContractViolation("target block must match current_target knowledge")
    elif target_blocks:
        raise ContractViolation("current_target block source has no matching target")
    if target is not None and target.hit_kind == "entity":
        entities = () if perception.value is None else perception.value.visible_entities
        if target.entity_ref not in {e.track_id for e in entities}:
            raise ContractViolation("target entity must have a currently visible reference")


@dataclass(frozen=True, slots=True)
class ObservationSnapshotV3:
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
    perception: ObservationGroupV2[PerceptionStateV3]
    source_backend: str
    field_profile: str
    targeting: ObservationGroupV2[TargetingStateV3]
    tracked_entity: ObservationGroupV2[TrackedEntityStateV3]
    server_state_age_ns: FieldValueV0[int] = field(default_factory=lambda: FieldValueV0.missing("server_state_age_unknown"))
    privileged_fields_present: tuple[str, ...] = ()
    schema_version: str = field(default="mc2p.observation.v3", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.episode_id, "episode id")
        require_identifier(self.source_backend, "source backend")
        require_identifier(self.controller_clock_id, "controller clock id")
        require_nonnegative_int(self.sequence_id, "sequence id")
        if self.request_sequence_id is not None:
            require_nonnegative_int(self.request_sequence_id, "request sequence id")
        require_nonnegative_int(self.request_started_at_monotonic_ns, "request start")
        require_nonnegative_int(self.received_at_monotonic_ns, "received time")
        if self.received_at_monotonic_ns < self.request_started_at_monotonic_ns:
            raise ContractViolation("received time precedes request start")
        if type(self.client_sample) is not ClientSampleTimingV2:
            raise ContractViolation("client sample must carry typed JVM-local timing")
        if self.client_sample.clock_id == self.controller_clock_id:
            raise ContractViolation("controller and JVM clock identities must be independent")
        if self.server_state_age_ns != FieldValueV0.missing("server_state_age_unknown"):
            raise ContractViolation("server state age is unknown")
        if type(self.world_time_ticks) is not FieldValueV0 or self.world_time_ticks.status is not FieldStatusV0.VALID:
            raise ContractViolation("world time must be valid")
        validate_v3_groups(self.world_time_ticks.value, self.field_profile, self.self_state,
                           self.inventory, self.gui, self.perception, self.targeting,
                           self.tracked_entity)
        own = self.self_state.value
        for name, expected_type in (("position", Vec3V0), ("yaw_degrees", (int, float)),
                ("pitch_degrees", (int, float)), ("is_on_ground", bool), ("is_dead", bool),
                ("health_points", (int, float)), ("food_points", (int, float))):
            wrapped = getattr(self, name)
            if type(wrapped) is not FieldValueV0:
                raise ContractViolation("invalid top-level self field wrapper")
            if own is None:
                if wrapped != FieldValueV0(self.self_state.status, None, self.self_state.reason_code):
                    raise ContractViolation("missing self projection must preserve availability")
            elif (wrapped.status is not FieldStatusV0.VALID or wrapped.value != getattr(own, name)
                  or type(wrapped.value) not in (expected_type if isinstance(expected_type, tuple) else (expected_type,))):
                raise ContractViolation("top-level self projection disagrees with self state")
        if type(self.privileged_fields_present) is not tuple:
            raise ContractViolation("privileged fields must be an immutable tuple")
        for name in self.privileged_fields_present:
            require_field_name(name, "privileged field")
        if self.privileged_fields_present != tuple(sorted(set(self.privileged_fields_present))):
            raise ContractViolation("privileged fields must be sorted and unique")
