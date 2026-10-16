from __future__ import annotations

import threading
import time
import unittest
import subprocess
import sys
from pathlib import Path


class _MemorySink:
    def __init__(self) -> None:
        self.records: list[tuple[str, object]] = []
        self.closed = False

    def write(self, record_type: str, payload: object) -> None:
        self.records.append((record_type, payload))

    def close(self) -> None:
        self.closed = True


class _ProjectedMemorySink(_MemorySink):
    def __init__(self) -> None:
        super().__init__()
        self.projected_records: list[tuple[str, object]] = []

    def write_projected(self, record_type: str, payload: object) -> None:
        self.projected_records.append((record_type, payload))


class _BlockingSink(_MemorySink):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def write(self, record_type: str, payload: object) -> None:
        self.started.set()
        if not self.release.wait(5.0):
            raise TimeoutError("test sink remained blocked")
        super().write(record_type, payload)


class _FailingSink(_MemorySink):
    def __init__(self) -> None:
        super().__init__()
        self.failed = threading.Event()

    def write(self, record_type: str, payload: object) -> None:
        self.failed.set()
        raise OSError("trace disk failed")


class _IndefinitelyBlockingSink(_MemorySink):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def write(self, record_type: str, payload: object) -> None:
        self.started.set()
        self.release.wait()
        super().write(record_type, payload)


class _CloseFailingSink(_MemorySink):
    def close(self) -> None:
        raise OSError("trace close failed")


class _PartialWriteThenFailSink(_MemorySink):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def write(self, record_type: str, payload: object) -> None:
        super().write(record_type, payload)
        self.started.set()
        if not self.release.wait(1.0):
            raise TimeoutError("partial-write test did not release sink")
        raise OSError("sink failed after producing a side effect")


