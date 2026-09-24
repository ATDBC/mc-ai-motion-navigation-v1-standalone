"""Handwritten follow scenes decoded through the strict V3 payload boundary."""
from __future__ import annotations

from dataclasses import astuple

from mc2p.backends.client_observation_payload_v3 import (
    decode_client_observation_value_v3,
    snapshot_v3_from_payload,
)
from mc2p.contracts.observation_v3 import (
    AabbV3,
    CollisionShapeV3,
    ObservedBlockV3,
)
from tests.follow_fixtures import player_value
from tests.observation_v3_fixtures import valid_payload_value


def observed_block(position, block_id="minecraft:stone", *, kind="full_cube", boxes=(),
                   fluid_id=None, sources=("first_hit_ray",)) -> ObservedBlockV3:
    """Construct one explicitly observed V3 block without any ray/face fiction."""
    typed_boxes = tuple(box if type(box) is AabbV3 else AabbV3(*box) for box in boxes)
    reason = "fixture_collision_unsupported" if kind == "unsupported" else None
    return ObservedBlockV3(tuple(position), block_id,
                           CollisionShapeV3(kind, typed_boxes, reason),
                           fluid_id, tuple(sources))


def _block_value(block: ObservedBlockV3) -> dict:
    return {
        "position": list(block.position),
        "block_id": block.block_id,
        "collision": {
            "kind": block.collision.kind,
            "boxes": [list(astuple(box)) for box in block.collision.boxes],
            "reason": block.collision.reason,
        },
        "fluid_id": block.fluid_id,
        "sources": list(block.sources),
    }


def follow_snapshot(*, sequence=0, received=100_000_000, position=(-1, 64, -2),
                    entities=None, blocks=(), truncated=False, yaw=0, pitch=0,
                    episode="episode-1", self_changes=None, gui_changes=None,
                    profile="navigation_v1"):
    """Build a real V3 snapshot while preserving the established follow literals."""
    payload = valid_payload_value(profile)
    payload["generation_id"] = sequence
    tick = 100 + sequence
    payload["sample_world_tick"] = tick
    payload["client_sample"].update(
        started_at_monotonic_ns=9000 + sequence * 100,
        completed_at_monotonic_ns=9010 + sequence * 100,
    )
    own = payload["self_state"]["value"]
    own.update(
        position=dict(zip(("x", "y", "z"), position)),
        yaw_degrees=yaw,
        head_yaw_degrees=yaw,
        body_yaw_degrees=yaw,
        pitch_degrees=pitch,
        game_mode="survival",
        allow_flying=False,
    )
    own.update(self_changes or {})
    payload["gui"]["value"].update(gui_changes or {})
    perception = payload["perception"]["value"]
    perception.update(
        blocks=[_block_value(block) for block in sorted(blocks, key=lambda item: item.position)],
        visible_entities=[player_value()] if entities is None else list(entities),
        entities_truncated=truncated,
        truncated_entity_count=1 if truncated else 0,
    )
    for name in ("self_state", "inventory", "gui", "perception", "targeting", "tracked_entity"):
        payload[name]["sample_world_tick"] = tick
    decoded = decode_client_observation_value_v3(payload)
    return snapshot_v3_from_payload(
        decoded,
        episode_id=episode,
        request_sequence_id=sequence if sequence else None,
        request_started_at_monotonic_ns=max(0, received - 1_000_000),
        received_at_monotonic_ns=received,
        controller_clock_id="controller-test",
        source_backend="fixture",
    )
