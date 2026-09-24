"""Bounded formal behavior transport, independent of CraftGround's key action space."""
from __future__ import annotations

from dataclasses import asdict
import json

from mc2p.contracts.action_v1 import ActionSnapshotV1
from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation_request_v3 import ObservationRequestV3

ACTION_FIELD_NUMBER = 50001
MAX_ACTION_BYTES = 16_384


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 127:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def behavior_action_message(action: ActionSnapshotV1, *, now_ns: int,
                            observation_received_at_ns: int | None = None,
                            observation_request: ObservationRequestV3 | None = None):
    from craftground.proto.action_space_pb2 import ActionSpaceMessageV2

    payload = encode_behavior_action(action, now_ns=now_ns,
                                     observation_received_at_ns=observation_received_at_ns)
    wire = _varint((ACTION_FIELD_NUMBER << 3) | 2) + _varint(len(payload)) + payload
    if observation_request is not None:
        if type(observation_request) is not ObservationRequestV3:
            raise ContractViolation("observation request must be ObservationRequestV3")
        request = json.dumps(asdict(observation_request), separators=(",", ":")).encode("utf-8")
        wire += _varint((50004 << 3) | 2) + _varint(len(request)) + request
    return ActionSpaceMessageV2.FromString(wire)


def encode_behavior_action(action: ActionSnapshotV1, *, now_ns: int,
                           observation_received_at_ns: int | None = None) -> bytes:
    if type(action) is not ActionSnapshotV1:
        raise ContractViolation("formal action must be ActionSnapshotV1")
    require_nonnegative_int(now_ns, "now_ns")
    budget = action.deadline_monotonic_ns - now_ns
    if budget <= 0:
        raise TimeoutError("client behavior deadline expired before transport")
    anchor = now_ns if observation_received_at_ns is None else observation_received_at_ns
    require_nonnegative_int(anchor, "observation_received_at_ns")
    if anchor > now_ns:
        raise ContractViolation("observation receive anchor is in the future")
    wire = asdict(action)
    del wire["deadline_monotonic_ns"]
    wire["schema_version"] = "mc2p.client_action.v1"
    wire["remaining_budget_ns"] = min(budget, 30_000_000_000)
    # The JVM anchors this interval at observation generation, which necessarily
    # precedes Python receiving it. This is conservative without shared clock epochs.
    wire["observation_budget_ns"] = action.deadline_monotonic_ns - anchor
    encoded = json.dumps(wire, ensure_ascii=True, allow_nan=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_ACTION_BYTES:
        raise ContractViolation("client action exceeds transport limit")
    return encoded


def decode_behavior_receipt(payload: bytes) -> dict:
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_ACTION_BYTES:
        raise ContractViolation("invalid behavior receipt size")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractViolation("duplicate behavior receipt key")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8", errors="strict"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (UnicodeError, ValueError) as error:
        raise ContractViolation("invalid behavior receipt JSON") from error
    behavior_receipt_from_mapping(value)
    return value