class _CloseCountingSink(_MemorySink):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class BoundedAsyncTraceTests(unittest.TestCase):
    def test_observed_block_projection_avoids_generic_dataclass_reflection(self) -> None:
        from unittest.mock import patch
        from mc2p.contracts.observation_v3 import CollisionShapeV3, ObservedBlockV3
        from mc2p.runtime.trace import trace_projection

        block = ObservedBlockV3(
            (1, 64, -2), "minecraft:stone",
            CollisionShapeV3("full_cube"), None, ("surface_depth",),
        )
        with patch(
            "mc2p.runtime.trace._dataclass_field_names",
            side_effect=AssertionError("generic dataclass path used"),
        ):
            projected = trace_projection(block)

        self.assertEqual(projected, {
            "position": [1, 64, -2],
            "block_id": "minecraft:stone",
            "collision": {"kind": "full_cube", "boxes": [], "reason": None},
            "fluid_id": None,
            "sources": ["surface_depth"],
        })

    def test_blocked_consumer_never_blocks_control_and_reports_drops(self) -> None:
        from mc2p.runtime.async_trace import BoundedAsyncTraceWriter

        sink = _BlockingSink()
        writer = BoundedAsyncTraceWriter(sink, capacity=2)
        writer.write("observation", {"index": 0})
        self.assertTrue(sink.started.wait(1.0))

        started = time.perf_counter_ns()
        for index in range(1, 33):
            writer.write("observation", {"index": index})
        elapsed = time.perf_counter_ns() - started

        self.assertLess(elapsed, 50_000_000)
        self.assertGreater(writer.stats.dropped_records, 0)
        self.assertLessEqual(writer.stats.peak_depth, 2)
        sink.release.set()
        writer.close()

        self.assertTrue(sink.closed)
        self.assertEqual(sink.records[-1][0], "trace_delivery_summary")
        summary = sink.records[-1][1]
        self.assertEqual(summary["schema_version"], "mc2p.trace-delivery-summary.v1")
        self.assertEqual(summary["capacity"], 2)
        self.assertEqual(summary["dropped_records"], writer.stats.dropped_records)
        self.assertEqual(summary["accepted_indeterminate_records"], 0)
        self.assertEqual(summary["accepted_dropped_records"], 0)
        self.assertEqual(summary["dropped_by_record_type"]["observation"], writer.stats.dropped_records)
        self.assertEqual(summary["peak_depth"], 2)
        self.assertFalse(summary["worker_failed"])
        self.assertGreaterEqual(summary["projection_timing_ns"]["sample_count"], 1)
        self.assertLessEqual(
            summary["projection_timing_ns"]["sample_count"],
            summary["accepted_records"],
        )

    def test_payload_is_snapshotted_before_caller_mutates_it(self) -> None:
        from mc2p.runtime.async_trace import BoundedAsyncTraceWriter

        sink = _MemorySink()
        writer = BoundedAsyncTraceWriter(sink, capacity=2)
        payload = {"items": [1]}
        writer.write("observation", payload)
        payload["items"].append(2)
        writer.close()
        self.assertEqual(sink.records[0], ("observation", {"items": [1]}))

    def test_worker_failure_degrades_evidence_without_stopping_control(self) -> None:
        from mc2p.runtime.async_trace import (
            AsyncTraceCloseStatus, BoundedAsyncTraceWriter,
        )

        sink = _FailingSink()
        writer = BoundedAsyncTraceWriter(sink, capacity=2)
        writer.write("observation", {"index": 0})
        self.assertTrue(sink.failed.wait(1.0))
        deadline = time.monotonic() + 1.0
        while not writer.stats.worker_failed and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertTrue(writer.stats.worker_failed)
        dropped = writer.stats.dropped_records

        # This is the control-thread contract: evidence is incomplete, but a
        # later frame is not failed merely because the writer already died.
        writer.write("observation", {"index": 1})
        self.assertEqual(writer.stats.dropped_records, dropped + 1)
        result = writer.close()
        self.assertIs(result.status, AsyncTraceCloseStatus.INCOMPLETE)
        self.assertFalse(result.sealed)
        self.assertIsNotNone(result.cleanup_failure)
        self.assertEqual(
            result.accepted,
            result.written + result.accepted_dropped
            + result.indeterminate_or_pending,
        )
        self.assertEqual(result.accepted_indeterminate, 1)
        self.assertEqual(result.indeterminate_or_pending, 1)
        self.assertEqual(result.dropped, 1)

    def test_write_side_effect_then_error_is_indeterminate_not_dropped(self) -> None:
        from mc2p.runtime.async_trace import (
            AsyncTraceCloseStatus, BoundedAsyncTraceWriter,
        )

        sink = _PartialWriteThenFailSink()
        writer = BoundedAsyncTraceWriter(sink, capacity=2)
        writer.write("partial", {"index": 0})
        self.assertTrue(sink.started.wait(1.0))
        writer.write("never_attempted", {"index": 1})
        sink.release.set()

        result = writer.close()

        self.assertIs(result.status, AsyncTraceCloseStatus.INCOMPLETE)
        self.assertEqual(result.accepted, 2)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.accepted_indeterminate, 1)
        self.assertEqual(result.accepted_dropped, 1)
        self.assertEqual(result.indeterminate_or_pending, 1)
        self.assertEqual(result.dropped, 1)
        self.assertEqual(result.rejected_before_accept, 0)
        self.assertEqual(
            result.accepted,
            result.written + result.accepted_dropped
            + result.indeterminate_or_pending,
        )
        self.assertEqual(sink.records, [("partial", {"index": 0})])

    def test_close_is_bounded_when_queue_is_full_and_sink_never_returns(self) -> None:
        from mc2p.runtime.async_trace import (
            AsyncTraceCloseStatus, BoundedAsyncTraceWriter,
        )

        sink = _IndefinitelyBlockingSink()
        writer = BoundedAsyncTraceWriter(sink, capacity=1)
        writer.write("first", {"index": 0})
        self.assertTrue(sink.started.wait(1.0))
        writer.write("queued", {"index": 1})
        writer.write("rejected", {"index": 2})

        started = time.monotonic()
        result = writer.close(timeout_seconds=.05)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, .5)
        self.assertIs(result.status, AsyncTraceCloseStatus.INCOMPLETE)
        self.assertTrue(result.worker_alive)
        self.assertFalse(result.sealed)
        self.assertEqual(result.accepted, 2)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.dropped, 1)
        self.assertEqual(result.indeterminate_or_pending, 2)
        self.assertEqual(
            result.accepted,
            result.written + result.accepted_dropped
            + result.indeterminate_or_pending,
        )
        self.assertEqual(result.rejected_before_accept, 1)
        self.assertEqual(result.accepted_dropped, 0)
        self.assertIsNotNone(result.cleanup_failure)
        self.assertFalse(sink.closed, "owner closed a sink still used by worker")

        repeated_incomplete = writer.close(timeout_seconds=0.0)
        self.assertIs(repeated_incomplete.status, AsyncTraceCloseStatus.INCOMPLETE)
        self.assertTrue(repeated_incomplete.worker_alive)
        self.assertEqual(repeated_incomplete.accepted, result.accepted)
        self.assertFalse(sink.closed, "repeated close raced the worker for its sink")

        sink.release.set()
        final = writer.close(timeout_seconds=1.0)
        self.assertIs(final.status, AsyncTraceCloseStatus.SEALED)
        self.assertTrue(final.sealed)
        self.assertFalse(final.worker_alive)
        self.assertEqual(final.accepted, 2)
        self.assertEqual(final.written, 2)
        self.assertEqual(final.indeterminate_or_pending, 0)
        self.assertTrue(sink.closed)

        repeated = writer.close(timeout_seconds=0.0)
        self.assertEqual(repeated, final)

    def test_sink_block_that_never_releases_does_not_block_process_exit(self) -> None:
        root = Path(__file__).resolve().parents[1]
        code = """
import threading
from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
class Sink:
    def __init__(self): self.started=threading.Event(); self.gate=threading.Event()
    def write(self, kind, payload): self.started.set(); self.gate.wait()
    def close(self): pass
sink=Sink(); writer=BoundedAsyncTraceWriter(sink, capacity=1)
writer.write('blocked', {'value': 1})
assert sink.started.wait(1.0)
"""
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=root,
            capture_output=True, text=True, timeout=3.0,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_sink_close_failure_returns_typed_incomplete_result(self) -> None:
        from mc2p.runtime.async_trace import (
            AsyncTraceCloseStatus, BoundedAsyncTraceWriter,
        )

        writer = BoundedAsyncTraceWriter(_CloseFailingSink(), capacity=1)
        writer.write("observation", {"index": 0})

        result = writer.close()

        self.assertIs(result.status, AsyncTraceCloseStatus.INCOMPLETE)
        self.assertFalse(result.worker_alive)
        self.assertFalse(result.sealed)
        self.assertEqual(result.accepted, 1)
        self.assertEqual(result.written, 1)
        self.assertEqual(result.indeterminate_or_pending, 0)
        self.assertEqual(result.cleanup_failure.stage, "sink_close")

    def test_concurrent_close_is_bounded_and_closes_sink_once(self) -> None:
        from mc2p.runtime.async_trace import (
            AsyncTraceCloseStatus, BoundedAsyncTraceWriter,
        )

        sink = _CloseCountingSink()
        writer = BoundedAsyncTraceWriter(sink, capacity=4)
        writer.write("observation", {"index": 0})
        barrier = threading.Barrier(20)
        results = []
        errors = []

        def close_from_thread() -> None:
            try:
                barrier.wait(timeout=2.0)
                results.append(writer.close(timeout_seconds=2.0))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=close_from_thread) for _ in range(20)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3.0)

        self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 20)
        self.assertTrue(all(
            result.status is AsyncTraceCloseStatus.SEALED
            for result in results
        ))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(sink.close_calls, 1)

    def test_projection_runs_on_the_worker_not_the_control_thread(self) -> None:
        from unittest.mock import patch
        from mc2p.runtime.async_trace import BoundedAsyncTraceWriter

        caller = threading.get_ident()
        projection_threads = []

        def project(payload):
            projection_threads.append(threading.get_ident())
            return payload

        with patch("mc2p.runtime.async_trace.trace_projection", side_effect=project):
            writer = BoundedAsyncTraceWriter(_MemorySink(), capacity=2)
            writer.write("observation", {"index": 0})
            writer.close()

        self.assertTrue(projection_threads)
        self.assertNotIn(caller, projection_threads)

    def test_projected_sink_does_not_project_the_same_payload_again(self) -> None:
        from unittest.mock import patch
        from mc2p.runtime.async_trace import BoundedAsyncTraceWriter

        sink = _ProjectedMemorySink()
        calls = []

        def project(payload):
            calls.append(payload)
            return {"items": [1, 2]}

        with patch("mc2p.runtime.async_trace.trace_projection", side_effect=project):
            writer = BoundedAsyncTraceWriter(sink, capacity=2)
            writer.write("observation", {"items": (1, 2)})
            writer.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            sink.projected_records,
            [("observation", {"items": [1, 2]})],
        )
        self.assertEqual(sink.records[-1][0], "trace_delivery_summary")

    def test_invalid_capacity_record_type_and_closed_use_are_rejected(self) -> None:
        from mc2p.runtime.async_trace import BoundedAsyncTraceWriter

        for capacity in (0, -1, True, 1.5):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                BoundedAsyncTraceWriter(_MemorySink(), capacity=capacity)
        writer = BoundedAsyncTraceWriter(_MemorySink(), capacity=1)
        with self.assertRaises(ValueError):
            writer.write("", {})
        writer.close()
        with self.assertRaises(ValueError):
            writer.write("observation", {})
        for timeout in (-1, float("inf"), float("nan"), True, "1"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                writer.close(timeout_seconds=timeout)


if __name__ == "__main__":
    unittest.main()
