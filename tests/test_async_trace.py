from __future__ import annotations

import threading
import time
import unittest


class _MemorySink:
    def __init__(self) -> None:
        self.records: list[tuple[str, object]] = []
        self.closed = False

    def write(self, record_type: str, payload: object) -> None:
        self.records.append((record_type, payload))

    def close(self) -> None:
        self.closed = True


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


class BoundedAsyncTraceTests(unittest.TestCase):
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
        self.assertEqual(summary["dropped_by_record_type"]["observation"], writer.stats.dropped_records)
        self.assertEqual(summary["peak_depth"], 2)
        self.assertFalse(summary["worker_failed"])
        self.assertGreaterEqual(summary["projection_timing_ns"]["sample_count"], 33)

    def test_payload_is_snapshotted_before_caller_mutates_it(self) -> None:
        from mc2p.runtime.async_trace import BoundedAsyncTraceWriter

        sink = _MemorySink()
        writer = BoundedAsyncTraceWriter(sink, capacity=2)
        payload = {"items": [1]}
        writer.write("observation", payload)
        payload["items"].append(2)
        writer.close()
        self.assertEqual(sink.records[0], ("observation", {"items": [1]}))

    def test_worker_failure_is_reported_by_next_write_and_close(self) -> None:
        from mc2p.runtime.async_trace import BoundedAsyncTraceWriter

        for operation in ("write", "close"):
            with self.subTest(operation=operation):
                sink = _FailingSink()
                writer = BoundedAsyncTraceWriter(sink, capacity=2)
                writer.write("observation", {"index": 0})
                self.assertTrue(sink.failed.wait(1.0))
                deadline = time.monotonic() + 1.0
                while not writer.stats.worker_failed and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertTrue(writer.stats.worker_failed)
                if operation == "write":
                    with self.assertRaisesRegex(RuntimeError, "worker failed"):
                        writer.write("observation", {"index": 1})
                with self.assertRaisesRegex(RuntimeError, "worker failed"):
                    writer.close()

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


if __name__ == "__main__":
    unittest.main()
