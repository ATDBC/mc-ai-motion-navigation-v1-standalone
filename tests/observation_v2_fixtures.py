from __future__ import annotations

import json


def empty_item_value() -> dict[str, object]:
    return {
        "empty": True,
        "item_id": None,
        "count": 0,
        "damage": 0,
        "max_damage": 0,
        "damageable": False,
        "custom_name": None,
    }


def visible_entity_value(*, hurt_animation_ticks: int = 0) -> dict[str, object]:
    return {
        "track_id": "entity-test-1",
        "entity_type": "minecraft:zombie",
        "display_name": "Zombie",
        "relative_position": {"x": 0.0, "y": 0.0, "z": 2.0},
        "relative_velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
        "relative_yaw_degrees": 0.0,
        "pitch_degrees": 0.0,
        "bounding_box_size": {"x": 0.6, "y": 1.95, "z": 0.6},
        "pose": "standing",
        "is_on_ground": True,
        "equipment": [],
        "hurt_animation_ticks": hurt_animation_ticks,
    }


def miss_ray_value(ray_id: int) -> dict[str, object]:
    row, column = divmod(ray_id, 15)
    return {
        "ray_id": ray_id,
        "row": row,
        "column": column,
        "yaw_offset_degrees": -45.0 + column * (90.0 / 14.0),
        "pitch_offset_degrees": -30.0 + row * (60.0 / 8.0),
        "hit_kind": "miss",
        "distance_blocks": 16.0,
        "relative_block_position": None,
        "relative_hit_position": None,
        "face": None,
        "block_id": None,
        "fluid_id": None,
        "state_properties": [],
        "collision_shape": None,
        "block_light": None,
        "sky_light": None,
    }


def dense_perception_value() -> dict[str, object]:
    """Explicit new profile; historical default fixtures remain profile 1."""
    value = valid_payload_value()['perception']['value']
    value.update(sensor_profile_revision=3, horizontal_fov_degrees=120.,
                 vertical_fov_degrees=120., ray_columns=159, ray_rows=9)
    rays = []
    for identifier in range(1431):
        row, column = divmod(identifier, 159)
        ray = miss_ray_value(0)
        ray.update(ray_id=identifier, row=row, column=column,
                   yaw_offset_degrees=-60. + column * (120. / 158.),
                   pitch_offset_degrees=-60. + row * 15.)
        rays.append(ray)
    value['block_rays'] = rays
    return value


def valid_payload_value(
    *,
    generation_id: int = 0,
    sample_world_tick: int = 100,
) -> dict[str, object]:
    empty = empty_item_value()
    self_value = {
        "position": {"x": 1.0, "y": 2.0, "z": 3.0},
        "velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
        "yaw_degrees": 90.0,
        "pitch_degrees": 0.0,
        "head_yaw_degrees": 90.0,
        "body_yaw_degrees": 90.0,
        "is_dead": False,
        "is_on_ground": True,
        "horizontal_collision": False,
        "vertical_collision": True,
        "pose": "standing",
        "eye_height_blocks": 1.62,
        "is_sprinting": False,
        "is_sneaking": False,
        "is_swimming": False,
        "is_submerged_in_water": False,
        "is_climbing": False,
        "is_fall_flying": False,
        "is_burning": False,
        "fall_distance_blocks": 0.0,
        "air_ticks": 300,
        "max_air_ticks": 300,
        "health_points": 20.0,
        "max_health_points": 20.0,
        "absorption_points": 0.0,
        "armor_points": 0,
        "food_points": 20,
        "saturation_points": 5.0,
        "experience_level": 0,
        "experience_progress": 0.0,
        "total_experience": 0,
        "attack_cooldown": 1.0,
        "hurt_animation_ticks": 0,
        "movement_tick_id": 1,
        "active_hand": None,
        "is_using_item": False,
        "item_use_ticks_remaining": 0,
        "status_effects": [],
        "game_mode": "creative",
        "is_flying": False,
        "allow_flying": True,
    }
    inventory_value = {
        "main": [dict(empty) for _ in range(36)],
        "selected_hotbar_slot": 0,
        "head": dict(empty),
        "chest": dict(empty),
        "legs": dict(empty),
        "feet": dict(empty),
        "offhand": dict(empty),
        "main_hand": dict(empty),
    }
    gui_value = {
        "open": False,
        "gui_session_id": None,
        "screen_kind": "closed",
        "handler_type": None,
        "sync_id": None,
        "revision": None,
        "title": None,
        "slots": [],
        "cursor_stack": dict(empty),
        "properties": [],
        "properties_status": "valid",
        "properties_reason_code": None,
        "focused_slot_id": None,
    }
    perception_value = {
        "horizontal_fov_degrees": 90.0,
        "vertical_fov_degrees": 60.0,
        "ray_columns": 15,
        "ray_rows": 9,
        "max_block_distance": 16.0,
        "body_expansion_blocks": 0.05,
        "block_epsilon_blocks": 0.001,
        "entity_max_distance": 32.0,
        "entity_occlusion_epsilon_blocks": 0.05,
        "block_rays": [miss_ray_value(index) for index in range(135)],
        "body_contacts": [],
        "visible_entities": [],
        "entities_truncated": False,
        "truncated_entity_count": 0,
    }

    def group(source: str, value: object) -> dict[str, object]:
        return {
            "status": "valid",
            "sample_world_tick": sample_world_tick,
            "source_kind": source,
            "reason_code": None,
            "value": value,
        }

    return {
        "schema_version": "mc2p.client_observation.v2",
        "generation_id": generation_id,
        "sample_world_tick": sample_world_tick,
        "client_sample": {"clock_id": "jvm-test", "started_at_monotonic_ns": 9000,
                          "completed_at_monotonic_ns": 9010},
        "self_state": group("client_player", self_value),
        "inventory": group("client_inventory", inventory_value),
        "gui": group("client_screen_handler", gui_value),
        "perception": group("client_perception_filtered", perception_value),
    }


