"""Receiver delivery timing on the formal Runtime -> navigation path.

The client clock advances independently of the receiver.  The only receiver
barrier in this fixture follows an explicit result injection; the control loop
never waits for a receiver cycle before advancing a movement tick.
"""
from __future__ import annotations

from dataclasses import replace
import inspect
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.async_work import AsyncAdmissionDisposition
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.motion_worker import (
    MotionJobOperation,
    MotionResultInbox,
    _execute_job,
)
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1, RuntimeStateV1
from mc2p.skills.navigation_session_driver import (
    RuntimeNavigationDriver,
    RuntimeNavigationDriverState,
)
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_b09_air_transitions import air_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal
from tests.motion_nav.test_runtime_navigation_verified_handoff import (
    _AnchorInjectionSession,
    _GapRuntimeBackend,
)
from tests.test_player_runtime import _RecordingTrace
from scripts.b10_gap_solver_runtime import (
    _B10EvidenceBuffer,
    _B10_STREAM_SPECS,
    _ReceiverDiagnosticOverflow,
    _receiver_diagnostic_summary,
)
from scripts.probe_fabric_deployment_observation import _runtime_trace
from mc2p.runtime.async_trace import BoundedAsyncTraceWriter


class _DiagnosticInbox:
    def __init__(self, rows) -> None:
        self.rows = list(rows)

    def drain_receiver_diagnostics(self):
        rows, self.rows = tuple(self.rows), []
        return rows


class _TickScheduledWorker:
    instances: list["_TickScheduledWorker"] = []

    def __init__(self, **_kwargs) -> None:
        self.lock = threading.Lock()
        self.jobs = []
        self.results = []
        self.closed = False
        self.instances.append(self)

    def submit(self, job) -> bool:
        with self.lock:
            self.jobs.append(job)
        return True

    def poll_available(self):
        with self.lock:
            results, self.results = tuple(self.results), []
        return results

    def deliver(self, job) -> None:
        with self.lock:
            self.results.append(_execute_job(job))

    def close(self):
        self.closed = True

    def is_alive(self) -> bool:
        return not self.closed


class _ReceiverWaitGate:
    """Allow a receiver barrier only around this fixture's explicit delivery."""

    def __init__(self) -> None:
        self.allowed = False
        self.calls: list[bool] = []
        self._native = MotionResultInbox.wait_for_receiver

    def wait(self, inbox, *, timeout: float = 5.) -> bool:
        self.calls.append(self.allowed)
        if not self.allowed:
            raise AssertionError("receiver barrier was used outside explicit delivery")
        return self._native(inbox, timeout=timeout)

    def patch(self):
        gate = self

        def guarded(inbox, *, timeout: float = 5.) -> bool:
            return gate.wait(inbox, timeout=timeout)

        return patch.object(MotionResultInbox, "wait_for_receiver", guarded)

    def deliver(self, worker, inbox, job) -> None:
        worker.deliver(job)
        inbox.wake_receiver()
        self.allowed = True
        try:
            if not inbox.wait_for_receiver(timeout=5.):
                raise AssertionError("receiver did not publish the injected result")
        finally:
            self.allowed = False


