"""Restore current formal Observation V3 traces without legacy V2 dependencies."""
from __future__ import annotations

import json

from mc2p.backends.client_observation_payload_v3 import (
    decode_client_observation_payload_v3,
    require_formal_surface_perception,
    snapshot_v3_from_payload,
)
from mc2p.runtime.trace import trace_projection


def _same_trace_value(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return (
            type(actual) in (int, float)
            and type(expected) in (int, float)
            and actual == expected
        )
    if type(actual) is dict:
        return actual.keys() == expected.keys() and all(
            _same_trace_value(actual[key], expected[key]) for key in actual
        )
    if type(actual) is list:
        return len(actual) == len(expected) and all(
            _same_trace_value(left, right)
            for left, right in zip(actual, expected)
        )
    return actual == expected


def _restore(raw: dict, *, require_surface: bool):
    if type(raw) is not dict or raw.get("schema_version") != "mc2p.observation.v3":
        raise ValueError("observation trace must use mc2p.observation.v3")
    try:
        payload = {
            key: raw[key]
            for key in (
                "client_sample", "self_state", "inventory", "gui", "perception",
                "field_profile", "targeting", "tracked_entity", "damage_events",
                "damage_events_dropped",
            )
        }
        payload = json.loads(json.dumps(payload, allow_nan=False))
        payload.update(
            schema_version="mc2p.client_observation.v3",
            generation_id=raw["sequence_id"],
            sample_world_tick=raw["world_time_ticks"]["value"],
        )
        if payload["gui"]["value"] is not None:
            payload["gui"]["value"]["properties"] = [
                {"property_id": index, "value": value}
                for index, value in payload["gui"]["value"]["properties"]
            ]
        if payload["perception"]["value"] is not None:
            for block in payload["perception"]["value"]["blocks"]:
                block["collision"]["boxes"] = [
                    [
                        box[key]
                        for key in (
                            "min_x", "min_y", "min_z", "max_x", "max_y", "max_z",
                        )
                    ]
                    for box in block["collision"]["boxes"]
                ]
            for entity in payload["perception"]["value"]["visible_entities"]:
                entity["equipment"] = [
                    {"slot": slot, "item": item}
                    for slot, item in entity["equipment"]
                ]
        decoded = decode_client_observation_payload_v3(
            json.dumps(payload, allow_nan=False).encode()
        )
        if require_surface:
            require_formal_surface_perception(decoded)
        observation = snapshot_v3_from_payload(
            decoded,
            **{
                key: raw[key]
                for key in (
                    "episode_id", "request_sequence_id",
                    "request_started_at_monotonic_ns", "received_at_monotonic_ns",
                    "controller_clock_id", "source_backend",
                )
            },
            privileged_fields_present=tuple(raw["privileged_fields_present"]),
        )
    except (KeyError, TypeError, IndexError) as error:
        raise ValueError("malformed formal V3 snapshot") from error
    if not _same_trace_value(trace_projection(observation), raw):
        raise ValueError("formal V3 snapshot changed during trace restoration")
    return observation


def restore_observation_v3_trace(raw: dict):
    """Restore historical or current V3 data without granting actor eligibility."""
    return _restore(raw, require_surface=False)


def restore_formal_surface_observation_v3(raw: dict):
    """Restore V3 data and require the current profile-4 surface contract."""
    return _restore(raw, require_surface=True)
