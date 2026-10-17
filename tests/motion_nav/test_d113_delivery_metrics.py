from __future__ import annotations

from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from mc2p.motion_nav.async_work import AsyncComputationScope, AsyncWorkIdentity, AsyncWorkKind
from mc2p.motion_nav.motion_worker import MotionResultInbox
from scripts import b10_gap_solver_runtime as probe


def identity(revision=1):
    return AsyncWorkIdentity(AsyncComputationScope("world", "task", 1), "owner",
                             AsyncWorkKind.MOTION_SOLVE, "edge", revision)


def trace(episode, control, observation, result_observation, ms):
    return {"record_type": "step", "episode_id": episode, "payload": {
        "decision_observation_received_ns": 1_000_000,
        "dispatched_monotonic_ns": 1_000_000 + int(ms * 1_000_000),
        "decision": {"action": {"request_sequence_id": control,
                                  "observation_sequence_id": observation}},
        "backend_result": {"observation": {"sequence_id": result_observation}},
    }}


class D113DiagnosticTests(unittest.TestCase):
    def test_peek_visibility_precedes_take_and_deduplicates(self):
        inbox = MotionResultInbox()
        self.addCleanup(inbox.close)
        work = identity()
        result = object()
        inbox._results[work] = result
        inbox.publish_diagnostic_movement_tick(10)
        self.assertIs(inbox.peek(work), result)
        self.assertIs(inbox.peek(work), result)
        inbox.publish_diagnostic_movement_tick(11)
        self.assertEqual(inbox.take(work), (result,))
        self.assertEqual(inbox.take(work), ())
        rows = inbox.drain_receiver_diagnostics()
        self.assertEqual([(r["event"], r["movement_tick"]) for r in rows], [
            ("first_control_visible", 10), ("first_control_consume", 11),
            ("result_take", 11)])

    def test_prepared_ready_is_visible_and_consumed_without_removal(self):
        inbox = MotionResultInbox()
        self.addCleanup(inbox.close)
        work = identity()
        ready = object()
        inbox._registry["registered"] = SimpleNamespace(
            identity=work, candidate=True, ready_query=ready,
            bundle=SimpleNamespace(request=SimpleNamespace(deadline_tick=20)))
        self.addCleanup(inbox._registry.clear)
        inbox.publish_diagnostic_movement_tick(10)
        self.assertIs(inbox.query_prepared(work, movement_tick=12), ready)
        self.assertIs(inbox.query_prepared(work, movement_tick=13), ready)
        rows = inbox.drain_receiver_diagnostics()
        self.assertEqual([(r["event"], r["movement_tick"]) for r in rows], [
            ("first_control_visible", 12), ("first_control_consume", 12)])
        self.assertFalse(rows[-1]["removes_result"])
        inbox._registry.clear()

    def test_submission_records_anchor_separately_from_clock(self):
        inbox = MotionResultInbox()
        self.addCleanup(inbox.close)
        work = identity()
        inbox.register(work)
        inbox.publish_diagnostic_movement_tick(40)
        inbox.mark_submitted(work, job_anchor_movement_tick=38)
        row, = inbox.drain_receiver_diagnostics()
        self.assertEqual(row["worker_submit_tick"], 40)
        self.assertEqual(row["job_anchor_movement_tick"], 38)


