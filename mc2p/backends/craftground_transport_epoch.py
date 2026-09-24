"""Versioned adapter-only reset binding; never added to the policy contracts."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from mc2p.backends.client_behavior_payload import MAX_ACTION_BYTES, _varint
from mc2p.backends.client_observation_payload import ClientObservationPayloadError, extract_length_delimited_field
from mc2p.contracts.common import ContractViolation

if TYPE_CHECKING:
    from craftground.proto.action_space_pb2 import ActionSpaceMessageV2

EPOCH_FIELD_NUMBER = 50003
MAX_EPOCH_BYTES = 96
_TOKEN = re.compile(r"mc2p\.transport-epoch\.v1/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/([1-9][0-9]{0,18})\Z")
_LIFECYCLE_COMMANDS = frozenset({"exit", "fastreset", "fastreset ", "respawn"})


def _validate_token(token: str) -> bytes:
    match = _TOKEN.fullmatch(token) if type(token) is str else None
    if match is None or int(match.group(1)) > 9_223_372_036_854_775_807:
        raise ContractViolation("invalid transport epoch")
    return token.encode("ascii")


def decode_transport_epoch(wire: bytes) -> str:
    if type(wire) is not bytes or not 0 < len(wire) <= 2_097_152:
        raise ContractViolation("invalid transport epoch envelope size")
    try:
        token = extract_length_delimited_field(wire, EPOCH_FIELD_NUMBER, MAX_EPOCH_BYTES).decode("ascii", errors="strict")
    except (UnicodeError, ClientObservationPayloadError) as error:
        raise ContractViolation("invalid transport epoch envelope") from error
    _validate_token(token)
    return token


def bind_transport_epoch(message: ActionSpaceMessageV2, token: str) -> ActionSpaceMessageV2:
    from craftground.proto.action_space_pb2 import ActionSpaceMessageV2
    from google.protobuf.unknown_fields import UnknownFieldSet

    payload = _validate_token(token)
    if type(message) is not ActionSpaceMessageV2:
        raise ContractViolation("transport binding requires an action envelope")
    fields = list(UnknownFieldSet(message))
    known = message.ListFields()
    if fields:
        numbers = [field.field_number for field in fields]
        if (len(numbers) != len(set(numbers)) or set(numbers) not in ({50001}, {50001, 50004})
                or any(field.wire_type != 2 or not 0 < len(field.data) <= MAX_ACTION_BYTES for field in fields)
                or known):
            raise ContractViolation("invalid or already bound formal action envelope")
    elif known:
        if (len(known) != 1 or known[0][0].name != "commands" or len(message.commands) != 1
                or message.commands[0] not in _LIFECYCLE_COMMANDS):
            raise ContractViolation("transport binding forbids legacy keys and arbitrary commands")
    wire = message.SerializeToString() + _varint((EPOCH_FIELD_NUMBER << 3) | 2) + _varint(len(payload)) + payload
    return ActionSpaceMessageV2.FromString(wire)
