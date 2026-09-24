"""POV debug frames kept outside the formal Observation contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mc2p.contracts.common import ContractViolation


@dataclass(frozen=True, slots=True)
class DebugFrameV0:
    sequence_id: int
    eye: str
    width: int
    height: int
    channels: int
    dtype: str
    payload: bytes
    schema_version: str = "mc2p.debug-frame.v0"

    def __post_init__(self) -> None:
        if type(self.sequence_id) is not int or self.sequence_id < 0:
            raise ContractViolation("debug frame sequence_id must be non-negative")
        if self.eye not in {"primary", "secondary"}:
            raise ContractViolation("debug frame eye is invalid")
        for name in ("width", "height", "channels"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ContractViolation(f"debug frame {name} must be positive")
        if not isinstance(self.dtype, str) or not self.dtype:
            raise ContractViolation("debug frame dtype must be non-empty")
        if not isinstance(self.payload, bytes):
            raise ContractViolation("debug frame payload must be bytes")


class DebugFrameSinkV0(Protocol):
    def write(self, frame: DebugFrameV0) -> None:
        """Consume one explicitly requested debug frame."""
