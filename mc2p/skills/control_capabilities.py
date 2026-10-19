"""Small current control-capability contract, independent of old probe readers.

The class describes controls already authorized for one bounded consumer. It
does not load historical evidence, know a sensor layout, or select a backend.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from mc2p.contracts.action_v1 import MovementV1


SCHEMA = "mc2p.normal-control-capability.v2"


@dataclass(frozen=True)
class ControlCapabilities:
    allowed_controls: tuple[tuple[int, int, str], ...]
    source_fingerprints: tuple[tuple[str, str], ...]
    source_root: Path
    measured_look_degrees_per_second: float | None = None
    measured_speed_blocks_per_second: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "allowed_controls", tuple(tuple(x) for x in self.allowed_controls))
        object.__setattr__(self, "source_fingerprints", tuple(tuple(x) for x in self.source_fingerprints))
        object.__setattr__(self, "source_root", Path(self.source_root).resolve())

    @property
    def measurements(self):
        return MappingProxyType({
            "normal_speed_blocks_per_second": self.measured_speed_blocks_per_second,
            "look_rate_degrees_per_second": self.measured_look_degrees_per_second,
            "sample_delivery_ns": None,
        })

    def allows(self, movement: MovementV1, look_axes: str) -> bool:
        if type(movement) is not MovementV1 or movement.jump or movement.sprint or movement.sneak:
            return False
        key = (movement.forward, movement.strafe, look_axes)
        return key == (0, 0, "fixed") or key in self.allowed_controls

    def verify_current_sources(self, root: Path | None = None) -> None:
        base = self.source_root if root is None else Path(root).resolve()
        for name, digest in self.source_fingerprints:
            path = (base / name).resolve()
            if not path.is_relative_to(base) or not path.is_file():
                raise ValueError("control source is unavailable: " + name)
            import hashlib
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest:
                raise ValueError("control source differs: " + name)