class D113PreparedDiagnosticClockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.motion_nav.test_d102_receiver import ReceiverTests
        ReceiverTests.setUpClass()
        cls.fixture = ReceiverTests

    def test_real_prepared_submit_uses_published_tick_over_stale_body_facts(self):
        from mc2p.motion_nav.prepared_motion import PreparedQueryStatus, PreparedSubmitStatus
        from scripts.analyze_d113_delivery_baseline import summarize_identity_delivery
        from tests.motion_nav.test_d102_receiver import QueuedWorker
        fixture = self.fixture
        self.assertEqual(fixture.current.movement_tick, 10)
        worker = QueuedWorker()
        inbox = MotionResultInbox(worker=worker, current_facts=fixture.current)
        self.addCleanup(inbox.close)
        inbox.publish_diagnostic_movement_tick(11)
        self.assertIs(inbox.register_preparation(fixture.request, fixture.lease).status,
                      PreparedSubmitStatus.ACCEPTED)
        self.assertTrue(worker.submitted.wait(10))
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        worker.put(fixture.message)
        self.assertTrue(inbox.wait_for_receiver(timeout=10))
        ready = inbox.query_prepared(fixture.request.async_identity, movement_tick=11)
        self.assertIs(ready.status, PreparedQueryStatus.READY_MATCH)
        rows = inbox.drain_receiver_diagnostics()
        events = {row["event"]: row for row in rows if row.get("identity") is not None}
        self.assertEqual(events["worker_submit"]["worker_submit_tick"], 11)
        for event in ("worker_submit", "registry_publish", "first_control_visible", "first_control_consume"):
            self.assertEqual(events[event]["movement_tick"], 11, event)
        self.assertTrue(all(row["movement_tick"] == 11 for row in rows
                            if row["event"] == "receiver_cycle"))
        self.assertIsNone(events["worker_submit"]["job_anchor_movement_tick"])
        self.assertEqual(events["worker_submit"]["prepared_entry_state_movement_tick"], 10)
        self.assertEqual(inbox._current_facts, fixture.current)
        delivery, = summarize_identity_delivery(rows)["identities"]
        self.assertEqual(delivery["actual_delivery_ticks"], 0)
        # The existing prepared wire does not produce a receiver-observed row.
        # This partial diagnostic chain cannot sign complete prepared delivery.
        self.assertNotIn("receiver_observed_result", events)
        self.assertFalse(delivery["complete"])

    def test_diagnostic_clock_falls_back_only_without_a_published_tick(self):
        inbox = MotionResultInbox(current_facts=self.fixture.current)
        self.addCleanup(inbox.close)
        with inbox._lock:
            self.assertEqual(inbox._diagnostic_tick_locked(), 10)
        inbox.publish_diagnostic_movement_tick(11)
        with inbox._lock:
            self.assertEqual(inbox._diagnostic_tick_locked(), 11)


