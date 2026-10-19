"""Current V3 runtime trace fixtures shared by Fabric evidence tests."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

from mc2p.contracts.action_v1 import ActionSnapshotV1
from tests.observation_v3_fixtures import valid_snapshot_v3
from tests.test_action_receipt import receipt_value


def trace_evidence():
    observations = [
        asdict(replace(
            valid_snapshot_v3(sequence=index),
            episode_id="episode",
            request_sequence_id=None if index == 0 else index - 1,
        ))
        for index in range(29)
    ]
    records = [{
        "record_type": "reset",
        "payload": {"result": {"observation": observations[0]}},
    }]
    for index in range(28):
        decision = {
            "action": asdict(ActionSnapshotV1("episode", index, index, 1000)),
        }
        receipt = receipt_value(
            episode_id="episode",
            request_sequence_id=index,
            generation_id=index + 1,
            world_tick=observations[index + 1]["world_time_ticks"]["value"],
            input_samples=index + 1,
        )
        records += [
            {"record_type": "dispatch", "payload": {"decision": deepcopy(decision)}},
            {"record_type": "step", "payload": {
                "decision": decision,
                "backend_result": {
                    "observation": observations[index + 1], "receipt": receipt,
                },
            }},
        ]
    rows = [{
        "session_id": "client",
        "observation_sequence": index + 1,
        "image_bytes": 0,
        "framebuffer_capture_calls": 0,
        "image_encode_calls": 0,
        "render_world_completions": 0,
        "window_visible": False,
        "window_visible_at_creation": False,
    } for index in range(29)]
    return json.loads(json.dumps(records)), rows