def valid_payload_bytes(**kwargs: int) -> bytes:
    return json.dumps(
        valid_payload_value(**kwargs),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def valid_snapshot_v2(
    *,
    episode_id: str = "episode-1",
    sequence_id: int = 0,
    request_sequence_id: int | None = None,
    world_time_ticks: int = 100,
    request_started_at_monotonic_ns: int = 10,
    received_at_monotonic_ns: int = 20,
    position=None,
    yaw_degrees: float = 90.0,
    pitch_degrees: float = 0.0,
    is_on_ground: bool = True,
    is_dead: bool = False,
    health_points: float = 20.0,
    food_points: int = 20,
    source_backend: str = "fake",
):
    from mc2p.backends.client_observation_payload import (
        decode_client_observation_payload,
    )
    from mc2p.contracts.common import FieldValueV0
    from mc2p.contracts.observation import Vec3V0
    from mc2p.contracts.observation_v2 import ObservationSnapshotV2

    if position is None:
        position = Vec3V0(1.0, 2.0, 3.0)
    payload = valid_payload_value(
        generation_id=sequence_id,
        sample_world_tick=world_time_ticks,
    )
    self_value = payload["self_state"]["value"]
    self_value["position"] = {
        "x": position.x,
        "y": position.y,
        "z": position.z,
    }
    self_value["yaw_degrees"] = yaw_degrees
    self_value["head_yaw_degrees"] = yaw_degrees
    self_value["body_yaw_degrees"] = yaw_degrees
    self_value["pitch_degrees"] = pitch_degrees
    self_value["is_on_ground"] = is_on_ground
    self_value["is_dead"] = is_dead
    self_value["health_points"] = health_points
    self_value["food_points"] = food_points
    decoded = decode_client_observation_payload(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    self_state = decoded.self_state.value
    assert self_state is not None
    return ObservationSnapshotV2(
        episode_id=episode_id,
        sequence_id=sequence_id,
        request_sequence_id=request_sequence_id,
        request_started_at_monotonic_ns=request_started_at_monotonic_ns,
        received_at_monotonic_ns=received_at_monotonic_ns,
        controller_clock_id="controller-test",
        client_sample=decoded.client_sample,
        world_time_ticks=FieldValueV0.valid(world_time_ticks),
        position=FieldValueV0.valid(self_state.position),
        yaw_degrees=FieldValueV0.valid(self_state.yaw_degrees),
        pitch_degrees=FieldValueV0.valid(self_state.pitch_degrees),
        is_on_ground=FieldValueV0.valid(self_state.is_on_ground),
        is_dead=FieldValueV0.valid(self_state.is_dead),
        health_points=FieldValueV0.valid(self_state.health_points),
        food_points=FieldValueV0.valid(float(self_state.food_points)),
        self_state=decoded.self_state,
        inventory=decoded.inventory,
        gui=decoded.gui,
        perception=decoded.perception,
        source_backend=source_backend,
    )
