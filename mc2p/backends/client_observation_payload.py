"""Strict extraction and decoding helpers for client observation payloads."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping, TypeVar

from mc2p.contracts.common import ContractViolation, FieldStatusV0, FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import (
    ClientSampleTimingV2,
    BlockRayV2,
    BodyContactV2,
    GuiSlotV2,
    GuiStateV2,
    InventoryStateV2,
    ItemStackV2,
    ObservationGroupV2,
    ObservationSnapshotV2,
    PerceptionStateV2,
    SelfStateV2,
    StatusEffectV2,
    VisibleEntityV2,
    VisibleItemV2,
)


class ClientObservationPayloadError(ValueError):
    """Raised when the V2 client payload envelope is malformed."""


@dataclass(frozen=True, slots=True)
class ClientObservationPayloadV2:
    generation_id: int
    sample_world_tick: int
    client_sample: ClientSampleTimingV2
    self_state: ObservationGroupV2[SelfStateV2]
    inventory: ObservationGroupV2[InventoryStateV2]
    gui: ObservationGroupV2[GuiStateV2]
    perception: ObservationGroupV2[PerceptionStateV2]
    schema_version: str = "mc2p.client_observation.v2"


U = TypeVar("U")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ClientObservationPayloadError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _object(value: Any, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ClientObservationPayloadError(f"{name} must be an object")
    actual = set(value)
    if actual != keys:
        raise ClientObservationPayloadError(
            f"{name} keys are invalid: missing={sorted(keys - actual)}, "
            f"extra={sorted(actual - keys)}"
        )
    return value


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ClientObservationPayloadError(f"{name} must be an integer")
    return value


def _number(value: Any, name: str) -> float:
    if type(value) not in (int, float):
        raise ClientObservationPayloadError(f"{name} must be a number")
    return float(value)


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ClientObservationPayloadError(f"{name} must be bool")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ClientObservationPayloadError(f"{name} must be a string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _optional_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, name)


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ClientObservationPayloadError(f"{name} must be an array")
    return value


def _vec(value: Any, name: str) -> Vec3V0:
    item = _object(value, {"x", "y", "z"}, name)
    return Vec3V0(
        _number(item["x"], f"{name}.x"),
        _number(item["y"], f"{name}.y"),
        _number(item["z"], f"{name}.z"),
    )


_ITEM_KEYS = {
    "empty",
    "item_id",
    "count",
    "damage",
    "max_damage",
    "damageable",
    "custom_name",
}


def _item_stack(value: Any, name: str) -> ItemStackV2:
    item = _object(value, _ITEM_KEYS, name)
    return ItemStackV2(
        empty=_boolean(item["empty"], f"{name}.empty"),
        item_id=_optional_string(item["item_id"], f"{name}.item_id"),
        count=_integer(item["count"], f"{name}.count"),
        damage=_integer(item["damage"], f"{name}.damage"),
        max_damage=_integer(item["max_damage"], f"{name}.max_damage"),
        damageable=_boolean(item["damageable"], f"{name}.damageable"),
        custom_name=_optional_string(item["custom_name"], f"{name}.custom_name"),
    )


def _status_effect(value: Any, name: str) -> StatusEffectV2:
    item = _object(
        value,
        {
            "effect_id",
            "amplifier",
            "duration_ticks",
            "ambient",
            "show_particles",
            "show_icon",
        },
        name,
    )
    return StatusEffectV2(
        effect_id=_string(item["effect_id"], f"{name}.effect_id"),
        amplifier=_integer(item["amplifier"], f"{name}.amplifier"),
        duration_ticks=_integer(item["duration_ticks"], f"{name}.duration_ticks"),
        ambient=_boolean(item["ambient"], f"{name}.ambient"),
        show_particles=_boolean(
            item["show_particles"], f"{name}.show_particles"
        ),
        show_icon=_boolean(item["show_icon"], f"{name}.show_icon"),
    )


_SELF_KEYS = {
    "position",
    "velocity",
    "yaw_degrees",
    "pitch_degrees",
    "head_yaw_degrees",
    "body_yaw_degrees",
    "is_dead",
    "is_on_ground",
    "horizontal_collision",
    "vertical_collision",
    "pose",
    "is_sprinting",
    "is_sneaking",
    "is_swimming",
    "is_submerged_in_water",
    "is_climbing",
    "is_fall_flying",
    "is_burning",
    "fall_distance_blocks",
    "air_ticks",
    "max_air_ticks",
    "health_points",
    "max_health_points",
    "absorption_points",
    "armor_points",
    "food_points",
    "saturation_points",
    "experience_level",
    "experience_progress",
    "total_experience",
    "attack_cooldown",
    "active_hand",
    "is_using_item",
    "item_use_ticks_remaining",
    "status_effects",
    "game_mode",
    "is_flying",
    "allow_flying",
}


def _self_state(value: Any) -> SelfStateV2:
    item = _object(value, _SELF_KEYS, "self_state.value")
    return SelfStateV2(
        position=_vec(item["position"], "self_state.position"),
        velocity=_vec(item["velocity"], "self_state.velocity"),
        yaw_degrees=_number(item["yaw_degrees"], "self_state.yaw"),
        pitch_degrees=_number(item["pitch_degrees"], "self_state.pitch"),
        head_yaw_degrees=_number(item["head_yaw_degrees"], "self_state.head_yaw"),
        body_yaw_degrees=_number(item["body_yaw_degrees"], "self_state.body_yaw"),
        is_dead=_boolean(item["is_dead"], "self_state.is_dead"),
        is_on_ground=_boolean(item["is_on_ground"], "self_state.on_ground"),
        horizontal_collision=_boolean(
            item["horizontal_collision"], "self_state.horizontal_collision"
        ),
        vertical_collision=_boolean(
            item["vertical_collision"], "self_state.vertical_collision"
        ),
        pose=_string(item["pose"], "self_state.pose"),
        is_sprinting=_boolean(item["is_sprinting"], "self_state.is_sprinting"),
        is_sneaking=_boolean(item["is_sneaking"], "self_state.is_sneaking"),
        is_swimming=_boolean(item["is_swimming"], "self_state.is_swimming"),
        is_submerged_in_water=_boolean(
            item["is_submerged_in_water"], "self_state.is_submerged_in_water"
        ),
        is_climbing=_boolean(item["is_climbing"], "self_state.is_climbing"),
        is_fall_flying=_boolean(
            item["is_fall_flying"], "self_state.is_fall_flying"
        ),
        is_burning=_boolean(item["is_burning"], "self_state.is_burning"),
        fall_distance_blocks=_number(
            item["fall_distance_blocks"], "self_state.fall_distance"
        ),
        air_ticks=_integer(item["air_ticks"], "self_state.air_ticks"),
        max_air_ticks=_integer(item["max_air_ticks"], "self_state.max_air_ticks"),
        health_points=_number(item["health_points"], "self_state.health"),
        max_health_points=_number(
            item["max_health_points"], "self_state.max_health"
        ),
        absorption_points=_number(
            item["absorption_points"], "self_state.absorption"
        ),
        armor_points=_integer(item["armor_points"], "self_state.armor"),
        food_points=_integer(item["food_points"], "self_state.food"),
        saturation_points=_number(
            item["saturation_points"], "self_state.saturation"
        ),
        experience_level=_integer(
            item["experience_level"], "self_state.experience_level"
        ),
        experience_progress=_number(
            item["experience_progress"], "self_state.experience_progress"
        ),
        total_experience=_integer(
            item["total_experience"], "self_state.total_experience"
        ),
        attack_cooldown=_number(
            item["attack_cooldown"], "self_state.attack_cooldown"
        ),
        active_hand=_optional_string(item["active_hand"], "self_state.active_hand"),
        is_using_item=_boolean(item["is_using_item"], "self_state.is_using_item"),
        item_use_ticks_remaining=_integer(
            item["item_use_ticks_remaining"], "self_state.item_use_ticks_remaining"
        ),
        status_effects=tuple(
            _status_effect(effect, f"self_state.status_effects[{index}]")
            for index, effect in enumerate(
                _array(item["status_effects"], "self_state.status_effects")
            )
        ),
        game_mode=_string(item["game_mode"], "self_state.game_mode"),
        is_flying=_boolean(item["is_flying"], "self_state.is_flying"),
        allow_flying=_boolean(item["allow_flying"], "self_state.allow_flying"),
    )


_INVENTORY_KEYS = {
    "main",
    "selected_hotbar_slot",
    "head",
    "chest",
    "legs",
    "feet",
    "offhand",
    "main_hand",
}


def _inventory(value: Any) -> InventoryStateV2:
    item = _object(value, _INVENTORY_KEYS, "inventory.value")
    main = _array(item["main"], "inventory.main")
    return InventoryStateV2(
        main=tuple(
            _item_stack(stack, f"inventory.main[{index}]")
            for index, stack in enumerate(main)
        ),
        selected_hotbar_slot=_integer(
            item["selected_hotbar_slot"], "inventory.selected_hotbar_slot"
        ),
        head=_item_stack(item["head"], "inventory.head"),
        chest=_item_stack(item["chest"], "inventory.chest"),
        legs=_item_stack(item["legs"], "inventory.legs"),
        feet=_item_stack(item["feet"], "inventory.feet"),
        offhand=_item_stack(item["offhand"], "inventory.offhand"),
        main_hand=_item_stack(item["main_hand"], "inventory.main_hand"),
    )


def _gui_slot(value: Any, index: int) -> GuiSlotV2:
    name = f"gui.slots[{index}]"
    item = _object(
        value,
        {"slot_id", "x", "y", "source_kind", "source_index", "item", "enabled", "can_take"},
        name,
    )
    return GuiSlotV2(
        slot_id=_integer(item["slot_id"], f"{name}.slot_id"),
        x=_integer(item["x"], f"{name}.x"),
        y=_integer(item["y"], f"{name}.y"),
        source_kind=_string(item["source_kind"], f"{name}.source_kind"),
        source_index=_optional_integer(item["source_index"], f"{name}.source_index"),
        item=_item_stack(item["item"], f"{name}.item"),
        enabled=_boolean(item["enabled"], f"{name}.enabled"),
        can_take=_boolean(item["can_take"], f"{name}.can_take"),
    )


def _gui(value: Any) -> GuiStateV2:
    item = _object(
        value,
        {
            "open", "screen_kind", "handler_type", "sync_id", "revision", "title",
            "slots", "cursor_stack", "properties", "focused_slot_id",
            "gui_session_id",
            "properties_status", "properties_reason_code",
        },
        "gui.value",
    )
    properties: list[tuple[int, int]] = []
    for index, raw in enumerate(_array(item["properties"], "gui.properties")):
        entry = _object(raw, {"property_id", "value"}, f"gui.properties[{index}]")
        properties.append(
            (
                _integer(entry["property_id"], "gui property id"),
                _integer(entry["value"], "gui property value"),
            )
        )
    return GuiStateV2(
        open=_boolean(item["open"], "gui.open"),
        gui_session_id=_optional_string(item["gui_session_id"], "gui.gui_session_id"),
        screen_kind=_string(item["screen_kind"], "gui.screen_kind"),
        handler_type=_optional_string(item["handler_type"], "gui.handler_type"),
        sync_id=_optional_integer(item["sync_id"], "gui.sync_id"),
        revision=_optional_integer(item["revision"], "gui.revision"),
        title=_optional_string(item["title"], "gui.title"),
        slots=tuple(
            _gui_slot(slot, index)
            for index, slot in enumerate(_array(item["slots"], "gui.slots"))
        ),
        cursor_stack=_item_stack(item["cursor_stack"], "gui.cursor_stack"),
        properties=tuple(properties),
        properties_status=_string(item["properties_status"], "gui.properties_status"),
        properties_reason_code=_optional_string(item["properties_reason_code"], "gui.properties_reason_code"),
        focused_slot_id=_optional_integer(
            item["focused_slot_id"], "gui.focused_slot_id"
        ),
    )


_RAY_KEYS = {
    "ray_id", "row", "column", "yaw_offset_degrees", "pitch_offset_degrees",
    "hit_kind", "distance_blocks", "relative_block_position", "relative_hit_position",
    "face", "block_id", "fluid_id", "state_properties", "collision_shape",
    "block_light", "sky_light",
}


def _ray(value: Any, index: int) -> BlockRayV2:
    name = f"perception.block_rays[{index}]"
    item = _object(value, _RAY_KEYS, name)
    properties: list[tuple[str, str]] = []
    for prop_index, raw in enumerate(
        _array(item["state_properties"], f"{name}.state_properties")
    ):
        entry = _object(raw, {"name", "value"}, f"{name}.state_properties[{prop_index}]")
        properties.append((_string(entry["name"], "property name"), _string(entry["value"], "property value")))
    return BlockRayV2(
        ray_id=_integer(item["ray_id"], f"{name}.ray_id"),
        row=_integer(item["row"], f"{name}.row"),
        column=_integer(item["column"], f"{name}.column"),
        yaw_offset_degrees=_number(item["yaw_offset_degrees"], f"{name}.yaw_offset"),
        pitch_offset_degrees=_number(item["pitch_offset_degrees"], f"{name}.pitch_offset"),
        hit_kind=_string(item["hit_kind"], f"{name}.hit_kind"),
        distance_blocks=_number(item["distance_blocks"], f"{name}.distance"),
        relative_block_position=(
            None if item["relative_block_position"] is None
            else _vec(item["relative_block_position"], f"{name}.relative_block_position")
        ),
        relative_hit_position=(
            None if item["relative_hit_position"] is None
            else _vec(item["relative_hit_position"], f"{name}.relative_hit_position")
        ),
        face=_optional_string(item["face"], f"{name}.face"),
        block_id=_optional_string(item["block_id"], f"{name}.block_id"),
        fluid_id=_optional_string(item["fluid_id"], f"{name}.fluid_id"),
        state_properties=tuple(properties),
        collision_shape=_optional_string(item["collision_shape"], f"{name}.collision_shape"),
        block_light=_optional_integer(item["block_light"], f"{name}.block_light"),
        sky_light=_optional_integer(item["sky_light"], f"{name}.sky_light"),
    )


def _body_contact(value: Any, index: int) -> BodyContactV2:
    name = f"perception.body_contacts[{index}]"
    item = _object(
        value,
        {"relative_block_position", "block_id", "fluid_id", "collision_shape"},
        name,
    )
    return BodyContactV2(
        relative_block_position=_vec(item["relative_block_position"], f"{name}.position"),
        block_id=_string(item["block_id"], f"{name}.block_id"),
        fluid_id=_optional_string(item["fluid_id"], f"{name}.fluid_id"),
        collision_shape=_string(item["collision_shape"], f"{name}.collision_shape"),
    )


def _visible_entity(value: Any, index: int) -> VisibleEntityV2:
    name = f"perception.visible_entities[{index}]"
    item = _object(
        value,
        {
            "track_id", "entity_type", "display_name", "relative_position",
            "relative_velocity", "relative_yaw_degrees", "pitch_degrees",
            "bounding_box_size", "pose", "is_on_ground", "equipment",
        },
        name,
    )
    equipment: list[tuple[str, VisibleItemV2]] = []
    for equipment_index, raw in enumerate(_array(item["equipment"], f"{name}.equipment")):
        entry = _object(raw, {"slot", "item"}, f"{name}.equipment[{equipment_index}]")
        appearance = _object(entry["item"], {"empty", "item_id"}, "visible equipment item")
        equipment.append(
            (
                _string(entry["slot"], "equipment slot"),
                VisibleItemV2(
                    empty=_boolean(appearance["empty"], "visible equipment empty"),
                    item_id=_optional_string(appearance["item_id"], "visible equipment id"),
                ),
            )
        )
    return VisibleEntityV2(
        track_id=_string(item["track_id"], f"{name}.track_id"),
        entity_type=_string(item["entity_type"], f"{name}.entity_type"),
        display_name=_optional_string(item["display_name"], f"{name}.display_name"),
        relative_position=_vec(item["relative_position"], f"{name}.relative_position"),
        relative_velocity=_vec(item["relative_velocity"], f"{name}.relative_velocity"),
        relative_yaw_degrees=_number(
            item["relative_yaw_degrees"], f"{name}.relative_yaw"
        ),
        pitch_degrees=_number(item["pitch_degrees"], f"{name}.pitch"),
        bounding_box_size=_vec(item["bounding_box_size"], f"{name}.bounding_box_size"),
        pose=_string(item["pose"], f"{name}.pose"),
        is_on_ground=_boolean(item["is_on_ground"], f"{name}.is_on_ground"),
        equipment=tuple(equipment),
    )


def _perception(value: Any) -> PerceptionStateV2:
    profile_keys = ({"sensor_profile_revision"}
                    if isinstance(value, dict) and "sensor_profile_revision" in value else set())
    item = _object(
        value,
        {
            "horizontal_fov_degrees", "vertical_fov_degrees", "ray_columns", "ray_rows",
            "max_block_distance", "body_expansion_blocks", "block_epsilon_blocks",
            "entity_max_distance", "entity_occlusion_epsilon_blocks", "block_rays",
            "body_contacts", "visible_entities", "entities_truncated",
            "truncated_entity_count",
        } | profile_keys,
        "perception.value",
    )
    return PerceptionStateV2(
        sensor_profile_revision=_integer(item.get("sensor_profile_revision", 1), "sensor profile revision"),
        horizontal_fov_degrees=_number(item["horizontal_fov_degrees"], "horizontal fov"),
        vertical_fov_degrees=_number(item["vertical_fov_degrees"], "vertical fov"),
        ray_columns=_integer(item["ray_columns"], "ray columns"),
        ray_rows=_integer(item["ray_rows"], "ray rows"),
        max_block_distance=_number(item["max_block_distance"], "block distance"),
        body_expansion_blocks=_number(item["body_expansion_blocks"], "body expansion"),
        block_epsilon_blocks=_number(item["block_epsilon_blocks"], "block epsilon"),
        entity_max_distance=_number(item["entity_max_distance"], "entity distance"),
        entity_occlusion_epsilon_blocks=_number(
            item["entity_occlusion_epsilon_blocks"], "entity epsilon"
        ),
        block_rays=tuple(
            _ray(ray, index)
            for index, ray in enumerate(_array(item["block_rays"], "block rays"))
        ),
        body_contacts=tuple(
            _body_contact(contact, index)
            for index, contact in enumerate(
                _array(item["body_contacts"], "body contacts")
            )
        ),
        visible_entities=tuple(
            _visible_entity(entity, index)
            for index, entity in enumerate(
                _array(item["visible_entities"], "visible entities")
            )
        ),
        entities_truncated=_boolean(item["entities_truncated"], "entities truncated"),
        truncated_entity_count=_integer(
            item["truncated_entity_count"], "truncated entity count"
        ),
    )


def _group(
    value: Any,
    name: str,
    expected_source: str,
    decoder: Callable[[Any], U],
) -> ObservationGroupV2[U]:
    item = _object(
        value,
        {"status", "sample_world_tick", "source_kind", "reason_code", "value"},
        name,
    )
    try:
        status = FieldStatusV0(_string(item["status"], f"{name}.status"))
    except ValueError as error:
        raise ClientObservationPayloadError(f"{name} status is invalid") from error
    source = _string(item["source_kind"], f"{name}.source_kind")
    if source != expected_source:
        raise ClientObservationPayloadError(f"{name} source kind is invalid")
    raw_value = item["value"]
    decoded = decoder(raw_value) if status is FieldStatusV0.VALID else None
    if status is not FieldStatusV0.VALID and raw_value is not None:
        raise ClientObservationPayloadError(f"{name} non-valid value must be null")
    return ObservationGroupV2(
        status=status,
        sample_world_tick=_integer(item["sample_world_tick"], f"{name}.sample_world_tick"),
        source_kind=source,
        reason_code=_optional_string(item["reason_code"], f"{name}.reason_code"),
        value=decoded,
    )


def decode_client_observation_payload(
    payload: bytes,
    *,
    expected_generation_id: int | None = None,
    expected_world_tick: int | None = None,
) -> ClientObservationPayloadV2:
    """Decode exact-key UTF-8 JSON emitted by the shared client collector."""

    if not isinstance(payload, bytes):
        raise ClientObservationPayloadError("client payload must be bytes")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ClientObservationPayloadError("client payload is not valid UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
        item = _object(
            value,
            {
                "schema_version", "generation_id", "sample_world_tick", "client_sample",
                "self_state", "inventory", "gui", "perception",
            },
            "client observation payload",
        )
        if item["schema_version"] != "mc2p.client_observation.v2":
            raise ClientObservationPayloadError("client payload schema is invalid")
        generation_id = _integer(item["generation_id"], "generation id")
        sample_world_tick = _integer(item["sample_world_tick"], "sample world tick")
        if expected_generation_id is not None and generation_id != expected_generation_id:
            raise ClientObservationPayloadError("client payload generation does not match")
        if expected_world_tick is not None and sample_world_tick != expected_world_tick:
            raise ClientObservationPayloadError("client payload world tick does not match")
        result = ClientObservationPayloadV2(
            generation_id=generation_id,
            sample_world_tick=sample_world_tick,
            client_sample=ClientSampleTimingV2(**_object(
                item["client_sample"],
                {"clock_id", "started_at_monotonic_ns", "completed_at_monotonic_ns"},
                "client sample timing",
            )),
            self_state=_group(item["self_state"], "self_state", "client_player", _self_state),
            inventory=_group(item["inventory"], "inventory", "client_inventory", _inventory),
            gui=_group(item["gui"], "gui", "client_screen_handler", _gui),
            perception=_group(
                item["perception"],
                "perception",
                "client_perception_filtered",
                _perception,
            ),
        )
        groups = (result.self_state, result.inventory, result.gui, result.perception)
        if any(group.sample_world_tick != sample_world_tick for group in groups):
            raise ClientObservationPayloadError("group sample world tick does not match")
        return result
    except ClientObservationPayloadError:
        raise
    except (ContractViolation, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ClientObservationPayloadError(f"client payload JSON is invalid: {error}") from error


def snapshot_v2_from_craftground(
    message: bytes,
    *,
    episode_id: str,
    sequence_id: int,
    request_sequence_id: int | None,
    request_started_at_monotonic_ns: int,
    received_at_monotonic_ns: int,
    controller_clock_id: str,
    world_time_ticks: int,
    privileged_fields_present: tuple[str, ...] = (),
) -> ObservationSnapshotV2:
    """Build the formal snapshot from the one authoritative client payload."""

    # Equality alone admits bool/float/None expectations after projecting decoded ints.
    # Preserve the wrapper's former strict argument boundary when sharing the projection.
    _integer(sequence_id, "sequence id")
    _integer(world_time_ticks, "world time ticks")
    decoded = decode_client_observation_payload(
        extract_length_delimited_field(message),
        expected_generation_id=sequence_id,
        expected_world_tick=world_time_ticks,
    )
    return snapshot_v2_from_payload(
        decoded, episode_id=episode_id, request_sequence_id=request_sequence_id,
        request_started_at_monotonic_ns=request_started_at_monotonic_ns,
        received_at_monotonic_ns=received_at_monotonic_ns, controller_clock_id=controller_clock_id,
        source_backend="craftground", privileged_fields_present=privileged_fields_present,
    )


def snapshot_v2_from_payload(
    decoded: ClientObservationPayloadV2,
    *,
    episode_id: str,
    request_sequence_id: int | None,
    request_started_at_monotonic_ns: int,
    received_at_monotonic_ns: int,
    controller_clock_id: str,
    source_backend: str,
    privileged_fields_present: tuple[str, ...] = (),
) -> ObservationSnapshotV2:
    """Project the shared validated payload without any backend wire representation."""
    self_value = decoded.self_state.value
    if self_value is None:
        reason = decoded.self_state.reason_code or "client_player_missing"

        def missing() -> FieldValueV0[Any]:
            return FieldValueV0(decoded.self_state.status, None, reason)

        position = missing()
        yaw = missing()
        pitch = missing()
        is_on_ground = missing()
        is_dead = missing()
        health = missing()
        food = missing()
    else:
        position = FieldValueV0.valid(self_value.position)
        yaw = FieldValueV0.valid(self_value.yaw_degrees)
        pitch = FieldValueV0.valid(self_value.pitch_degrees)
        is_on_ground = FieldValueV0.valid(self_value.is_on_ground)
        is_dead = FieldValueV0.valid(self_value.is_dead)
        health = FieldValueV0.valid(self_value.health_points)
        food = FieldValueV0.valid(float(self_value.food_points))
    return ObservationSnapshotV2(
        episode_id=episode_id,
        sequence_id=decoded.generation_id,
        request_sequence_id=request_sequence_id,
        request_started_at_monotonic_ns=request_started_at_monotonic_ns,
        received_at_monotonic_ns=received_at_monotonic_ns,
        controller_clock_id=controller_clock_id,
        client_sample=decoded.client_sample,
        world_time_ticks=FieldValueV0.valid(decoded.sample_world_tick),
        position=position,
        yaw_degrees=yaw,
        pitch_degrees=pitch,
        is_on_ground=is_on_ground,
        is_dead=is_dead,
        health_points=health,
        food_points=food,
        self_state=decoded.self_state,
        inventory=decoded.inventory,
        gui=decoded.gui,
        perception=decoded.perception,
        source_backend=source_backend,
        privileged_fields_present=privileged_fields_present,
    )


def _read_varint(message: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if offset >= len(message):
            raise ClientObservationPayloadError("truncated protobuf varint")
        byte = message[offset]
        offset += 1
        if index == 9 and byte > 1:
            raise ClientObservationPayloadError("protobuf varint exceeds 64 bits")
        value |= (byte & 0x7F) << (index * 7)
        if not byte & 0x80:
            return value, offset
    raise ClientObservationPayloadError("overlong protobuf varint")


def extract_length_delimited_field(
    message: bytes,
    field_number: int = 50000,
    max_bytes: int = 1_048_576,
) -> bytes:
    """Return exactly one top-level length-delimited protobuf field."""

    if not isinstance(message, bytes):
        raise ClientObservationPayloadError("protobuf message must be bytes")
    if type(field_number) is not int or field_number <= 0:
        raise ClientObservationPayloadError("protobuf field number is invalid")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ClientObservationPayloadError("payload limit is invalid")
    found: bytes | None = None
    offset = 0
    while offset < len(message):
        tag, offset = _read_varint(message, offset)
        number = tag >> 3
        wire_type = tag & 7
        if number == 0:
            raise ClientObservationPayloadError("protobuf field number zero is invalid")
        if number == field_number and wire_type != 2:
            raise ClientObservationPayloadError("target field has wrong wire type")
        if wire_type == 0:
            _, offset = _read_varint(message, offset)
            payload = None
        elif wire_type == 1:
            end = offset + 8
            if end > len(message):
                raise ClientObservationPayloadError("truncated fixed64 field")
            offset = end
            payload = None
        elif wire_type == 2:
            size, offset = _read_varint(message, offset)
            end = offset + size
            if end > len(message):
                raise ClientObservationPayloadError("truncated length-delimited field")
            payload = message[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(message):
                raise ClientObservationPayloadError("truncated fixed32 field")
            offset = end
            payload = None
        else:
            raise ClientObservationPayloadError("unsupported protobuf wire type")
        if number == field_number:
            if found is not None:
                raise ClientObservationPayloadError("target field occurs more than once")
            assert payload is not None
            if len(payload) > max_bytes:
                raise ClientObservationPayloadError("target payload exceeds size limit")
            found = payload
    if found is None:
        raise ClientObservationPayloadError(f"field {field_number} is missing")
    return found
