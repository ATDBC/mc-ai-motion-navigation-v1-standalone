"""Hand-declared V3 wire fixtures; never convert V2 ray evidence into blocks."""
from __future__ import annotations

import json

from tests.observation_v2_fixtures import valid_payload_value as legacy_payload


def block_value(position=(0, 63, 0), block_id="minecraft:stone", sources=("first_hit_ray",)):
    return dict(position=list(position), block_id=block_id,
                collision=dict(kind="full_cube", boxes=[], reason=None),
                fluid_id=None, sources=list(sources))


def tracked_entity_value(track_id="entity-world-7", *, health=20.0, dead=False):
    return dict(
        track_id=track_id,
        entity_type="minecraft:zombie",
        relative_position=dict(x=2.0, y=0.0, z=1.0),
        relative_velocity=dict(x=0.1, y=0.0, z=0.0),
        relative_yaw_degrees=15.0,
        pitch_degrees=0.0,
        bounding_box_size=dict(x=0.6, y=1.95, z=0.6),
        pose="standing",
        is_on_ground=True,
        is_loaded=True,
        is_dead=dead,
        health_points=health,
        max_health_points=20.0,
    )


def valid_payload_value(profile="navigation_v1"):
    shared = legacy_payload()
    shared["self_state"]["value"]["position"] = dict(x=.5, y=64., z=.5)
    perception = dict(horizontal_fov_degrees=120., vertical_fov_degrees=120.,
        ray_columns=159, ray_rows=9, sensor_profile_revision=3, max_block_distance=16.,
        body_expansion_blocks=.05, block_epsilon_blocks=.001, entity_max_distance=32.,
        entity_occlusion_epsilon_blocks=.05, knowledge_model="block_state_v1", blocks=[],
        visible_entities=[], entities_truncated=False, truncated_entity_count=0)
    def group(value):
        return dict(status="valid", sample_world_tick=100, source_kind="client_perception_filtered",
                    reason_code=None, value=value)
    targeting = group(dict(hit_kind="miss", block_position=None, entity_ref=None,
                           face=None, hit_position=None, distance_blocks=None))
    if profile == "navigation_v1":
        targeting.update(status="missing", reason_code="not_requested", value=None)
    return dict(schema_version="mc2p.client_observation.v3", generation_id=0,
                sample_world_tick=100, client_sample=shared["client_sample"],
                self_state=shared["self_state"], inventory=shared["inventory"], gui=shared["gui"],
                field_profile=profile, perception=group(perception), targeting=targeting,
                tracked_entity=dict(status="missing", sample_world_tick=100,
                    source_kind="client_registered_entity", reason_code="not_requested", value=None))


def encoded(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


def valid_snapshot_v3(*, profile="navigation_v1", blocks=(), sequence=1,
                      request_start_ns=100_000_000, received_at_ns=101_000_000):
    from mc2p.backends.client_observation_payload_v3 import (
        decode_client_observation_payload_v3, snapshot_v3_from_payload,
    )
    from dataclasses import astuple
    value = valid_payload_value(profile)
    value["generation_id"] = sequence
    value["sample_world_tick"] = 100 + sequence
    value["client_sample"].update(started_at_monotonic_ns=9000 + sequence * 100,
                                   completed_at_monotonic_ns=9010 + sequence * 100)
    for key in ("self_state", "inventory", "gui", "perception", "targeting", "tracked_entity"):
        value[key]["sample_world_tick"] = 100 + sequence
    value["perception"]["value"]["blocks"] = [dict(position=list(b.position), block_id=b.block_id,
        collision=dict(kind=b.collision.kind, boxes=[list(astuple(box)) for box in b.collision.boxes],
                       reason=b.collision.reason), fluid_id=b.fluid_id, sources=list(b.sources)) for b in blocks]
    return snapshot_v3_from_payload(decode_client_observation_payload_v3(encoded(value)),
        episode_id="v3-test", request_sequence_id=sequence,
        request_started_at_monotonic_ns=request_start_ns, received_at_monotonic_ns=received_at_ns,
        controller_clock_id="controller-test", source_backend="fixture")
