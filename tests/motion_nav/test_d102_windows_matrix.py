"""The G2 meter must reject missing work, forged readiness and mixed timing.

All ordinary runs are temporary. These tests do not create a formal sample.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import time


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests/sim/manifests/prepared-motion-proof-d102-v1.json"


class ExistingMeterGapTests(unittest.TestCase):
    def legacy_record(self):
        return dict(group="fast", published=True, requires_activation=True,
                    thread_events=[dict(operation="receive_validate_publish", thread_id=1,
                                        thread_name="mc2p-motion-receiver")],
                    publication_events=[dict(operation="publish", proof_token="proof", timestamp_ns=10)],
                    consumption_events=[dict(operation="consume", proof_token="proof", timestamp_ns=11)],
                    reference_events=[dict(operation="retain", proof_token="proof", timestamp_ns=10)],
                    activation_events=[dict(proof_token="proof")])

    def test_existing_g2_validator_rejects_missing_control_phases(self):
        from scripts.verify_prepared_motion_d102 import _event_errors
        self.assertTrue(_event_errors(self.legacy_record(), "controlled-gap", stage="G2"),
                        "G2 currently accepts a record with no complete control meter")

    def test_existing_g2_validator_rejects_queue_time_as_ready(self):
        from scripts.verify_prepared_motion_d102 import _event_errors
        raw = self.legacy_record()
        raw.update(ready_ns=2, request_preparation_started_ns=2,
                   receiver_thread_id=1, control_thread_id=2,
                   native_events=[dict(operation="publish", proof_token="proof", timestamp_ns=10,
                                       thread_id=1, producer="MotionResultInbox"),
                                  dict(operation="first_consume", proof_token="proof", timestamp_ns=11,
                                       thread_id=2, producer="ActionRouteExecutor")])
        errors = _event_errors(raw, "controlled-gap", stage="G2")
        self.assertTrue(any("ready" in value for value in errors), errors)

    def test_existing_g2_validator_rejects_over_30ms(self):
        from scripts.verify_prepared_motion_d102 import _event_errors
        raw = self.legacy_record()
        raw["control_samples"] = [dict(duration_ns=30_000_001, kind="start", phases_ns={})]
        errors = _event_errors(raw, "controlled-gap", stage="G2")
        self.assertTrue(any("maximum" in value for value in errors), errors)


class MatrixTests(unittest.TestCase):
    def meter(self):
        name = "tests.sim.d102_prepared_motion"
        self.assertIsNotNone(importlib.util.find_spec(name),
                             "G2 has no real-service matrix meter")
        return importlib.import_module(name)

    def native_case(self, **axes):
        meter = self.meter()
        coordinates = dict(platform_width=2, start_offset=0, entry_offset=0., direction="south", backlog=0)
        coordinates.update(axes)
        worker = meter._InlinePort()
        inbox = meter.MotionResultInbox(worker=worker)
        case = meter._Case(coordinates, inbox, worker)
        self.addCleanup(inbox.close, timeout_seconds=10)
        self.addCleanup(case.runtime.close)
        self.addCleanup(case.restore)
        return case

    def sample(self, output, *, measurement_mode="legacy-combined", **axes):
        coordinates = dict(platform_width=2, start_offset=0, entry_offset=0.,
                           direction="south", backlog=0)
        coordinates.update(axes)
        raw = self.meter().record_probe(coordinates, output=Path(output),
                                       manifest=MANIFEST, transport="inline",
                                       measurement_mode=measurement_mode)
        self.assertEqual(raw["status"], "PASS", raw.get("failure_reason", raw.get("typed_result")))
        return raw

    def test_performance_mode_rejects_an_active_profile_hook(self):
        meter = self.meter()._ControlMeter("performance")
        previous = sys.getprofile()
        sys.setprofile(lambda frame, event, arg: None)
        try:
            with self.assertRaisesRegex(ValueError, "profile"):
                with meter.tick("start", 10):
                    pass
        finally:
            sys.setprofile(previous)

    def test_call_count_mode_reports_sources_without_claiming_timing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self.sample(root, measurement_mode="call-count")
            report = self.meter().recompute_run(root / "index.json")
        self.assertEqual(raw["measurement_mode"], "call-count")
        self.assertTrue(raw["profile_enabled"])
        self.assertFalse(raw["timing_valid"])
        self.assertTrue(raw["hot_path_calls"])
        self.assertTrue(any(sample["forbidden_events"] for sample in raw["control_samples"]))
        self.assertTrue(all("duration_ns" not in sample and "phases_ns" not in sample
                            for sample in raw["control_samples"]))
        self.assertTrue(any("performance validation requires" in error for error in
                            self.meter().validate_probe(raw, check_performance=True)))
        self.assertFalse(report["timing_valid"])
        self.assertTrue(report["profile_enabled"])
        self.assertEqual(report["complete"]["count"], 0)

    def test_performance_mode_disables_profile_and_records_explicit_spans(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = self.sample(root, measurement_mode="performance")
            report = self.meter().recompute_run(root / "index.json")
        self.assertEqual(raw["measurement_mode"], "performance")
        self.assertFalse(raw["profile_enabled"])
        self.assertTrue(raw["timing_valid"])
        self.assertIsNone(raw["hot_path_calls"])
        self.assertTrue(all("forbidden_events" not in sample for sample in raw["control_samples"]))
        self.assertTrue(raw["control_samples"])
        self.assertTrue(all(sample["duration_ns"] >= sum(sample["phases_ns"].values())
                            for sample in raw["control_samples"]))
        self.assertTrue(report["timing_valid"])
        self.assertFalse(report["profile_enabled"])
        self.assertGreater(report["complete"]["count"], 0)

    def test_measurement_modes_preserve_the_same_seeded_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            counted = self.sample(root / "count", measurement_mode="call-count",
                                  platform_width=3, start_offset=1, entry_offset=.05,
                                  direction="west", backlog=1)
            timed = self.sample(root / "performance", measurement_mode="performance",
                                platform_width=3, start_offset=1, entry_offset=.05,
                                direction="west", backlog=1)
        fields = ("status", "typed_result", "continuous_complete", "second_jump_authorizations",
                  "actual_first_start_offset", "actual_application_ticks", "native_final_state",
                  "unauthorized_body_steps", "safety_violations", "released_by_original_owner")
        self.assertEqual({name: counted.get(name) for name in fields},
                         {name: timed.get(name) for name in fields})
        counted_inputs = [row["requested_movement"] for row in counted["native_motion_trace"]]
        timed_inputs = [row["requested_movement"] for row in timed["native_motion_trace"]]
        self.assertEqual(counted_inputs, timed_inputs)

    def test_fast_matrix_is_144_independent_exact_entries(self):
        # Reusing a single proof for the +/- offsets must fail this test.
        cases = self.meter().matrix_cases("fast", MANIFEST)
        self.assertEqual(len(cases), 144)
        self.assertEqual(len({tuple(row.items()) for row in cases}), 144)
        with tempfile.TemporaryDirectory() as temporary:
            raws = [self.sample(Path(temporary) / str(i), entry_offset=value)
                    for i, value in enumerate((-.05, 0., .05))]
        entries = [raw["input"]["entry"]["physics_state"] for raw in raws]
        self.assertEqual(len({tuple(row["position"]) for row in entries}), 3)
        self.assertEqual(len({raw["input"]["identity"]["job_token"] for raw in raws}), 3)
        for raw, entry in zip(raws, entries):
            self.assertEqual(raw["proof"]["branches"][0]["entry"]["physics_state"], entry)
            self.assertTrue(raw["parent_input_reference_is_independent"])

    def test_fast_025_preserves_11_12_window_and_late_branch_to_first_boundary(self):
        from mc2p.motion_nav.online_motion import InputApplicationStatus
        case = self.native_case(start_offset=1, entry_offset=-.05)
        owner, first_controller = case.f.supervisor.route, case.f.executor._controller
        boundary_facts = []
        native_decide = case.f.executor.decide
        def observe_boundary(*args, **kwargs):
            decision = native_decide(*args, **kwargs)
            if decision.reason_code == "prepared_action_boundary":
                boundary_facts.append((case.backend.state.movement_tick_id,
                                       first_controller._start_variant.start_tick,
                                       case.f.supervisor.route))
            return decision
        with patch.object(case.f.executor, "decide", side_effect=observe_boundary):
            case.run()
        record = case.f.ledger.record(0)
        self.assertEqual((record.requested_first_tick, record.latest_allowed_first_tick), (11, 12))
        self.assertEqual(record.applied_ticks, (12,))
        self.assertIs(record.status, InputApplicationStatus.APPLIED)
        self.assertTrue(boundary_facts, "the original owner never reached its first segment boundary")
        self.assertEqual(boundary_facts[0][:2], (case.f.proofs[0].delayed_start_variants[0].exit_state.movement_tick_id, 12))
        self.assertIs(boundary_facts[0][2], owner)
        self.assertTrue(case.completed)
        self.assertTrue(case.release())

    def test_midstream_late_one_is_input_loss_and_cannot_reselect_start_branch(self):
        from mc2p.motion_nav.online_motion import InputApplicationStatus
        from mc2p.motion_nav.action_route_executor import ActionRouteState
        case = self.native_case()
        controller = case.f.executor._controller
        case.tick(kind="start")
        case.tick(offset=1)
        second = case.f.ledger.record(1)
        self.assertEqual((second.requested_first_tick, second.latest_allowed_first_tick), (12, 12))
        self.assertEqual(second.applied_ticks, (13,))
        self.assertIs(second.status, InputApplicationStatus.APPLIED_OUTSIDE_WINDOW)
        case.tick()
        self.assertEqual(controller._start_variant.start_tick, 11)
        self.assertIn(case.last_route_decision.state, {ActionRouteState.CANCELLING, ActionRouteState.INPUT_LOST})
        self.assertEqual(case.f.executor.action_index, 0)

    def test_prepared_window_does_not_attach_to_another_arbitration_winner(self):
        from mc2p.contracts.action import ActionPriorityV0
        from mc2p.contracts.action_v1 import MovementV1
        from mc2p.contracts.motion_activation import ActivationStatus
        from mc2p.motion_nav.prepared_motion import ProofUse
        case = self.native_case()
        native_tick, handoffs = case.tick, []
        def another_wins(**kwargs):
            if kwargs.get("kind") == "handoff":
                case.runtime.submit_intent(case.intent(
                    "safety-winner", movement=MovementV1(), priority=ActionPriorityV0.SAFETY,
                ))
            result = native_tick(**kwargs)
            if kwargs.get("kind") == "handoff":
                handoffs.append(result)
            return result
        with patch.object(case, "tick", side_effect=another_wins):
            case.run()
        result, = handoffs
        self.assertIs(result.stream_activation.status, ActivationStatus.NOT_SELECTED)
        self.assertIsNone(result.decision.movement_tick_window)
        record = case.f.ledger.record(case.backend.actions[-1].request_sequence_id)
        self.assertEqual((record.requested_first_tick, record.latest_allowed_first_tick),
                         (case.handoff_tick + 1, case.handoff_tick + 1))
        self.assertTrue(case.inbox.usage_active(case.handle, ProofUse.CANDIDATE))
        self.assertFalse(case.inbox.usage_active(case.handle, ProofUse.EXECUTING))
        self.assertEqual(case.f.executor.action_index, 0)

    def test_midstream_failure_preserves_executing_tail_and_ledger_during_closeout(self):
        from mc2p.motion_nav.prepared_motion import ProofUse
        case = self.native_case(negative_case_id="midstream_missing")
        owner, ledger = case.f.supervisor.route, case.f.ledger
        case.run(negative="midstream_missing")
        self.assertEqual(case.typed_result, "DEVIATION")
        before = ledger.snapshot()
        command_count = len(case.backend.actions)
        for use in (ProofUse.EXECUTING, ProofUse.SAFE_TAIL):
            self.assertTrue(case.inbox.usage_active(case.handle, use))
        self.assertFalse(case.release())
        self.assertEqual(len(case.backend.actions), command_count,
                         "closeout must not invent extra Neutral commands")
        self.assertIs(case.f.supervisor.route, owner)
        self.assertIs(case.f.ledger, ledger)
        self.assertEqual(ledger.snapshot(), before)
        for use in (ProofUse.EXECUTING, ProofUse.SAFE_TAIL):
            self.assertTrue(case.inbox.usage_active(case.handle, use))
        self.assertTrue(any(sample["kind"] == "closeout" for sample in case.meter.samples))
        self.assertFalse(case.completed)

    def test_runner_rejects_expired_window_before_submission_and_keeps_owner(self):
        from mc2p.contracts.common import ContractViolation
        case = self.native_case()
        owner = case.f.supervisor.route
        native_decide = case.f.executor.decide
        def expired(*args, **kwargs):
            return replace(native_decide(*args, **kwargs), expected_movement_tick=9, latest_movement_tick=10)
        with patch.object(case.f.executor, "decide", side_effect=expired), \
                self.assertRaisesRegex(ContractViolation, "expired"):
            case.tick()
        self.assertEqual(case.backend.actions, [])
        self.assertEqual(case.verified_submissions, [])
        self.assertEqual(case.f.ledger.snapshot(), ())
        self.assertIs(case.f.supervisor.route, owner)

    def test_typed_failure_stops_normal_loop_and_keeps_measured_owner_closeout(self):
        from mc2p.motion_nav.action_route_executor import ActionRouteState
        from mc2p.motion_nav.world_model import ObservationStamp
        case = self.native_case()
        owner, ledger = case.f.supervisor.route, case.f.ledger
        position = case.f.proofs[0].world_dependencies[0]
        stamp = ObservationStamp(case.f.world.session, 2, case.backend.state.movement_tick_id,
                                 "actual-dependency-loss", case.clock[0])
        case.f.world.invalidate(stamp, (position,))
        native_frame, native_decide = case.f.frame, case.f.executor.decide
        terminal_observations = []
        def changed_frame(anchor):
            return replace(native_frame(anchor), changed_cells=(position,))
        def counted_decide(frame, **kwargs):
            decision = native_decide(frame, **kwargs)
            if decision.state is ActionRouteState.FAILED:
                terminal_observations.append((frame.body.sequence_id, frame.body.movement_tick_id))
            return decision
        with patch.object(case.f, "frame", side_effect=changed_frame), \
                patch.object(case.f.executor, "decide", side_effect=counted_decide), \
                patch.object(case.f.executor, "stage_prepared_action", wraps=case.f.executor.stage_prepared_action) as stage:
            case.run()
            self.assertEqual(terminal_observations, [(0, 10)])
            self.assertEqual(stage.call_count, 0, "a failed first owner must not request the second jump")
            self.assertEqual(case.backend.actions, [])
            self.assertIs(case.f.supervisor.route, owner)
            self.assertIs(case.f.ledger, ledger)
            before = ledger.snapshot()
            self.assertFalse(case.release(), "unproved residual movement cannot release the owner")
        self.assertIs(case.f.supervisor.route, owner)
        self.assertEqual(ledger.snapshot(), before)
        closeouts = [sample for sample in case.meter.samples if sample["kind"] == "closeout"]
        self.assertTrue(closeouts, "necessary owner and supervisor closeout must stay in the meter")
        self.assertTrue(all(set(sample["phases_ns"]) == set(self.meter().PHASES) for sample in closeouts))
        self.assertFalse(case.completed)
        self.assertEqual(case.typed_result, "FAILED")

    def test_realtime_denominator_excludes_fast_and_fallback(self):
        meter = self.meter()
        cases = meter.matrix_cases("realtime", MANIFEST)
        self.assertEqual(len(cases), 60)
        self.assertEqual({row["direction"] for row in cases}, {"south"})
        self.assertEqual({row["entry_offset"] for row in cases}, {0.})
        self.assertEqual({row["repetition"] for row in cases}, {1, 2, 3, 4, 5})
        rows = [dict(group="realtime", warmup=False, complete_decision_ns=7)] * 60
        rows += [dict(group=group, warmup=warmup, complete_decision_ns=99)
                 for group, warmup in (("fast", False), ("fallback", False), ("realtime", True))]
        selected = meter.realtime_samples(rows)
        self.assertEqual(selected, [7] * 60)

    def test_ready_is_actual_publish_and_first_consume(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        events = raw["native_events"]
        publish = [row for row in events if row["operation"] == "publish"]
        consume = [row for row in events if row["operation"] == "first_consume"]
        self.assertEqual(len(publish), 1)
        self.assertEqual(len(consume), 1)
        self.assertEqual(raw["ready_ns"], publish[0]["timestamp_ns"])
        self.assertGreaterEqual(consume[0]["timestamp_ns"], raw["ready_ns"])
        self.assertEqual(publish[0]["thread_id"], raw["receiver_thread_id"])
        self.assertNotEqual(publish[0]["thread_id"], consume[0]["thread_id"])
        changed = deepcopy(raw)
        changed["ready_ns"] = raw["request_preparation_started_ns"]
        self.assertTrue(self.meter().validate_probe(changed, check_performance=False))

    def test_hot_path_counts_and_control_phase_coverage_are_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        meter = self.meter()
        required = {"observation", "handle", "comparison", "ledger_receipt", "budget",
                    "handoff", "arbitration", "send"}
        self.assertTrue(raw["control_samples"])
        self.assertTrue(all(set(row["phases_ns"]) == required for row in raw["control_samples"]))
        self.assertEqual(set(raw["hot_path_calls"]), set(meter.FORBIDDEN_OPERATIONS))
        changed = deepcopy(raw)
        del changed["control_samples"][0]["phases_ns"]["observation"]
        self.assertTrue(meter.validate_probe(changed, check_performance=False))
        changed = deepcopy(raw)
        del changed["hot_path_calls"]["deserialize"]
        self.assertTrue(meter.validate_probe(changed, check_performance=False))

    def test_third_job_and_byte_capacity_are_rejected(self):
        # Actual admission is held at a receiver barrier; queue length is not enough.
        with tempfile.TemporaryDirectory() as temporary:
            report = self.meter().capacity_probe(output=Path(temporary))
        self.assertEqual(report["third_job"], "QUEUE_FULL")
        self.assertEqual(report["lease_overflow"], "CAPACITY")
        self.assertEqual(report["byte_overflow"], "QUEUE_FULL")
        self.assertEqual(report["unfinished_jobs_at_third"], 2)
        self.assertEqual(report["unfinished_jobs_at_byte_check"], 0)
        self.assertEqual(report["retained_bytes_at_byte_check"], 16_777_216)
        self.assertEqual(report["responsibility_reference_evictions"], 0)
        self.assertTrue(all(value == 0 for value in report["final_resources"].values()))

    def test_recompute_preserves_raw_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sample(root)
            index = root / "index.json"
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
            with patch("mc2p.motion_nav.motion_worker.MotionSolverWorker",
                       side_effect=AssertionError("recompute must not create a worker")):
                report = self.meter().recompute_run(index)
            self.assertEqual(report["raw_case_count"], 1)
            self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in root.rglob("*") if p.is_file()})
            document = json.loads(index.read_text("utf-8"))
            raw_path = root / document["cases"][0]["raw_path"]
            with raw_path.open("ab") as stream:
                stream.write(b" ")
            with self.assertRaisesRegex(ValueError, "raw hash mismatch"):
                self.meter().recompute_run(index)

    def test_artificial_over_30ms_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        raw["control_samples"][0]["duration_ns"] = 30_000_001
        errors = self.meter().validate_probe(raw, check_performance=True)
        self.assertTrue(any("maximum" in error for error in errors), errors)

    def test_forged_ready_event_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        changed = deepcopy(raw)
        for event in changed["native_events"]:
            if event["operation"] == "publish":
                event["thread_id"] = changed["control_thread_id"]
                event["producer"] = "queue"
        self.assertTrue(self.meter().validate_probe(changed, check_performance=False))

    def test_resealed_payload_tampering_cannot_pass_recompute(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.sample(root)
            index_path = root / "index.json"
            index = json.loads(index_path.read_text("utf-8"))
            raw_path = root / index["cases"][0]["raw_path"]
            raw = json.loads(raw_path.read_text("utf-8"))
            raw["proof"] = None
            raw_path.write_text(json.dumps(raw), "utf-8")
            index["cases"][0]["raw_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            index_path.write_text(json.dumps(index), "utf-8")
            with self.assertRaisesRegex(ValueError, "payload"):
                self.meter().recompute_run(index_path)

    def test_first_start_offset_is_proved_by_native_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        self.assertEqual(self.meter().validate_probe(raw, check_performance=False), ())
        changed = deepcopy(raw)
        changed["actual_first_start_offset"] = 1
        self.assertTrue(self.meter().validate_probe(changed, check_performance=False))

    def test_background_release_does_not_extend_finished_control_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        for sample in raw["control_samples"]:
            self.assertGreaterEqual(sample["duration_ns"], sum(sample["phases_ns"].values()))

    def test_output_is_unique_and_default_tests_stay_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.sample(temporary)
            with self.assertRaisesRegex(ValueError, "OUTPUT_EXISTS"):
                self.sample(temporary)

    def test_scope_outside_exact_entry_is_typed_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary, negative_case_id="entry")
        self.assertEqual(raw["typed_result"], "ENTRY_MISMATCH")
        self.assertEqual(raw["unauthorized_body_steps"], 0)
        self.assertEqual(raw["second_jump_authorizations"], 0)

    def test_negative_transport_is_rejected_by_the_existing_receiver(self):
        for case_id, want in (("identity", "REQUEST"), ("inputs", "INPUTS"), ("ruleset", "RULESET")):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as temporary:
                raw = self.sample(temporary, negative_case_id=case_id)
                self.assertEqual(raw["typed_result"], want)
                self.assertEqual(raw["second_jump_authorizations"], 0)
                self.assertEqual(raw["unauthorized_body_steps"], 0)
                self.assertEqual(raw["native_rejection"]["receiver_thread_id"], raw["receiver_thread_id"])

    def test_unknown_and_cancel_orders_produce_real_typed_results(self):
        for case_id, want in (("unknown", "MISSING_INFORMATION"),
                              ("cancel_before_publish", "CANCELLED"),
                              ("cancel_after_publish", "STALE")):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as temporary:
                raw = self.sample(temporary, negative_case_id=case_id)
                self.assertEqual(raw.get("typed_result"), want, raw.get("failure_reason"))
                self.assertEqual(raw["second_jump_authorizations"], 0)
                self.assertTrue(all(value == 0 for value in raw["final_resources"].values()))

    def test_fixture_backend_calls_are_separately_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        events = [event for sample in raw["control_samples"] for event in sample["forbidden_events"]]
        fixture = [event for event in events if event.get("provenance") == "fixture_backend"]
        for operation, count in raw["fixture_backend_calls"].items():
            self.assertEqual(count, sum(event["operation"] == operation for event in fixture), operation)
        self.assertGreater(raw["fixture_backend_calls"]["physics_replay"], 0)
        self.assertGreater(raw["native_motion_trace"][-1]["after"]["movement_tick_id"],
                           raw["native_motion_trace"][0]["before"]["movement_tick_id"])
        native = [event for event in events
                  if event.get("provenance") == "control" and event["operation"] == "serialize"]
        self.assertEqual(raw["hot_path_calls"]["serialize"], len(native))

    def test_complete_control_meter_has_no_observation_request_capacity_encoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        events = [event for sample in raw["control_samples"] for event in sample["forbidden_events"]]
        capacity = [event for event in events if event.get("provenance") == "control"
                    and event["operation"] == "serialize"
                    and "mc2p.contracts.observation_request_v3.__post_init__" in event["callers"]]
        self.assertEqual(len(capacity), 0, "complete control meter still sees request capacity JSON")
        self.assertEqual(raw["hot_path_calls"]["serialize"], 0)
        self.assertGreater(raw["hot_path_calls"]["immutable_graph"], 0,
                           "Task B must preserve visibility of remaining Task C graph work")
        self.assertGreater(raw["fixture_backend_calls"]["physics_replay"], 0)
        self.assertTrue(any(sample["kind"] == "closeout" for sample in raw["control_samples"]))

    def test_g2_directory_verifier_reports_explicit_missing_stages_without_writes(self):
        from scripts.verify_prepared_motion_d102 import main
        from contextlib import redirect_stdout
        from io import StringIO
        with tempfile.TemporaryDirectory() as temporary:
            before = list(Path(temporary).iterdir())
            output = StringIO()
            with redirect_stdout(output), patch.object(self.meter(), "MotionSolverWorker",
                    side_effect=AssertionError("verification cannot start a worker")):
                status = main(["--stage", "G2", "--input", temporary])
            self.assertEqual(status, 1)
            self.assertIn("missing G2 stage: fast", output.getvalue())
            self.assertIn("missing G2 stage: fallback", output.getvalue())
            self.assertEqual(before, list(Path(temporary).iterdir()))

    def test_stage_order_cannot_be_bypassed_by_starting_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "spawn"
            with self.assertRaisesRegex(ValueError, "previous G2 stage.*fast"), patch.object(
                    self.meter(), "MotionSolverWorker", side_effect=AssertionError("worker started too early")):
                self.meter().run_d102_matrix("spawn-smoke", manifest=MANIFEST, output=output)
            self.assertFalse(output.exists())

    def test_full_meter_includes_prepare_and_terminal_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        self.assertTrue(any(e.get("operation") == "prepare" and e.get("meter_active") is True
                            for e in raw["native_events"]))
        self.assertTrue(any(sample["kind"] == "finish" for sample in raw["control_samples"]))
        self.assertEqual(raw["control_samples"][-1]["kind"], "closeout")

    def test_slow_measurement_clock_does_not_change_movement_results(self):
        real_clock = time.perf_counter_ns
        with tempfile.TemporaryDirectory() as temporary:
            normal = self.sample(Path(temporary) / "normal")
            with patch("time.perf_counter_ns", side_effect=lambda: real_clock() * 10):
                slow = self.sample(Path(temporary) / "slow")
        for name in ("status", "native_final_state", "actual_first_start_offset", "continuous_complete",
                     "second_jump_authorizations", "actual_application_ticks", "unauthorized_body_steps",
                     "safety_violations", "released_by_original_owner", "final_resources"):
            self.assertEqual(normal[name], slow[name], name)

    def test_native_runtime_activation_and_receipt_cannot_be_removed_or_rebound(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.sample(temporary)
        for mutation in ("missing", "ledger", "generation", "receipt"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(raw)
                if mutation == "missing":
                    changed["native_activations"] = []
                elif mutation == "receipt":
                    changed["native_applications"][0]["application"]["effect_tick"] += 1
                else:
                    changed["native_activations"][0]["binding"][mutation + "_id"] = "unrelated"
                self.assertTrue(self.meter().validate_probe(changed, check_performance=False))

    def test_spawn_probe_waits_for_this_job_publication_before_movement_handoff(self):
        axes = dict(platform_width=2, start_offset=0, entry_offset=-.05, direction="south", backlog=0)
        with tempfile.TemporaryDirectory() as temporary:
            raw = self.meter().record_probe(axes, output=Path(temporary), manifest=MANIFEST, transport="spawn")
        self.assertEqual(raw["status"], "PASS", raw.get("failure_reason"))
        self.assertEqual(self.meter().validate_probe(raw, check_performance=False), ())
        self.assertIsInstance(raw["native_worker_pid"], int)
        self.assertTrue(raw["released_by_original_owner"])
        self.assertTrue(all(value == 0 for value in raw["final_resources"].values()))


if __name__ == "__main__":
    unittest.main()
