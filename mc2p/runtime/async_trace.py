"""Bounded non-blocking handoff to an existing trace sink."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import math
from queue import Full, Queue
import threading
import time

from mc2p.runtime.trace import TraceSinkV0, trace_projection


_STOP = object()


@dataclass(frozen=True, slots=True)
class AsyncTraceStats:
    accepted_records: int
    written_records: int
    dropped_records: int
    capacity: int
    peak_depth: int
    worker_failed: bool


class BoundedAsyncTraceWriter:
    """Snapshot on the caller, write on one worker, and never wait on a full queue."""

    def __init__(self, sink: TraceSinkV0, *, capacity: int = 256) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("trace queue capacity must be a positive integer")
        if not callable(getattr(sink, "write", None)) or not callable(getattr(sink, "close", None)):
            raise TypeError("trace sink must provide write and close")
        self._sink = sink
        self._capacity = capacity
        self._queue: Queue[object] = Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._accepted = self._written = self._dropped = self._peak = 0
        self._dropped_by_type: Counter[str] = Counter()
        self._projection_samples: deque[int] = deque(maxlen=4096)
        self._projection_count = self._projection_total = self._projection_max = 0
        self._worker_error: BaseException | None = None
        self._closing = self._closed = False
        self._worker = threading.Thread(
            target=self._run, name="mc2p-trace-writer", daemon=False,
        )
        self._worker.start()

    @property
    def stats(self) -> AsyncTraceStats:
        with self._lock:
            return AsyncTraceStats(
                self._accepted, self._written, self._dropped,
                self._capacity, self._peak, self._worker_error is not None,
            )

    def _raise_worker_error(self) -> None:
        if self._worker_error is not None:
            raise RuntimeError("asynchronous trace worker failed") from self._worker_error

    def write(self, record_type: str, payload: object) -> None:
        if type(record_type) is not str or not record_type:
            raise ValueError("record_type must be non-empty")
        with self._lock:
            if self._closed or self._closing:
                raise ValueError("asynchronous trace writer is closed")
            if self._worker_error is not None:
                self._dropped += 1
                self._dropped_by_type[record_type] += 1
                return
        snapshot = self._snapshot_payload(payload)
        with self._lock:
            if self._closed or self._closing:
                raise ValueError("asynchronous trace writer is closed")
            if self._worker_error is not None:
                self._dropped += 1
                self._dropped_by_type[record_type] += 1
                return
            try:
                self._queue.put_nowait((record_type, snapshot))
            except Full:
                self._dropped += 1
                self._dropped_by_type[record_type] += 1
            else:
                self._accepted += 1
                self._peak = max(self._peak, self._queue.qsize())

    def _drop_accepted(self, record_type: str) -> None:
        with self._lock:
            self._dropped += 1
            self._dropped_by_type[record_type] += 1

    def _run(self) -> None:
        failed = False
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                record_type, payload = item
                if failed:
                    self._drop_accepted(record_type)
                    continue
                try:
                    started = time.perf_counter_ns()
                    projected = trace_projection(payload)
                    elapsed = time.perf_counter_ns() - started
                    with self._lock:
                        self._projection_count += 1
                        self._projection_total += elapsed
                        self._projection_max = max(self._projection_max, elapsed)
                        self._projection_samples.append(elapsed)
                    self._sink.write(record_type, projected)
                except BaseException as error:
                    with self._lock:
                        if self._worker_error is None:
                            self._worker_error = error
                    self._drop_accepted(record_type)
                    failed = True
                else:
                    with self._lock:
                        self._written += 1
            finally:
                self._queue.task_done()

    @classmethod
    def _snapshot_payload(cls, value: object) -> object:
        """Copy mutable containers while retaining immutable typed records.

        Runtime contracts are frozen dataclasses.  Copying only their small
        surrounding containers keeps caller mutation from changing evidence
        without walking and projecting an entire observation on the control
        thread.
        """
        if type(value) is dict:
            return {
                cls._snapshot_payload(key): cls._snapshot_payload(item)
                for key, item in value.items()
            }
        if type(value) is list:
            return [cls._snapshot_payload(item) for item in value]
        if type(value) is tuple:
            return tuple(cls._snapshot_payload(item) for item in value)
        if type(value) is set:
            return frozenset(cls._snapshot_payload(item) for item in value)
        return value

    @staticmethod
    def _percentile(samples: tuple[int, ...], fraction: float) -> int:
        if not samples:
            return 0
        ordered = sorted(samples)
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]

    def _summary(self) -> dict[str, object]:
        with self._lock:
            samples = tuple(self._projection_samples)
            return {
                "schema_version": "mc2p.trace-delivery-summary.v1",
                "accepted_records": self._accepted,
                "written_records": self._written,
                "dropped_records": self._dropped,
                "dropped_by_record_type": dict(sorted(self._dropped_by_type.items())),
                "capacity": self._capacity,
                "peak_depth": self._peak,
                "worker_failed": self._worker_error is not None,
                "projection_timing_ns": {
                    "count": self._projection_count,
                    "sample_count": len(samples),
                    "total": self._projection_total,
                    "maximum": self._projection_max,
                    "p95": self._percentile(samples, .95),
                    "p99": self._percentile(samples, .99),
                },
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                self._raise_worker_error()
                return
            self._closing = True
        self._queue.put(_STOP)
        self._worker.join()
        error: BaseException | None = None
        with self._lock:
            error = self._worker_error
        try:
            if error is None:
                self._sink.write("trace_delivery_summary", self._summary())
        except BaseException as summary_error:
            error = summary_error
            with self._lock:
                if self._worker_error is None:
                    self._worker_error = summary_error
        try:
            self._sink.close()
        except BaseException as close_error:
            if error is None:
                error = close_error
                with self._lock:
                    if self._worker_error is None:
                        self._worker_error = close_error
        finally:
            with self._lock:
                self._closed = True
        if error is not None:
            raise RuntimeError("asynchronous trace worker failed") from error