class D113ReadGateTests(unittest.TestCase):
    def fixture(self, delay=2):
        worker = SimpleNamespace(submit=lambda _: True, poll_available=lambda: (), is_alive=lambda: True)
        delayed = probe._MovementTickDelayedMotionWorker(worker)
        delayed.begin_trial(probe._B10CoordinatorScenario("delay", "worker_delay", 0, 0,
                            worker_delay_ticks=delay), movement_tick=40)
        inbox = MotionResultInbox()
        self.addCleanup(inbox.close)
        delayed.install_read_gate(inbox)
        work = identity()
        delayed.submit(SimpleNamespace(work_identity=work))
        return delayed, inbox, work

    def test_exact_visibility_blocks_peek_take_and_prepared(self):
        delayed, inbox, work = self.fixture(5)
        result = object()
        inbox._results[work] = result
        delayed.publish_movement_tick(44)
        self.assertIsNone(inbox.peek(work))
        self.assertEqual(inbox.take(work), ())
        self.assertIs(inbox.query_prepared(work, movement_tick=44), inbox._NOT_READY)
        delayed.publish_movement_tick(45)
        self.assertIs(inbox.peek(work), result)
        delayed.publish_movement_tick(46)
        self.assertEqual(inbox.take(work), (result,))
        row, = delayed.end_trial()["submissions"]
        self.assertEqual(row["target_visible_tick"], 45)
        self.assertEqual(row["first_control_visible_tick"], 45)
        self.assertEqual(row["actual_delivery_ticks"], 5)
        self.assertEqual(row["injection_status"], "matched")

    def test_delay2_preserves_peek_take_separation(self):
        delayed, inbox, work = self.fixture()
        inbox.publish_diagnostic_movement_tick(42)
        inbox._results[work] = object()
        delayed.publish_movement_tick(42)
        self.assertIsNotNone(inbox.peek(work))
        delayed.publish_movement_tick(43)
        inbox.publish_diagnostic_movement_tick(43)
        inbox.take(work)
        rows = inbox.drain_receiver_diagnostics()
        self.assertEqual([(r["event"], r["movement_tick"]) for r in rows], [
            ("first_control_visible", 42), ("first_control_consume", 43), ("result_take", 43)])
        self.assertEqual(delayed.end_trial()["submissions"][0]["actual_delivery_ticks"], 2)

    def test_worker_not_ready_is_not_matched(self):
        delayed, inbox, work = self.fixture()
        delayed.publish_movement_tick(42)
        self.assertIsNone(inbox.peek(work))
        inbox._results[work] = object()
        delayed.publish_movement_tick(43)
        self.assertIsNotNone(inbox.peek(work))
        row, = delayed.end_trial()["submissions"]
        self.assertEqual(row["actual_delivery_ticks"], 3)
        self.assertEqual(row["injection_status"], "worker_not_ready")

    def test_skipped_tick_and_incomplete_are_not_matched(self):
        delayed, inbox, work = self.fixture()
        inbox._results[work] = object()
        delayed.publish_movement_tick(43)
        inbox.peek(work)
        self.assertEqual(delayed.end_trial()["submissions"][0]["injection_status"], "control_tick_skipped")
        delayed, inbox, _ = self.fixture()
        self.assertEqual(delayed.end_trial()["submissions"][0]["injection_status"], "incomplete")

    def test_old_generation_is_hidden_and_gate_is_removed(self):
        delayed, inbox, work = self.fixture()
        native = delayed._gate_originals["peek"]
        delayed.end_trial()
        self.assertEqual(inbox.peek, native)
        delayed.begin_trial(probe._B10CoordinatorScenario("new", "worker_delay", 0, 0,
                            worker_delay_ticks=2), movement_tick=50)
        delayed.install_read_gate(inbox)
        inbox._results[work] = object()
        self.assertIsNone(inbox.peek(work))
        self.assertEqual(delayed.end_trial()["stale_result_count"], 1)

    def test_unknown_identity_is_not_exposed_or_sampled(self):
        delayed, inbox, work = self.fixture()
        other = identity(2)
        delayed.publish_movement_tick(42)
        self.assertIsNone(inbox.peek(other))
        self.assertIsNone(delayed.end_trial()["submissions"][0]["first_control_visible_tick"])

    def test_invalid_injection_cannot_count_as_success(self):
        for status in ("worker_not_ready", "control_tick_skipped", "old_generation", "incomplete"):
            self.assertFalse(probe._worker_injection_valid({"scenario_kind": "worker_delay",
                "worker_injection": {"submissions": [{"injection_status": status}], "stale_result_count": 0}}))

    def test_prepared_ready_query_uses_same_visibility_gate(self):
        from mc2p.motion_nav.prepared_motion import PreparedQueryStatus
        delayed, inbox, work = self.fixture()
        ready = SimpleNamespace(status=PreparedQueryStatus.READY_MATCH)
        inbox._registry["registered"] = SimpleNamespace(identity=work, candidate=True,
            ready_query=ready, bundle=SimpleNamespace(request=SimpleNamespace(deadline_tick=50)))
        self.addCleanup(inbox._registry.clear)
        delayed.publish_movement_tick(41)
        self.assertIs(inbox.query_prepared(work, movement_tick=41), inbox._NOT_READY)
        delayed.publish_movement_tick(42)
        self.assertIs(inbox.query_prepared(work, movement_tick=42), ready)
        self.assertEqual(delayed.end_trial()["submissions"][0]["injection_status"], "matched")

    def test_gate_preserves_public_argument_validation(self):
        from mc2p.contracts.common import ContractViolation
        delayed, inbox, work = self.fixture()
        with self.assertRaises(ContractViolation):
            inbox.peek("not-an-identity")
        with self.assertRaises(ContractViolation):
            inbox.take("not-an-identity")
        with self.assertRaises(ContractViolation):
            inbox.query_prepared("not-an-identity", movement_tick=41)
        with self.assertRaises(ContractViolation):
            inbox.query_prepared(work, movement_tick=-1)
        delayed.end_trial()


