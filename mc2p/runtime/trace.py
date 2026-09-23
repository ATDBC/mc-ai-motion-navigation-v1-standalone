"""Strict, flushed JSONL evidence for runtime transitions."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Any, Mapping, Protocol


class TraceSinkV0(Protocol):
    def write(self, record_type: str, payload: object) -> None: ...

    def close(self) -> None: ...


def trace_projection(value: Any) -> Any:
    # Dense structured observations are mostly exact JSON scalars. Preserve the
    # same finite/type contract without classifying each leaf as a structure.
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("trace values must be finite")
        return value
    if isinstance(value, bytes):
        return {
            "byte_length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: trace_projection(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("trace mapping keys must be strings")
        return {
            key: trace_projection(item)
            for key, item in sorted(value.items())
        }
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