class D108ReceiverTickDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        _TickScheduledWorker.instances.clear()

    def _fixture(self):
        clock = [100_000_000]
        backend = _GapRuntimeBackend(clock)
        runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
        self.assertTrue(runtime.reset(ResetRequestV0(
            "reset-d108", "episode-verified-runtime", "test", 1,
            10_000_000_000,
        )).succeeded)
        profiles = NavigationSessionProfiles(
            replace(
                ordinary_profile(),
                support_materials=frozenset({"minecraft:grass_block"}),
            ),
            jump_profile(),
            step_profile(),
            air=(air_profile(MovementMode.JUMP_GAP),),
        )
        session = _AnchorInjectionSession(
            "d108-receiver-session", profiles,
            planner_worker=_InlinePlanner(), clock_ns=lambda: clock[0],
        )
        driver = RuntimeNavigationDriver(runtime, session, clock_ns=lambda: clock[0])
        driver.start("d108-receiver-goal", 1, _goal((.5, 64.0, 2.5)), clock[0])
        self.addCleanup(runtime.close)
        self.addCleanup(session.close)
        return clock, backend, runtime, session, driver

    def _run_delay(self, delay_ticks: int) -> dict:
        _TickScheduledWorker.instances.clear()
        clock, backend, runtime, session, driver = self._fixture()
        delivered = set()
        submissions = {}
        operations = []
        publication_barriers = 0
        decisions = 0
        wait_gate = _ReceiverWaitGate()
        with patch("mc2p.motion_nav.navigation_session.MotionSolverWorker",
                   _TickScheduledWorker), wait_gate.patch():
            for _ in range(80):
                step = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                decisions += 1
                self.assertIsNone(step.report.failure)
                self.assertIs(runtime.state, RuntimeStateV1.READY)
                self.assertIsNot(driver.state, RuntimeNavigationDriverState.FAILED)
                if step.decision is not None and step.decision.action.movement.jump:
                    break
                if not _TickScheduledWorker.instances:
                    continue
                worker = _TickScheduledWorker.instances[0]
                for job in tuple(worker.jobs):
                    identity = job.work_identity
                    self.assertIsNotNone(identity)
                    submissions.setdefault(identity, backend.movement_tick)
                    if (identity not in delivered
                            and backend.movement_tick - submissions[identity]
                                >= delay_ticks):
                        operations.append((job.operation, identity))
                        wait_gate.deliver(
                            worker, session._motion_result_inbox, job,
                        )
                        delivered.add(identity)
                        publication_barriers += 1
            else:
                self.fail("formal runtime path did not reach a bounded result")

        control = session._supervisor.route
        coordinator = None if control is None else control.coordinator
        records = () if coordinator is None else coordinator.admission_records
        self.assertEqual(publication_barriers, len(delivered))
        self.assertEqual(len(wait_gate.calls), len(delivered))
        self.assertTrue(all(wait_gate.calls))
        self.assertLessEqual(decisions, 80)
        self.assertEqual(runtime.observation.health_points.value, 20.0)
        self.assertFalse(session.report.terminal)
        self.assertTrue(session.has_owned_body_control)
        self.assertLessEqual(len(session.active_motion_mailboxes), 2)
        self.assertLessEqual(session.diagnostics.retry_total_failures, 2)
        self.assertTrue(operations)
        self.assertEqual(len({identity for _, identity in operations}), len(operations))
        self.assertLessEqual(len(operations), 4)
        self.assertTrue(any(
            record.disposition is AsyncAdmissionDisposition.APPLIED
            for record in records
        ))
        operation_by_identity = {
            identity: operation for operation, identity in operations
        }
        admissions = tuple(
            (operation_by_identity[record.identity], record.disposition)
            for record in records
            if record.identity in operation_by_identity
        )
        return {
            "delay": delay_ticks,
            "operations": tuple(operation for operation, _ in operations),
            "movement_tick": backend.movement_tick,
            "barriers": publication_barriers,
            "admissions": admissions,
        }

    def test_solve_and_revalidate_arrive_zero_one_or_two_ticks_late(self) -> None:
        results = tuple(self._run_delay(delay) for delay in (0, 1, 2))

        self.assertEqual(tuple(result["delay"] for result in results), (0, 1, 2))
        self.assertTrue(all(
            result["operations"][0] is MotionJobOperation.SOLVE
            for result in results
        ))
        self.assertTrue(any(
            MotionJobOperation.REVALIDATE in result["operations"]
            for result in results
        ))
        self.assertEqual(
            tuple((result["operations"], result["movement_tick"], result["barriers"])
                  for result in results),
            (
                ((MotionJobOperation.SOLVE,), 4, 1),
                ((MotionJobOperation.SOLVE, MotionJobOperation.REVALIDATE), 7, 2),
                ((MotionJobOperation.SOLVE, MotionJobOperation.REVALIDATE), 9, 2),
            ),
        )
        self.assertEqual(results[0]["admissions"], (
            (MotionJobOperation.SOLVE, AsyncAdmissionDisposition.APPLIED),
        ))
        for result in results[1:]:
            self.assertEqual(result["admissions"], (
                (MotionJobOperation.SOLVE, AsyncAdmissionDisposition.RECOMPUTE),
                (MotionJobOperation.REVALIDATE, AsyncAdmissionDisposition.APPLIED),
            ))

    def test_receiver_summary_counts_next_decision_visibility_and_unavailable_ticks(self) -> None:
        summary = _receiver_diagnostic_summary([
            {"event": "receiver_cycle", "duration_ns": 4},
            {"event": "receiver_cycle", "duration_ns": 9},
            {"event": "worker_submit", "identity": "a", "movement_tick": 8,
             "monotonic_ns": 100},
            {"event": "receiver_observed_result", "identity": "a", "movement_tick": 10,
             "monotonic_ns": 180, "worker_elapsed_ns": 40,
             "worker_completed_monotonic_ns": 160},
            {"event": "registry_publish", "identity": "a", "movement_tick": 10},
            {"event": "first_control_consume", "identity": "a", "movement_tick": 12},
            {"event": "receiver_observed_result", "identity": "b", "movement_tick": None},
            {"event": "registry_publish", "identity": "b", "movement_tick": None},
        ])
        self.assertEqual(summary["cycle_count"], 2)
        self.assertEqual(summary["cycle_duration_ns"]["maximum"], 9)
        self.assertEqual(summary["delivery_count"], 2)
        self.assertEqual(summary["published_but_next_decision_invisible_count"], 1)
        self.assertEqual(summary["delivery_tick_unavailable_count"], 1)
        self.assertEqual(
            summary["worker_completion_to_receiver_observed"]["status"],
            "available",
        )
        self.assertEqual(
            summary["deliveries"][0]["receiver_observed_to_registry_publish_ticks"],
            0,
        )
        self.assertEqual(
            summary["deliveries"][0]["registry_publish_to_first_control_consume_ticks"],
            2,
        )
        self.assertEqual(summary["deliveries"][0]["worker_submit_to_observed_ticks"], 2)
        self.assertEqual(summary["deliveries"][0]["worker_queue_after_completion_ns"], 20)

    def test_receiver_evidence_buffers_without_control_path_io_and_flushes_once(self) -> None:
        rows = (
            {"schema_version": "v", "event": "receiver_observed_result",
             "identity": "a", "movement_tick": 10, "monotonic_ns": 1},
            {"schema_version": "v", "event": "registry_publish",
             "identity": "a", "movement_tick": 10, "monotonic_ns": 2},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            specs = {"b10-receiver-diagnostics.jsonl": replace(
                _B10_STREAM_SPECS["b10-receiver-diagnostics.jsonl"],
                max_rows=4,
            )}
            buffer = _B10EvidenceBuffer(root, stream_specs=specs)
            with patch.object(Path, "open", side_effect=AssertionError("control-path open")), \
                    patch("os.fsync", side_effect=AssertionError("control-path fsync")):
                buffer.collect_receiver(_DiagnosticInbox(rows), "trial-a")
                buffer.collect_receiver(_DiagnosticInbox(rows), "trial-a")
            path = root / "b10-receiver-diagnostics.jsonl"
            self.assertFalse(path.exists())
            self.assertEqual(len(buffer.rows(path.name)), 2)
            self.assertEqual(buffer.duplicate_count, 2)
            buffer.flush()
            buffer.flush()
            written = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
            self.assertEqual([row["event"] for row in written], [
                "receiver_observed_result", "registry_publish",
            ])
            self.assertTrue(all(row["trial"] == "trial-a" for row in written))

    def test_receiver_evidence_flushes_during_exception_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            buffer = _B10EvidenceBuffer(root)
            with self.assertRaisesRegex(ValueError, "primary"):
                with buffer:
                    buffer.collect_receiver(_DiagnosticInbox((
                        {"event": "receiver_cycle", "cycle": 1,
                         "movement_tick": 3, "duration_ns": 4},
                    )), "trial-a")
                    raise ValueError("primary")
            path = root / "b10-receiver-diagnostics.jsonl"
            written = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0]["event"], "receiver_cycle")

    def test_receiver_evidence_overflow_is_structured_and_fails_at_flush(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            specs = {"b10-receiver-diagnostics.jsonl": replace(
                _B10_STREAM_SPECS["b10-receiver-diagnostics.jsonl"],
                max_rows=2,
            )}
            buffer = _B10EvidenceBuffer(root, stream_specs=specs)
            buffer.collect_receiver(_DiagnosticInbox(tuple(
                {"event": "receiver_cycle", "cycle": cycle,
                 "movement_tick": cycle, "duration_ns": cycle}
                for cycle in range(3)
            )), "trial-a")
            path = root / "b10-receiver-diagnostics.jsonl"
            self.assertEqual(len(buffer.rows(path.name)), 2)
            self.assertEqual(buffer.overflow_count, 1)
            with self.assertRaises(_ReceiverDiagnosticOverflow):
                buffer.flush()
            written = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
            self.assertEqual(written[-1], {
                "schema_version": "mc2p.b10-evidence-buffer-overflow.v1",
                "event": "buffer_overflow",
                "stream": "b10-receiver-diagnostics.jsonl",
                "max_rows": 2,
                "max_bytes": specs[path.name].max_bytes,
                "overflow_count": 1,
            })

    def test_receiver_dedup_state_only_tracks_rows_accepted_by_the_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            specs = {"b10-receiver-diagnostics.jsonl": replace(
                _B10_STREAM_SPECS["b10-receiver-diagnostics.jsonl"],
                max_rows=2,
            )}
            buffer = _B10EvidenceBuffer(root, stream_specs=specs)
            rows = tuple(
                {"event": "receiver_cycle", "cycle": cycle,
                 "movement_tick": cycle, "duration_ns": cycle}
                for cycle in range(1_002)
            )

            buffer.collect_receiver(_DiagnosticInbox(rows), "trial-a")

            stream = "b10-receiver-diagnostics.jsonl"
            self.assertEqual(len(buffer.rows(stream)), 2)
            self.assertEqual(buffer.overflow_count, 1_000)
            self.assertEqual(len(buffer._receiver_seen), 2)

    def test_b10_all_runtime_jsonl_uses_bounded_buffer_without_control_io(self) -> None:
        expected = {
            "diagnostics.jsonl",
            "b10-gap-solver-trials.jsonl",
            "b10-coordinator-control-frames.jsonl",
            "b10-coordinator-trials.jsonl",
            "r4-moving-gap-negative-trials.jsonl",
            "b10-receiver-diagnostics.jsonl",
        }
        self.assertEqual(set(_B10_STREAM_SPECS), expected)
        import scripts.b10_gap_solver_runtime as b10_runtime
        self.assertNotIn("append_jsonl(", inspect.getsource(b10_runtime))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            buffer = _B10EvidenceBuffer(root)
            with patch.object(Path, "open", side_effect=AssertionError("control-path open")), \
                    patch("os.fsync", side_effect=AssertionError("control-path fsync")), \
                    patch("scripts.control_probe_core.append_jsonl",
                          side_effect=AssertionError("control-path append_jsonl")):
                buffer.append("diagnostics.jsonl", {"sequence": 1})
                buffer.append("diagnostics.jsonl", {"sequence": 2})
                buffer.append(
                    "b10-coordinator-control-frames.jsonl",
                    {"frame": 1},
                )
            self.assertFalse(any(root.iterdir()))
            buffer.flush()
            diagnostics = [
                json.loads(line)
                for line in (root / "diagnostics.jsonl").read_text("utf-8").splitlines()
            ]
            self.assertEqual([row["sequence"] for row in diagnostics], [1, 2])

    def test_b10_buffer_preserves_primary_error_when_batch_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            buffer = _B10EvidenceBuffer(Path(temporary))
            buffer.append("diagnostics.jsonl", {"sequence": 1})
            with patch(
                "scripts.b10_gap_solver_runtime.write_jsonl_atomic",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(ValueError, "primary") as caught:
                    with buffer:
                        raise ValueError("primary")
            self.assertTrue(any(
                "disk unavailable" in note
                for note in getattr(caught.exception, "__notes__", ())
            ))
            self.assertEqual(buffer.flush_failure_count, 1)

    def test_b10_buffer_fails_without_primary_error_on_write_or_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            buffer = _B10EvidenceBuffer(Path(temporary))
            buffer.append("diagnostics.jsonl", {"sequence": 1})
            with patch(
                "scripts.b10_gap_solver_runtime.write_jsonl_atomic",
                side_effect=OSError("disk unavailable"),
            ), self.assertRaisesRegex(OSError, "disk unavailable"):
                buffer.flush()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            specs = dict(_B10_STREAM_SPECS)
            specs["diagnostics.jsonl"] = replace(
                specs["diagnostics.jsonl"], max_rows=1,
            )
            buffer = _B10EvidenceBuffer(root, stream_specs=specs)
            buffer.append("diagnostics.jsonl", {"sequence": 1})
            buffer.append("diagnostics.jsonl", {"sequence": 2})
            with self.assertRaises(_ReceiverDiagnosticOverflow):
                buffer.flush()
            rows = [
                json.loads(line)
                for line in (root / "diagnostics.jsonl").read_text("utf-8").splitlines()
            ]
            self.assertEqual(rows[-1]["event"], "buffer_overflow")
            self.assertEqual(rows[-1]["stream"], "diagnostics.jsonl")

    def test_b10_runtime_trace_writes_on_async_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = _runtime_trace(
                root,
                backend=None,
                capture_close_diagnostics=False,
                async_delivery=True,
            )
            self.assertIsInstance(trace, BoundedAsyncTraceWriter)
            with patch.object(
                Path, "open", side_effect=AssertionError("control-path open"),
            ), patch("os.fsync", side_effect=AssertionError("control-path fsync")):
                trace.write("step", {"sequence": 1})
            trace.close()
            self.assertEqual(trace.stats.accepted_records, 1)
            self.assertEqual(trace.stats.dropped_records, 0)
            records = [
                json.loads(line)
                for line in (root / "trace.jsonl").read_text("utf-8").splitlines()
            ]
            self.assertEqual(records[0]["record_type"], "step")

    def test_wait_gate_rejects_a_barrier_inside_driver_tick(self) -> None:
        clock, backend, _, session, driver = self._fixture()
        wait_gate = _ReceiverWaitGate()
        native_tick = RuntimeNavigationDriver.tick

        def mutated_tick(target, *args, **kwargs):
            target.session._motion_result_inbox.wait_for_receiver(timeout=.01)
            return native_tick(target, *args, **kwargs)

        with wait_gate.patch(), \
                patch.object(RuntimeNavigationDriver, "tick", mutated_tick):
            with self.assertRaisesRegex(
                AssertionError, "outside explicit delivery",
            ):
                driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
        self.assertEqual(wait_gate.calls, [False])

    def test_receiver_diagnostics_report_cycles_publish_and_first_consume(self) -> None:
        clock, backend, _, session, driver = self._fixture()
        wait_gate = _ReceiverWaitGate()
        session._motion_result_inbox.publish_diagnostic_movement_tick(
            backend.movement_tick,
        )
        with patch("mc2p.motion_nav.navigation_session.MotionSolverWorker",
                   _TickScheduledWorker), wait_gate.patch():
            for _ in range(20):
                step = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                self.assertIsNone(step.report.failure)
                if _TickScheduledWorker.instances and _TickScheduledWorker.instances[0].jobs:
                    break
            worker = _TickScheduledWorker.instances[0]
            job = worker.jobs[0]
            wait_gate.deliver(worker, session._motion_result_inbox, job)
            for _ in range(4):
                step = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                self.assertIsNone(step.report.failure)

        rows = session._motion_result_inbox.drain_receiver_diagnostics()
        cycle_rows = [row for row in rows if row["event"] == "receiver_cycle"]
        submit_rows = [row for row in rows if row["event"] == "worker_submit"]
        observed_rows = [row for row in rows if row["event"] == "receiver_observed_result"]
        publish_rows = [row for row in rows if row["event"] == "registry_publish"]
        consume_rows = [row for row in rows if row["event"] == "first_control_consume"]
        self.assertTrue(cycle_rows)
        self.assertTrue(all(row["duration_ns"] >= 0 for row in cycle_rows))
        self.assertEqual(len(submit_rows), 1)
        self.assertEqual(len(observed_rows), 1)
        self.assertEqual(len(publish_rows), 1)
        self.assertEqual(len(consume_rows), 1)
        self.assertEqual(publish_rows[0]["identity"], consume_rows[0]["identity"])
        self.assertEqual(submit_rows[0]["identity"], publish_rows[0]["identity"])
        self.assertIsInstance(submit_rows[0]["movement_tick"], int)
        self.assertIsInstance(submit_rows[0]["monotonic_ns"], int)
        self.assertIsInstance(observed_rows[0]["worker_elapsed_ns"], int)
        self.assertIsInstance(observed_rows[0]["worker_completed_monotonic_ns"], int)
        self.assertGreaterEqual(
            observed_rows[0]["monotonic_ns"],
            observed_rows[0]["worker_completed_monotonic_ns"],
        )
        self.assertIsInstance(publish_rows[0]["movement_tick"], int)
        self.assertIsInstance(consume_rows[0]["movement_tick"], int)
        self.assertGreaterEqual(
            consume_rows[0]["movement_tick"], publish_rows[0]["movement_tick"],
        )
        self.assertEqual(session._motion_result_inbox.drain_receiver_diagnostics(), ())

    def test_old_generation_result_is_not_installed_after_goal_revision(self) -> None:
        clock, backend, runtime, session, driver = self._fixture()
        barriers = 0
        wait_gate = _ReceiverWaitGate()
        with patch("mc2p.motion_nav.navigation_session.MotionSolverWorker",
                   _TickScheduledWorker), wait_gate.patch():
            for _ in range(20):
                step = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                self.assertIsNone(step.report.failure)
                if _TickScheduledWorker.instances and _TickScheduledWorker.instances[0].jobs:
                    break
            worker = _TickScheduledWorker.instances[0]
            old_job = worker.jobs[0]
            old_identity = old_job.work_identity
            old_generation = old_identity.scope.generation
            self.assertTrue(driver.replace_goal(
                "d108-receiver-goal", 2, _goal((.5, 64.0, 2.6)), clock[0],
            ))
            # A goal revision alone deliberately preserves the computation
            # scope.  Invalidating a route dependency is what advances the
            # generation and makes the already submitted result stale.
            backend.landing_present = False
            for _ in range(4):
                step = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                self.assertIsNone(step.report.failure)
                if session.current_computation_scope.generation > old_generation:
                    break
            self.assertGreater(session.current_computation_scope.generation, old_generation)
            wait_gate.deliver(worker, session._motion_result_inbox, old_job)
            barriers += 1
            for _ in range(4):
                step = driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                self.assertIsNone(step.report.failure)
                if session.report.terminal:
                    break

        self.assertEqual(barriers, 1)
        self.assertEqual(wait_gate.calls, [True])
        self.assertIs(runtime.state, RuntimeStateV1.READY)
        self.assertTrue(session.report.terminal)
        self.assertEqual(session.report.reason, "no_route_within_complete_scope")
        self.assertIs(driver.state, RuntimeNavigationDriverState.FAILED)
        self.assertFalse(session.has_owned_body_control)
        self.assertFalse(any(action.movement.jump for action in backend.actions))
        old_admissions = tuple(
            record
            for diagnostics in session.async_work_diagnostics
            for record in diagnostics.admissions
            if record.identity == old_identity
        )
        self.assertFalse(any(
            record.disposition is AsyncAdmissionDisposition.APPLIED
            for record in old_admissions
        ))
        self.assertEqual(runtime.observation.health_points.value, 20.0)
        session.close()
        self.assertEqual(session.active_motion_mailboxes, ())
        self.assertTrue(worker.closed)


if __name__ == "__main__":
    unittest.main()