class D113TraceGroupingTests(unittest.TestCase):
    def analyze(self, rows, frames, probes=()):
        from scripts.analyze_d113_delivery_baseline import group_control_prepare
        return group_control_prepare(rows, frames, probes)

    def test_mixed_trace_keeps_waiting_and_full_interval(self):
        rows = [trace("a", 1, 10, 11, 40), trace("a", 2, 11, 12, 4),
                trace("a", 3, 12, 13, 12), trace("a", 4, 13, 14, 2)]
        result = self.analyze(rows[::-1], [
            {"episode_id": "a", "observation_sequence_id": 13},
            {"episode_id": "a", "observation_sequence_id": 12, "control_sequence_id": 2,
             "proposal_elapsed_ns": 1, "runtime_outside_backend_ns": 1},
        ], [{"episode_id": "a", "start_observation_sequence_id": 10,
             "solve_started_ns": 2_000_000, "solve_ended_ns": 30_000_000}])
        self.assertEqual(result["production_control_prepare"]["samples"], 2)
        self.assertEqual(result["production_control_prepare"]["maximum"], 12)
        self.assertEqual(result["synchronous_probe"]["samples"], 1)
        self.assertEqual(result["other_fixture"]["samples"], 1)
        self.assertTrue(result["performance_signable"])

    def test_episodes_do_not_mix(self):
        result = self.analyze([trace("a", 1, 1, 2, 4), trace("b", 1, 1, 2, 40)],
            [{"episode_id": "a", "result_observation_sequence_id": 2, "control_sequence_id": 1}])
        self.assertEqual(result["production_control_prepare"]["maximum"], 4)

    def test_v3_control_frame_requires_complete_unique_identity(self):
        row = trace("a", 1, 1, 2, 4)
        complete = {
            "schema_version": "mc2p.b10-coordinator-control-frame.v3",
            "episode_id": "a",
            "control_sequence_id": 1,
            "decision_observation_sequence_id": 1,
            "result_observation_sequence_id": 2,
            "observation_sequence_id": 2,
            "proposal_elapsed_ns": 1,
            "runtime_outside_backend_ns": 1,
        }
        self.assertTrue(
            self.analyze([row], [complete])["performance_signable"]
        )
        for field in (
            "episode_id",
            "control_sequence_id",
            "decision_observation_sequence_id",
            "result_observation_sequence_id",
            "observation_sequence_id",
            "proposal_elapsed_ns",
            "runtime_outside_backend_ns",
        ):
            with self.subTest(field=field):
                missing = dict(complete)
                missing.pop(field)
                result = self.analyze([row], [missing])
                self.assertFalse(result["performance_signable"])
                self.assertIn(
                    "missing_v3_field",
                    {issue["issue"] for issue in result["mapping_issues"]},
                )

    def test_v2_identifier_rule_is_preserved(self):
        row = trace("a", 1, 1, 2, 4)
        complete = {
            "schema_version": "mc2p.b10-coordinator-control-frame.v2",
            "episode_id": "a",
            "control_sequence_id": 1,
            "decision_observation_sequence_id": 1,
            "result_observation_sequence_id": 2,
        }
        self.assertTrue(
            self.analyze([row], [complete])["performance_signable"]
        )
        incomplete = dict(complete)
        incomplete.pop("control_sequence_id")
        result = self.analyze([row], [incomplete])
        self.assertFalse(result["performance_signable"])
        self.assertIn(
            "missing_v2_identifier",
            {issue["issue"] for issue in result["mapping_issues"]},
        )

    def test_ambiguous_missing_duplicate_and_invalid_mapping_block_signing(self):
        for rows, frames in [
            ([trace("a", 1, 1, 2, 4), trace("b", 1, 1, 2, 5)], [{"observation_sequence_id": 2}]),
            ([trace("a", 1, 1, 2, 4)], [{"observation_sequence_id": 99}]),
            ([trace("a", 1, 1, 2, 4)], [{"observation_sequence_id": 2}] * 2),
            ([trace("a", 1, 1, 2, -1)], [{"observation_sequence_id": 2}]),
            ([trace("a", 1, 1, 2, 4)], [{"observation_sequence_id": 2, "control_sequence_id": 9}]),
        ]:
            with self.subTest(frames=frames):
                self.assertFalse(self.analyze(rows, frames)["performance_signable"])

    def test_all_identities_keep_missing_visibility_and_actual_delay(self):
        from scripts.analyze_d113_delivery_baseline import summarize_identity_delivery
        rows = [{"trial": "t", "identity": "a", "event": event, "movement_tick": tick}
                for event, tick in [("worker_submit", 40), ("receiver_observed_result", 42),
                                    ("registry_publish", 42), ("first_control_visible", 43),
                                    ("first_control_consume", 44), ("result_take", 44)]]
        rows.append({"trial": "t", "identity": "b", "event": "worker_submit", "movement_tick": 45})
        result = summarize_identity_delivery(rows)
        self.assertEqual(result["identities"][0]["actual_delivery_ticks"], 3)
        self.assertIsNone(result["identities"][1]["actual_delivery_ticks"])
        self.assertEqual(result["incomplete_count"], 1)


