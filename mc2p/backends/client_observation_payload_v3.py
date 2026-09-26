"""Strict V3 wire decoding, independent of transport and the old ray envelope.

The V2 module supplies only the unchanged self/inventory/GUI/entity grammars and
JSON primitives. Neither its V2 envelope decoder nor ray parser is called here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from mc2p.backends import client_observation_payload as _v2
from mc2p.backends.client_observation_payload import ClientObservationPayloadError
from mc2p.contracts.common import ContractViolation, FieldValueV0, require_nonnegative_int
from mc2p.contracts.observation_v2 import (
    ClientSampleTimingV2, GuiStateV2, InventoryStateV2, ObservationGroupV2, SelfStateV2,
)
from mc2p.contracts.observation_v3 import (
    MAX_BLOCKS_V3, AabbV3, CollisionShapeV3, DamageEventV3,
    ObservedBlockV3, ObservationSnapshotV3,
    PerceptionStateV3, TargetingStateV3, TrackedEntityStateV3, validate_v3_groups,
)


MAX_CLIENT_OBSERVATION_BYTES_V3 = 1_048_576


@dataclass(frozen=True, slots=True)
class ClientObservationPayloadV3:
    generation_id: int
    sample_world_tick: int
    client_sample: ClientSampleTimingV2
    self_state: ObservationGroupV2[SelfStateV2]
    inventory: ObservationGroupV2[InventoryStateV2]
    gui: ObservationGroupV2[GuiStateV2]
    perception: ObservationGroupV2[PerceptionStateV3]
    field_profile: str
    targeting: ObservationGroupV2[TargetingStateV3]
    tracked_entity: ObservationGroupV2[TrackedEntityStateV3]
    damage_events: tuple[DamageEventV3, ...]
    damage_events_dropped: int
    schema_version: str = field(default="mc2p.client_observation.v3", init=False)

    def __post_init__(self) -> None:
        require_nonnegative_int(self.generation_id, "generation id")
        if type(self.client_sample) is not ClientSampleTimingV2:
            raise ContractViolation("client sample must carry typed JVM-local timing")
        validate_v3_groups(self.sample_world_tick, self.field_profile, self.self_state,
                           self.inventory, self.gui, self.perception, self.targeting,
                           self.tracked_entity)
        if (type(self.damage_events) is not tuple
                or len(self.damage_events) > 64
                or any(type(event) is not DamageEventV3
                       for event in self.damage_events)):
            raise ContractViolation("damage events must be a bounded typed tuple")
        event_ids = tuple(event.event_sequence_id for event in self.damage_events)
        if event_ids != tuple(sorted(set(event_ids))):
            raise ContractViolation("damage events must be ordered and unique")
        require_nonnegative_int(
            self.damage_events_dropped, "dropped damage event count",
        )


def _grid(value: Any) -> tuple[int, int, int]:
    if type(value) is not list or len(value) != 3:
        raise ClientObservationPayloadError("block position must be an integer triple")
    return tuple(_v2._integer(n, "block grid coordinate") for n in value)


def _collision(value: Any) -> CollisionShapeV3:
    item = _v2._object(value, {"kind", "boxes", "reason"}, "collision")
    boxes = []
    for raw in _v2._array(item["boxes"], "collision boxes"):
        if type(raw) is not list or len(raw) != 6:
            raise ClientObservationPayloadError("collision box must contain six coordinates")
        boxes.append(AabbV3(*(_v2._number(n, "collision coordinate") for n in raw)))
    return CollisionShapeV3(_v2._string(item["kind"], "collision kind"), tuple(boxes),
                            _v2._optional_string(item["reason"], "collision reason"))


def _block(value: Any) -> ObservedBlockV3:
    item = _v2._object(value, {"position", "block_id", "collision", "fluid_id", "sources"}, "block")
    return ObservedBlockV3(_grid(item["position"]), _v2._string(item["block_id"], "block id"),
        _collision(item["collision"]), _v2._optional_string(item["fluid_id"], "fluid id"),
        tuple(_v2._string(s, "block source") for s in _v2._array(item["sources"], "block sources")))


def _targeting(value: Any) -> TargetingStateV3:
    item = _v2._object(value, {"hit_kind", "block_position", "entity_ref", "face", "hit_position", "distance_blocks"}, "targeting")
    return TargetingStateV3(_v2._string(item["hit_kind"], "targeting hit kind"),
        None if item["block_position"] is None else _grid(item["block_position"]),
        _v2._optional_string(item["entity_ref"], "target entity reference"),
        _v2._optional_string(item["face"], "target face"),
        None if item["hit_position"] is None else _v2._vec(item["hit_position"], "target hit position"),
        None if item["distance_blocks"] is None else _v2._number(item["distance_blocks"], "target distance"))


def _tracked_entity(value: Any) -> TrackedEntityStateV3:
    item = _v2._object(value, {"track_id", "entity_type", "relative_position", "relative_velocity",
        "relative_yaw_degrees", "pitch_degrees", "bounding_box_size", "pose", "is_on_ground",
        "is_loaded", "is_dead", "health_points", "max_health_points"}, "tracked entity")
    return TrackedEntityStateV3(
        track_id=_v2._string(item["track_id"], "tracked entity id"),
        entity_type=_v2._string(item["entity_type"], "tracked entity type"),
        relative_position=_v2._vec(item["relative_position"], "tracked entity relative position"),
        relative_velocity=_v2._vec(item["relative_velocity"], "tracked entity relative velocity"),
        relative_yaw_degrees=_v2._number(item["relative_yaw_degrees"], "tracked entity relative yaw"),
        pitch_degrees=_v2._number(item["pitch_degrees"], "tracked entity pitch"),
        bounding_box_size=_v2._vec(item["bounding_box_size"], "tracked entity bounding box"),
        pose=_v2._string(item["pose"], "tracked entity pose"),
        is_on_ground=_v2._boolean(item["is_on_ground"], "tracked entity on ground"),
        is_loaded=_v2._boolean(item["is_loaded"], "tracked entity loaded"),
        is_dead=_v2._boolean(item["is_dead"], "tracked entity dead"),
        health_points=_v2._number(item["health_points"], "tracked entity health"),
        max_health_points=_v2._number(item["max_health_points"], "tracked entity max health"),
    )


def _damage_event(value: Any) -> DamageEventV3:
    item = _v2._object(value, {
        "event_sequence_id", "world_tick", "target_is_self",
        "target_entity_ref", "damage_type", "source_entity_present",
        "source_is_self", "source_entity_ref", "direct_entity_present",
        "direct_source_is_self", "direct_source_entity_ref",
    }, "damage event")
    return DamageEventV3(
        _v2._integer(item["event_sequence_id"], "damage event sequence"),
        _v2._integer(item["world_tick"], "damage event world tick"),
        _v2._boolean(item["target_is_self"], "damage target is self"),
        _v2._optional_string(item["target_entity_ref"], "damage target reference"),
        _v2._string(item["damage_type"], "damage type"),
        _v2._boolean(item["source_entity_present"], "damage source present"),
        _v2._boolean(item["source_is_self"], "damage source is self"),
        _v2._optional_string(item["source_entity_ref"], "damage source reference"),
        _v2._boolean(item["direct_entity_present"], "direct damage source present"),
        _v2._boolean(item["direct_source_is_self"], "direct damage source is self"),
        _v2._optional_string(
            item["direct_source_entity_ref"], "direct damage source reference",
        ),
    )


def _perception(value: Any) -> PerceptionStateV3:
    item = _v2._object(value, {"horizontal_fov_degrees", "vertical_fov_degrees", "ray_columns", "ray_rows",
        "max_block_distance", "body_expansion_blocks", "block_epsilon_blocks", "entity_max_distance",
        "entity_occlusion_epsilon_blocks", "blocks", "visible_entities", "entities_truncated",
        "truncated_entity_count", "sensor_profile_revision", "knowledge_model"}, "perception")
    raw_blocks = _v2._array(item["blocks"], "blocks")
    raw_entities = _v2._array(item["visible_entities"], "visible entities")
    if len(raw_blocks) > MAX_BLOCKS_V3 or len(raw_entities) > 64:
        raise ClientObservationPayloadError("observation collection budget exceeded")
    numbers = {key: _v2._number(item[key], key) for key in ("horizontal_fov_degrees", "vertical_fov_degrees",
        "max_block_distance", "body_expansion_blocks", "block_epsilon_blocks", "entity_max_distance",
        "entity_occlusion_epsilon_blocks")}
    return PerceptionStateV3(**numbers,
        ray_columns=_v2._integer(item["ray_columns"], "ray columns"),
        ray_rows=_v2._integer(item["ray_rows"], "ray rows"),
        sensor_profile_revision=_v2._integer(item["sensor_profile_revision"], "sensor profile"),
        knowledge_model=_v2._string(item["knowledge_model"], "knowledge model"),
        blocks=tuple(_block(b) for b in raw_blocks),
        visible_entities=tuple(_v2._visible_entity(e, i) for i, e in enumerate(raw_entities)),
        entities_truncated=_v2._boolean(item["entities_truncated"], "entities truncated"),
        truncated_entity_count=_v2._integer(item["truncated_entity_count"], "truncated entity count"))


def decode_client_observation_value_v3(value: dict) -> ClientObservationPayloadV3:
    """Validate an already strictly parsed JSON object without re-encoding it.

