"""Fixed, task-minimal observation field selections (not player actions)."""
from dataclasses import dataclass, field
from typing import Literal

from mc2p.contracts.common import ContractViolation, require_identifier

OBSERVATION_V2 = "mc2p.client_observation.v2"
OBSERVATION_V3 = "mc2p.client_observation.v3"
MAX_AIR_QUERY_POSITIONS = 128
MAX_OBSERVATION_REQUEST_BYTES = 16384
_FIELD_PROFILES = ("navigation_v1", "interaction_v1")
_REQUEST_SCHEMA = "mc2p.observation_request.v3"
_MAX_ENTITY_TRACK_ID_LENGTH = 128
_MIN_GRID_AXIS = -(2 ** 31)
_MAX_GRID_AXIS = 2 ** 31 - 1


def observation_request_wire_upper_bound() -> int:
    """Bound the four native fields' compact JSON, without encoding a request.

    Profiles, schema and validated identifiers are ASCII without JSON escapes.
    A signed-32-bit axis takes at most 11 bytes; each triple adds two brackets
    and two commas. The outer array adds one comma between adjacent triples.
    The longest identifier also covers the shorter JSON null alternative.
    Field or encoding changes must update this proof and its wire cross-check.
    """
    empty_fields_bytes = len(
        '{"field_profile":"","air_positions":[],"entity_track_id":"","schema_version":""}'
    )
    scalar_bytes = (empty_fields_bytes + max(map(len, _FIELD_PROFILES))
                    + _MAX_ENTITY_TRACK_ID_LENGTH + len(_REQUEST_SCHEMA))
    axis_bytes = max(len(str(_MIN_GRID_AXIS)), len(str(_MAX_GRID_AXIS)))
    triple_bytes = 3 * axis_bytes + 4
    return (scalar_bytes + MAX_AIR_QUERY_POSITIONS * triple_bytes
            + max(0, MAX_AIR_QUERY_POSITIONS - 1))


_REQUEST_WIRE_UPPER_BOUND = observation_request_wire_upper_bound()


def _air_positions(value: tuple[tuple[int, int, int], ...]) -> tuple[tuple[int, int, int], ...]:
    if (type(value) is not tuple
            or any(type(position) is not tuple or len(position) != 3
                   or any(type(axis) is not int or not _MIN_GRID_AXIS <= axis <= _MAX_GRID_AXIS
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
    schema_version: str = field(default=_REQUEST_SCHEMA, init=False)

    def __post_init__(self) -> None:
        if type(self.field_profile) is not str or self.field_profile not in _FIELD_PROFILES:
            raise ContractViolation("unsupported observation field profile")
        if self.entity_track_id is not None:
            if type(self.entity_track_id) is not str:
                raise ContractViolation("entity track id must be a native string identifier")
            require_identifier(self.entity_track_id, "entity track id")
            if len(self.entity_track_id) > _MAX_ENTITY_TRACK_ID_LENGTH:
                raise ContractViolation("entity track id exceeds size bound")
        ordered = _air_positions(self.air_positions)
        if _REQUEST_WIRE_UPPER_BOUND > MAX_OBSERVATION_REQUEST_BYTES:
            raise ContractViolation("observation request payload budget exceeded")
        object.__setattr__(self, "air_positions", ordered)


DEFAULT_OBSERVATION_REQUEST_V3 = ObservationRequestV3()


def resolve_observation_request(schema: str, request: ObservationRequestV3 | None) -> ObservationRequestV3 | None:
    validate_observation_schema(schema)
    if schema == OBSERVATION_V2:
        if request is not None:
            raise ContractViolation("V2 backend does not accept observation requests")
        return None
    if request is not None and type(request) is not ObservationRequestV3:
        raise ContractViolation("observation request must be ObservationRequestV3")
    return DEFAULT_OBSERVATION_REQUEST_V3 if request is None else request


def merge_observation_requests(
    requests: tuple[ObservationRequestV3 | None, ...],
) -> ObservationRequestV3:
    """Merge same-frame needs without choosing one tracked entity by accident."""
    if (type(requests) is not tuple
            or any(request is not None and type(request) is not ObservationRequestV3
                   for request in requests)):
        raise ContractViolation("control frame requests must be an observation request tuple")
    present = tuple(request for request in requests if request is not None)
    if not present:
        return DEFAULT_OBSERVATION_REQUEST_V3
    if all(request == present[0] for request in present[1:]):
        return present[0]
    profiles = {request.field_profile for request in present}
    entity_ids = {request.entity_track_id for request in present
                  if request.entity_track_id is not None}
    if len(entity_ids) > 1:
        raise ContractViolation("one control frame cannot track different entities")
    profile = "interaction_v1" if "interaction_v1" in profiles else "navigation_v1"
    positions = _air_positions(tuple(position for request in present for position in request.air_positions))
    entity_id = next(iter(entity_ids), None)
    for request in present:
        if (request.field_profile == profile and request.air_positions == positions
                and request.entity_track_id == entity_id):
            return request
    return ObservationRequestV3(profile, positions, entity_id)