class D113PartialSummaryTests(unittest.TestCase):
    def test_partial_summary_keeps_probe_and_gc_without_passing_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            buffer = probe._B10EvidenceBuffer(Path(directory))
            buffer.append("b10-gap-solver-trials.jsonl", {
                "solve_time_ns": 40_000_000, "solve_started_ns": 1,
                "solve_ended_ns": 40_000_001, "solve_thread_cpu_ns": 30_000_000,
                "solve_gc_overlap": False, "solve_measurement_errors": []})
            result = probe._partial_b10_summary(buffer, probe._GCPauseTimeline())
            self.assertTrue(result["partial"])
            self.assertEqual(result["synchronous_probe_timing"]["completed_count"], 1)
            self.assertEqual(result["solve_time_ns"]["maximum"], 40_000_000)
            self.assertIn("gc_pause_timeline", result)
            self.assertFalse(result["matrix_complete"])

    def test_real_outer_finally_writes_partial_and_preserves_primary(self):
        primary = TimeoutError("original matrix failure")
        runtime = MagicMock()
        runtime.state.value = "ready"
        planner, motion = MagicMock(), MagicMock()
        def stop(*args, **kwargs):
            kwargs["evidence_buffer"].append("b10-gap-solver-trials.jsonl", {
                "solve_time_ns": 4, "solve_started_ns": 1, "solve_ended_ns": 5,
                "solve_thread_cpu_ns": 2, "solve_gc_overlap": False, "solve_measurement_errors": []})
            raise primary
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(probe, "PlannerWorker", return_value=planner), \
                patch.object(probe, "MotionSolverWorker", return_value=motion), \
                patch.object(probe, "_run_b10_gap_solver_runtime", side_effect=stop):
            with self.assertRaises(TimeoutError) as caught:
                probe.run_b10_gap_solver_runtime(runtime, object(), "e", Path(directory), 1,
                    lambda *_: None, lambda *_: None)
            self.assertIs(caught.exception, primary)
            partial = json.loads((Path(directory) / "b10-gap-solver-partial-summary.json").read_text())
            self.assertEqual(partial["synchronous_probe_timing"]["completed_count"], 1)
            self.assertEqual(partial["primary_error_type"], "TimeoutError")
            self.assertFalse(partial["matrix_complete"])


if __name__ == "__main__":
    unittest.main()