The enclosing transport MUST enforce its byte budget and reject duplicate JSON
keys/nonstandard constants before calling this entry. Lost wire syntax cannot
be recovered from a dict. Every field is still checked and copied to typed values.
"""
    try:
        item = _v2._object(value, {"schema_version", "generation_id", "sample_world_tick", "client_sample",
            "self_state", "inventory", "gui", "perception", "field_profile", "targeting",
            "tracked_entity", "damage_events", "damage_events_dropped"},
            "V3 client payload")
        if item["schema_version"] != "mc2p.client_observation.v3":
            raise ClientObservationPayloadError("invalid V3 client schema")
        timing = _v2._object(item["client_sample"], {"clock_id", "started_at_monotonic_ns", "completed_at_monotonic_ns"}, "client timing")
        return ClientObservationPayloadV3(
            generation_id=_v2._integer(item["generation_id"], "generation id"),
            sample_world_tick=_v2._integer(item["sample_world_tick"], "sample world tick"),
            client_sample=ClientSampleTimingV2(**timing),
            self_state=_v2._group(item["self_state"], "self_state", "client_player", _v2._self_state),
            inventory=_v2._group(item["inventory"], "inventory", "client_inventory", _v2._inventory),
            gui=_v2._group(item["gui"], "gui", "client_screen_handler", _v2._gui),
            perception=_v2._group(item["perception"], "perception", "client_perception_filtered", _perception),
            field_profile=_v2._string(item["field_profile"], "field profile"),
            targeting=_v2._group(item["targeting"], "targeting", "client_perception_filtered", _targeting),
            tracked_entity=_v2._group(item["tracked_entity"], "tracked_entity",
                                      "client_registered_entity", _tracked_entity),
            damage_events=tuple(
                _damage_event(event)
                for event in _v2._array(item["damage_events"], "damage events")
            ),
            damage_events_dropped=_v2._integer(
                item["damage_events_dropped"], "dropped damage events",
            ))
    except ClientObservationPayloadError:
        raise
    except (ContractViolation, ValueError, TypeError, OverflowError, RecursionError) as error:
        raise ClientObservationPayloadError("invalid V3 observation values") from error


def _reject_constant(value: str) -> None:
    raise ClientObservationPayloadError("non-finite JSON constant")


def decode_client_observation_payload_v3(payload: bytes) -> ClientObservationPayloadV3:
    """Decode one bounded UTF-8 JSON value; no coercion, fallback or field removal."""
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_CLIENT_OBSERVATION_BYTES_V3:
        raise ClientObservationPayloadError("V3 payload must be nonempty bytes within budget")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"),
                           object_pairs_hook=_v2._pairs_without_duplicates, parse_constant=_reject_constant)
    except ClientObservationPayloadError:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError) as error:
        raise ClientObservationPayloadError("invalid V3 UTF-8 JSON") from error
    return decode_client_observation_value_v3(value)


def snapshot_v3_from_payload(decoded: ClientObservationPayloadV3, *, episode_id: str,
        request_sequence_id: int | None, request_started_at_monotonic_ns: int,
        received_at_monotonic_ns: int, controller_clock_id: str, source_backend: str,
        privileged_fields_present: tuple[str, ...] = ()) -> ObservationSnapshotV3:
    """Bind a validated client sample to controller-owned identity and time."""
    if type(decoded) is not ClientObservationPayloadV3:
        raise ContractViolation("V3 projection requires exact ClientObservationPayloadV3")
    own = decoded.self_state.value
    projected = {}
    for name in ("position", "yaw_degrees", "pitch_degrees", "is_on_ground", "is_dead", "health_points", "food_points"):
        if own is None:
            projected[name] = FieldValueV0(decoded.self_state.status, None, decoded.self_state.reason_code)
        else:
            value = getattr(own, name)
            projected[name] = FieldValueV0.valid(float(value) if name == "food_points" else value)
    return ObservationSnapshotV3(**projected, episode_id=episode_id, sequence_id=decoded.generation_id,
        request_sequence_id=request_sequence_id, request_started_at_monotonic_ns=request_started_at_monotonic_ns,
        received_at_monotonic_ns=received_at_monotonic_ns, controller_clock_id=controller_clock_id,
        client_sample=decoded.client_sample, world_time_ticks=FieldValueV0.valid(decoded.sample_world_tick),
        self_state=decoded.self_state, inventory=decoded.inventory, gui=decoded.gui, perception=decoded.perception,
        targeting=decoded.targeting, tracked_entity=decoded.tracked_entity,
        damage_events=decoded.damage_events,
        damage_events_dropped=decoded.damage_events_dropped,
        field_profile=decoded.field_profile, source_backend=source_backend,
        privileged_fields_present=privileged_fields_present)
