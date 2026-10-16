"""Bounded non-blocking handoff to an existing trace sink."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from enum import StrEnum
import math
from queue import Empty, Full, Queue
import threading
import time

from mc2p.runtime.trace import TraceSinkV0, trace_projection


@dataclass(frozen=True, slots=True)
class AsyncTraceStats:
    accepted_records: int
    written_records: int
    dropped_records: int
    capacity: int
    peak_depth: int
    worker_failed: bool


class AsyncTraceCloseStatus(StrEnum):
    SEALED = "sealed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class AsyncTraceCleanupFailure:
    stage: str
    exception_type: str
    message: str


@dataclass(frozen=True, slots=True)
class AsyncTraceCloseResult:
    status: AsyncTraceCloseStatus
    sealed: bool
    worker_alive: bool
    accepted: int
    written: int
    dropped: int
    indeterminate_or_pending: int
    cleanup_failure: AsyncTraceCleanupFailure | None
    rejected_before_accept: int
    accepted_dropped: int
    accepted_indeterminate: int


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
        self._rejected_before_accept = self._accepted_dropped = 0
        self._accepted_indeterminate = 0
        self._dropped_by_type: Counter[str] = Counter()
        self._indeterminate_by_type: Counter[str] = Counter()
        self._projection_samples: deque[int] = deque(maxlen=4096)
        self._projection_count = self._projection_total = self._projection_max = 0
        self._worker_error: BaseException | None = None
        self._cleanup_failure: AsyncTraceCleanupFailure | None = None
        self._closing = self._closed = False
        self._sealed = False
        self._stop_requested = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="mc2p-trace-writer", daemon=True,
        )
        self._worker.start()

    @property
    def stats(self) -> AsyncTraceStats:
        with self._lock:
            return AsyncTraceStats(
                self._accepted, self._written, self._dropped,
                self._capacity, self._peak, self._worker_error is not None,
            )

    def write(self, record_type: str, payload: object) -> None:
        if type(record_type) is not str or not record_type:
            raise ValueError("record_type must be non-empty")
        with self._lock:
            if self._closed or self._closing:
                raise ValueError("asynchronous trace writer is closed")
            if self._worker_error is not None:
                self._dropped += 1
                self._rejected_before_accept += 1
                self._dropped_by_type[record_type] += 1
                return
        snapshot = self._snapshot_payload(payload)
        with self._lock:
            if self._closed or self._closing:
                raise ValueError("asynchronous trace writer is closed")
            if self._worker_error is not None:
                self._dropped += 1
                self._rejected_before_accept += 1
                self._dropped_by_type[record_type] += 1
                return
            try:
                self._queue.put_nowait((record_type, snapshot))
            except Full:
                self._dropped += 1
                self._rejected_before_accept += 1
                self._dropped_by_type[record_type] += 1
            else:
                self._accepted += 1
                self._peak = max(self._peak, self._queue.qsize())

    def _drop_accepted(self, record_type: str) -> None:
        with self._lock:
            self._dropped += 1
            self._accepted_dropped += 1
            self._dropped_by_type[record_type] += 1

    def _mark_accepted_indeterminate(self, record_type: str) -> None:
        with self._lock:
            self._accepted_indeterminate += 1
            self._indeterminate_by_type[record_type] += 1

    @staticmethod
    def _failure(stage: str, error: BaseException) -> AsyncTraceCleanupFailure:
        return AsyncTraceCleanupFailure(
            stage, type(error).__name__, str(error),
        )

    def _remember_failure(self, stage: str, error: BaseException) -> None:
        with self._lock:
            if self._worker_error is None:
                self._worker_error = error
            if self._cleanup_failure is None:
                self._cleanup_failure = self._failure(stage, error)

    def _run(self) -> None:
        failed = False
        sink_close_attempted = False
        try:
            while True:
                try:
                    item = self._queue.get(timeout=.05)
                except Empty:
                    if self._stop_requested.is_set():
                        break
                    continue
                try:
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
                    except BaseException as error:
                        self._remember_failure("trace_projection", error)
                        self._drop_accepted(record_type)
                        failed = True
                        continue
                    try:
                        write_projected = getattr(self._sink, "write_projected", None)
                        if callable(write_projected):
                            write_projected(record_type, projected)
                        else:
                            self._sink.write(record_type, projected)
                    except BaseException as error:
                        self._remember_failure("sink_write", error)
                        self._mark_accepted_indeterminate(record_type)
                        failed = True
                    else:
                        with self._lock:
                            self._written += 1
                finally:
                    self._queue.task_done()
                if self._stop_requested.is_set() and self._queue.empty():
                    break

            if not failed:
                try:
                    self._sink.write("trace_delivery_summary", self._summary())
                except BaseException as error:
                    self._remember_failure("summary_write", error)
                    failed = True
            try:
                sink_close_attempted = True
                self._sink.close()
            except BaseException as error:
                self._remember_failure("sink_close", error)
                failed = True
            with self._lock:
                self._sealed = not failed
        except BaseException as error:
            self._remember_failure("worker", error)
            failed = True
        finally:
            if not sink_close_attempted:
                try:
                    sink_close_attempted = True
                    self._sink.close()
                except BaseException as error:
                    self._remember_failure("sink_close", error)
                    failed = True
            with self._lock:
                self._sealed = not failed
                self._closed = True

    def _close_result(self) -> AsyncTraceCloseResult:
        worker_alive = self._worker.is_alive()
        with self._lock:
            pending = max(
                0,
                self._accepted - self._written - self._accepted_dropped
                - self._accepted_indeterminate,
            )
            indeterminate_or_pending = self._accepted_indeterminate + pending
            cleanup_failure = self._cleanup_failure
            sealed = self._sealed and not worker_alive
            if worker_alive:
                cleanup_failure = AsyncTraceCleanupFailure(
                    "worker_join", "TimeoutError",
                    "asynchronous trace worker did not stop before close deadline",
                )
            return AsyncTraceCloseResult(
                AsyncTraceCloseStatus.SEALED if sealed
                else AsyncTraceCloseStatus.INCOMPLETE,
                sealed,
                worker_alive,
                self._accepted,
                self._written,
                self._dropped,
                indeterminate_or_pending,
                cleanup_failure,
                self._rejected_before_accept,
                self._accepted_dropped,
                self._accepted_indeterminate,
            )

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
                "accepted_dropped_records": self._accepted_dropped,
                "accepted_indeterminate_records": self._accepted_indeterminate,
                "indeterminate_by_record_type": dict(sorted(
                    self._indeterminate_by_type.items()
                )),
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

    def close(self, *, timeout_seconds: float = 5.0) -> AsyncTraceCloseResult:
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(float(timeout_seconds))
                or timeout_seconds < 0):
            raise ValueError("trace close timeout must be finite and nonnegative")
        deadline = time.monotonic() + min(float(timeout_seconds), 30.0)
        with self._lock:
            self._closing = True
        self._stop_requested.set()
        if self._worker is not threading.current_thread():
            self._worker.join(max(0.0, deadline - time.monotonic()))
        return self._close_result()
