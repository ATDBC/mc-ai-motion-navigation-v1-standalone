"""Strict, flushed JSONL evidence for runtime transitions."""

from __future__ import annotations

from dataclasses import fields
from enum import Enum
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Any, Mapping, Protocol

from mc2p.contracts.observation_v3 import ObservedBlockV3


class TraceSinkV0(Protocol):
    def write(self, record_type: str, payload: object) -> None: ...

    def close(self) -> None: ...


@lru_cache(maxsize=256)
def _dataclass_field_names(value_type: type) -> tuple[str, ...]:
    return tuple(item.name for item in fields(value_type))


def _observed_block_projection(value: ObservedBlockV3) -> dict[str, Any]:
    """Project the high-volume immutable block leaf without reflection."""
    collision = value.collision
    return {
        "position": list(value.position),
        "block_id": value.block_id,
        "collision": {
            "kind": collision.kind,
            "boxes": [
                {
                    "min_x": box.min_x,
                    "min_y": box.min_y,
                    "min_z": box.min_z,
                    "max_x": box.max_x,
                    "max_y": box.max_y,
                    "max_z": box.max_z,
                }
                for box in collision.boxes
            ],
            "reason": collision.reason,
        },
        "fluid_id": value.fluid_id,
        "sources": list(value.sources),
    }


def trace_projection(value: Any) -> Any:
    # Dense structured observations are mostly exact JSON scalars. Preserve the
    # same finite/type contract without classifying each leaf as a structure.
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is ObservedBlockV3:
        return _observed_block_projection(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("trace values must be finite")
        return value
    if type(value) in (tuple, list):
        return [trace_projection(item) for item in value]
    if type(value) is dict:
        if not all(isinstance(key, str) for key in value):
            raise TypeError("trace mapping keys must be strings")
        return {
            key: trace_projection(item)
            for key, item in sorted(value.items())
        }
    if type(value) is frozenset:
        projected = [trace_projection(item) for item in value]
        return sorted(projected, key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True,
        ))
    if isinstance(value, bytes):
        return {
            "byte_length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, Enum):
        return value.value
    if not isinstance(value, type) and hasattr(type(value), "__dataclass_fields__"):
        return {
            name: trace_projection(getattr(value, name))
            for name in _dataclass_field_names(type(value))
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("trace mapping keys must be strings")
        return {
            key: trace_projection(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, frozenset):
        projected = [trace_projection(item) for item in value]
        return sorted(projected, key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True,
        ))
    if isinstance(value, (tuple, list)):
        return [trace_projection(item) for item in value]
    raise TypeError(f"unsupported trace value: {type(value).__name__}")


class JsonlTraceWriterV0:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("x", encoding="utf-8", newline="\n")
        self._closed = False
        self._lock = threading.Lock()

    def write(self, record_type: str, payload: object) -> None:
        if not record_type or not isinstance(record_type, str):
            raise ValueError("record_type must be non-empty")
        record = {
            "schema_version": "mc2p.trace-record.v0",
            "record_type": record_type,
            "payload": trace_projection(payload),
        }
        line = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
        with self._lock:
            if self._closed:
                raise ValueError("trace writer is closed")
            self._stream.write(line + "\n")
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._stream.close()
            self._closed = True


class NullTraceWriterV0:
    def write(self, record_type: str, payload: object) -> None:
        return None

    def close(self) -> None:
        return None
