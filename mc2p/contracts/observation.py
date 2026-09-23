"""Player Runtime V0 observation contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mc2p.contracts.common import (
    ContractViolation,
    FieldStatusV0,
    FieldValueV0,
    require_field_name,
    require_finite,
    require_identifier,
    require_nonnegative_int,
)


COORDINATE_FRAME_V0 = "minecraft_world_xyz_blocks"
YAW_CONVENTION_V0 = "0=+z,+90=-x,clockwise_top_down"
PITCH_CONVENTION_V0 = "positive=down"


@dataclass(frozen=True, slots=True)
class Vec3V0:
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        require_finite(self.x, "x")
        require_finite(self.y, "y")
        require_finite(self.z, "z")


@dataclass(frozen=True, slots=True)
class ObservationSnapshotV1:
    episode_id: str
    sequence_id: int
    request_sequence_id: int | None
    observed_at_monotonic_ns: int
    received_at_monotonic_ns: int
    world_time_ticks: FieldValueV0[int]
    position: FieldValueV0[Vec3V0]
    yaw_degrees: FieldValueV0[float]
    pitch_degrees: FieldValueV0[float]
    is_on_ground: FieldValueV0[bool]
    is_dead: FieldValueV0[bool]
    health_points: FieldValueV0[float]
    food_points: FieldValueV0[float]
    source_backend: str
    privileged_fields_present: tuple[str, ...] = ()
    schema_version: str = field(default="mc2p.observation.v1", init=False)

    def __post_init__(self) -> None:
        require_identifier(self.episode_id, "episode_id")
        require_identifier(self.source_backend, "source_backend")
        require_nonnegative_int(self.sequence_id, "sequence_id")
        if self.request_sequence_id is not None:
            require_nonnegative_int(
                self.request_sequence_id,
                "request_sequence_id",
            )
        require_nonnegative_int(
            self.observed_at_monotonic_ns,
            "observed_at_monotonic_ns",
        )
        require_nonnegative_int(
            self.received_at_monotonic_ns,
            "received_at_monotonic_ns",
        )
        if self.received_at_monotonic_ns < self.observed_at_monotonic_ns:
            raise ContractViolation("received time cannot precede observed time")
        self._validate_fields()
        if type(self.privileged_fields_present) is not tuple:
            raise ContractViolation("privileged fields must be a tuple of names")
        for name in self.privileged_fields_present:
            require_field_name(name, "privileged field")
        if self.privileged_fields_present != tuple(
            sorted(set(self.privileged_fields_present))
        ):
            raise ContractViolation("privileged fields must be sorted and unique")

    def _validate_fields(self) -> None:
        expected_types: tuple[tuple[str, FieldValueV0[Any], type], ...] = (
            ("world_time_ticks", self.world_time_ticks, int),
            ("position", self.position, Vec3V0),
            ("yaw_degrees", self.yaw_degrees, (int, float)),
            ("pitch_degrees", self.pitch_degrees, (int, float)),
            ("is_on_ground", self.is_on_ground, bool),
            ("is_dead", self.is_dead, bool),
            ("health_points", self.health_points, (int, float)),
            ("food_points", self.food_points, (int, float)),
        )
        for name, wrapped, expected in expected_types:
            if not isinstance(wrapped, FieldValueV0):
                raise ContractViolation(f"{name} must be FieldValueV0")
            if wrapped.status is not FieldStatusV0.VALID:
                continue
            value = wrapped.value
            if expected is bool:
                valid_type = type(value) is bool
            elif expected is int:
                valid_type = type(value) is int
            else:
                valid_type = isinstance(value, expected)
            if not valid_type:
                raise ContractViolation(f"{name} has invalid value type")
            if name in {
                "yaw_degrees",
                "pitch_degrees",
                "health_points",
                "food_points",
            }:
                require_finite(value, name)  # type: ignore[arg-type]
            if name in {"world_time_ticks", "health_points", "food_points"}:
                if float(value) < 0:  # type: ignore[arg-type]
                    raise ContractViolation(f"{name} cannot be negative")
