"""Fixed, task-minimal observation field selections (not player actions)."""
from dataclasses import dataclass, field
import json
from typing import Literal

from mc2p.contracts.common import ContractViolation, require_identifier

OBSERVATION_V2 = "mc2p.client_observation.v2"
OBSERVATION_V3 = "mc2p.client_observation.v3"
MAX_AIR_QUERY_POSITIONS = 512
MAX_OBSERVATION_REQUEST_BYTES = 16384


def _air_positions(value: tuple[tuple[int, int, int], ...]) -> tuple[tuple[int, int, int], ...]:
    if (type(value) is not tuple
            or any(type(position) is not tuple or len(position) != 3
                   or any(type(axis) is not int or not -(2 ** 31) <= axis < 2 ** 31
                          for axis in position) for position in value)):
        raise ContractViolation("air positions must be signed 32-bit integer grid triples")
    ordered = tuple(sorted(set(value)))
    if len(ordered) > MAX_AIR_QUERY_POSITIONS:
        raise ContractViolation("air query position budget exceeded")
    return ordered


def validate_observation_schema(value: str) -> str:
    if type(value) is not str or value not in (OBSERVATION_V2, OBSERVATION_V3):
        raise ContractViolation("unsupported observation schema")
    return value


@dataclass(frozen=True, slots=True)
class ObservationRequestV3:
    field_profile: Literal["navigation_v1", "interaction_v1"] = "navigation_v1"
    air_positions: tuple[tuple[int, int, int], ...] = ()
    entity_track_id: str | None = None
    schema_version: str = field(default="mc2p.observation_request.v3", init=False)

    def __post_init__(self) -> None:
        if type(self.field_profile) is not str or self.field_profile not in ("navigation_v1", "interaction_v1"):
            raise ContractViolation("unsupported observation field profile")
        if self.entity_track_id is not None:
            require_identifier(self.entity_track_id, "entity track id")
            if len(self.entity_track_id) > 128:
                raise ContractViolation("entity track id exceeds size bound")
        ordered = _air_positions(self.air_positions)
        payload = json.dumps({
            "field_profile": self.field_profile,
            "air_positions": ordered,
            "entity_track_id": self.entity_track_id,
            "schema_version": self.schema_version,
        }, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_OBSERVATION_REQUEST_BYTES:
            raise ContractViolation("observation request payload budget exceeded")
        object.__setattr__(self, "air_positions", ordered)


def resolve_observation_request(schema: str, request: ObservationRequestV3 | None) -> ObservationRequestV3 | None:
    validate_observation_schema(schema)
    if schema == OBSERVATION_V2:
        if request is not None:
            raise ContractViolation("V2 backend does not accept observation requests")
        return None
    if request is not None and type(request) is not ObservationRequestV3:
        raise ContractViolation("observation request must be ObservationRequestV3")
    return ObservationRequestV3() if request is None else request
