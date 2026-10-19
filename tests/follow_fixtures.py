"""Handwritten follow scenes decoded through the real V2 payload boundary."""
from __future__ import annotations

from dataclasses import replace
import json

from mc2p.backends.client_observation_payload import decode_client_observation_payload
from mc2p.contracts.common import FieldValueV0
from mc2p.contracts.observation import Vec3V0
from tests.observation_v2_fixtures import valid_payload_value, valid_snapshot_v2


def player_value(track_id="player-1", relative=(2, 0, 3), *, entity_type="minecraft:player"):
    return {
        "track_id": track_id, "entity_type": entity_type, "display_name": "Visible label",
        "relative_position": dict(zip(("x", "y", "z"), relative)),
        "relative_velocity": {"x": 9, "y": 0, "z": 9},
        "relative_yaw_degrees": 0, "pitch_degrees": 0,
        "bounding_box_size": {"x": .6, "y": 1.8, "z": .6},
        "pose": "standing", "is_on_ground": True,
        "equipment": [{"slot": "head", "item": {"empty": False, "item_id": "minecraft:iron_helmet"}}],
        "hurt_animation_ticks": 0,
    }


def follow_snapshot(*, sequence=0, received=100_000_000, position=(-1, 64, -2),
                    entities=None, contacts=(), rays=(), truncated=False, yaw=0, pitch=0,
                    episode="episode-1", self_changes=None, gui_changes=None, sensor_profile_revision=1):
    payload = valid_payload_value(generation_id=sequence)
    own = payload["self_state"]["value"]
    own.update(position=dict(zip(("x", "y", "z"), position)), yaw_degrees=yaw,
               head_yaw_degrees=yaw, body_yaw_degrees=yaw, pitch_degrees=pitch,
               game_mode="survival", allow_flying=False)
    own.update(self_changes or {})
    payload["gui"]["value"].update(gui_changes or {})
    perception = payload["perception"]["value"]
    if sensor_profile_revision == 2:
        perception.update(sensor_profile_revision=2, horizontal_fov_degrees=120., vertical_fov_degrees=120.)
        for ray in perception['block_rays']:
            ray['yaw_offset_degrees'] = -60. + ray['column'] * (120. / 14.)
            ray['pitch_offset_degrees'] = -60. + ray['row'] * 15.
    elif sensor_profile_revision == 3:
        from tests.observation_v2_fixtures import dense_perception_value
        perception.update(dense_perception_value())
    elif sensor_profile_revision != 1:
        raise ValueError('unsupported fixture sensor profile')
    perception.update(visible_entities=[player_value()] if entities is None else entities,
                      body_contacts=list(contacts), entities_truncated=truncated,
                      truncated_entity_count=1 if truncated else 0)
    for ray in rays:
        perception["block_rays"][ray["ray_id"]].update(ray)
    payload["client_sample"].update(started_at_monotonic_ns=9000 + sequence * 100,
                                    completed_at_monotonic_ns=9010 + sequence * 100)
    decoded = decode_client_observation_payload(json.dumps(payload).encode())
    snapshot = valid_snapshot_v2(episode_id=episode, sequence_id=sequence,
                                request_sequence_id=sequence if sequence else None,
                                request_started_at_monotonic_ns=max(0, received - 1_000_000),
                                received_at_monotonic_ns=received,
                                position=Vec3V0(*position), yaw_degrees=yaw, pitch_degrees=pitch,
                                is_on_ground=own["is_on_ground"], is_dead=own["is_dead"])
    return replace(snapshot, self_state=decoded.self_state, gui=decoded.gui,
                   perception=decoded.perception, client_sample=decoded.client_sample,
                   health_points=FieldValueV0.valid(own["health_points"]))


def surface_ray(ray_id, block, own=(-1, 64, -2), *, block_id="minecraft:stone",
                face="up", shape="solid", fluid=None, hit=None):
    if hit is None:
        hit = (block[0] + .5, block[1] + 1, block[2] + .5)
    return {"ray_id": ray_id, "hit_kind": "block", "distance_blocks": 3.0,
            "relative_block_position": dict(zip(("x", "y", "z"), (b-p for b,p in zip(block,own)))),
            "relative_hit_position": dict(zip(("x", "y", "z"), (b-p for b,p in zip(hit,own)))),
            "face": face, "block_id": block_id, "fluid_id": fluid,
            "state_properties": [], "collision_shape": shape, "block_light": 0, "sky_light": 15}
