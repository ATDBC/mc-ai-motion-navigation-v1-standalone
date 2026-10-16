from __future__ import annotations

from dataclasses import asdict, replace
import ast
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_receipt import (
    ClientBehaviorReceiptV3,
    ClientInputApplicationV1,
)
from mc2p.contracts.action_v1 import ActionSnapshotV1
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.online_motion import InputApplicationStatus
from mc2p.motion_nav.motion_candidate import (
    MotionCandidateAdmitter,
    MotionCandidateStatus,
    VerifiedMotionExecutor,
    VerifiedMotionExecutorState,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget

from scripts import b10_gap_solver_runtime as probe
from scripts.b10_gap_solver_runtime import (
    _information_retry_allowed,
    _bounded_fixture_feet_y,
    _inspection_support,
    _inspection_supports,
    _manual_executor_submission_window,
    _input_window_diagnostics,
    _observation_vantage,
    _pending_air_request,
    _runtime_input_ledger,
    _runtime_navigation_frame,
    _targeted_information_request,
    _verified_submission_window,
    _waiting_control_proposals,
)
from mc2p.contracts.intent_source import ControlFrameProposalV1
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.motion_nav.world_model import CellKnowledge


class _Adapter:
    def __init__(self, frame) -> None:
        self.latest_frame = frame
        self.ingest_calls = 0

    def ingest(self, _observation):
        self.ingest_calls += 1
        raise AssertionError("B10 must not ingest a Runtime-owned observation twice")


class B10RuntimeProbeTests(unittest.TestCase):
    def test_coordinator_matrix_preregisters_all_56_scenarios(self):
        scenarios = probe._b10_coordinator_scenarios()

        self.assertEqual(len(scenarios), 56)
        self.assertEqual(
            sum(item.kind == "baseline" for item in scenarios),
            20,
        )
        self.assertEqual(
            sum(item.kind == "first_input_late1" for item in scenarios),
            8,
        )
        self.assertEqual(
            {
                delay: sum(
                    item.kind == "worker_delay"
                    and item.worker_delay_ticks == delay
                    for item in scenarios
                )
                for delay in (2, 5, 10)
            },
            {2: 8, 5: 8, 10: 8},
        )
        self.assertEqual(
            sum(item.kind == "worker_never_return" for item in scenarios),
            4,
        )
        self.assertEqual(
            {item.direction_index for item in scenarios},
            {0, 1, 2, 3},
        )
        late = tuple(
            item for item in scenarios if item.kind == "first_input_late1"
        )
        self.assertTrue(all(not item.fabric_supported for item in late))
        self.assertTrue(all(
            item.unsupported_reason == "fabric_input_late1_injection_unavailable"
            for item in late
        ))

    def test_tick_delayed_worker_leaves_receiver_publication_natural_without_sleep(self):
        class FakeWorker:
            def __init__(self):
                self.available = []
                self.submitted = []

            def submit(self, job):
                self.submitted.append(job)
                return True

            def poll_available(self):
                available, self.available = tuple(self.available), []
                return available

            def close(self):
                raise AssertionError("the fixture wrapper does not own the worker")

            def is_alive(self):
                return True

        real = FakeWorker()
        delayed = probe._MovementTickDelayedMotionWorker(real)
        scenario = probe._B10CoordinatorScenario(
            "worker-delay-2-east-0", "worker_delay", 0, 0,
            worker_delay_ticks=2,
        )
        job = SimpleNamespace(work_identity="identity-1")
        result = SimpleNamespace(work_identity="identity-1")

        delayed.begin_trial(scenario, movement_tick=40)
        self.assertTrue(delayed.submit(job))
        real.available.append(result)
        self.assertEqual(delayed.poll_available(), (result,))
        delayed.publish_movement_tick(41)
        self.assertEqual(delayed.poll_available(), ())
        delayed.publish_movement_tick(42)
        self.assertEqual(delayed.poll_available(), ())
        evidence = delayed.end_trial()

        self.assertEqual(evidence["scenario"], scenario.name)
        self.assertEqual(evidence["worker_delay_ticks"], 2)
        self.assertEqual(len(evidence["submissions"]), 1)
        self.assertEqual({key: evidence["submissions"][0][key] for key in (
            "identity", "submit_tick", "worker_observe_tick", "wrapper_publish_tick",
            "discarded_as_stale")}, {
            "identity": "'identity-1'",
            "submit_tick": 40,
            "worker_observe_tick": 40,
            "wrapper_publish_tick": 40,
            "discarded_as_stale": False,
        })
        self.assertEqual(evidence["submissions"][0]["injection_status"], "incomplete")

    def test_tick_delayed_worker_never_return_and_next_trial_drop_stale_results(self):
        class FakeWorker:
            def __init__(self):
                self.available = []

            def submit(self, _job):
                return True

            def poll_available(self):
                available, self.available = tuple(self.available), []
                return available

            def close(self):
                raise AssertionError("the fixture wrapper does not own the worker")

            def is_alive(self):
                return True

        real = FakeWorker()
        delayed = probe._MovementTickDelayedMotionWorker(real)
        never = probe._B10CoordinatorScenario(
            "never-east", "worker_never_return", 0, 0,
            never_return=True,
        )
        old_job = SimpleNamespace(work_identity="old")
        old_result = SimpleNamespace(work_identity="old")

        delayed.begin_trial(never, movement_tick=80)
        self.assertTrue(delayed.submit(old_job))
        real.available.append(old_result)
        self.assertEqual(delayed.poll_available(), ())
        delayed.publish_movement_tick(120)
        self.assertEqual(delayed.poll_available(), ())
        evidence = delayed.end_trial()
        self.assertIsNone(
            evidence["submissions"][0]["wrapper_publish_tick"],
        )
        self.assertEqual(evidence["held_result_count"], 1)

        baseline = probe._B10CoordinatorScenario(
            "baseline-east", "baseline", 0, 0,
        )
        delayed.begin_trial(baseline, movement_tick=121)
        # A result that completed after the old trial ended must never be
        # released into the new session.
        real.available.append(old_result)
        self.assertEqual(delayed.poll_available(), ())
        next_evidence = delayed.end_trial()
        self.assertEqual(next_evidence["stale_result_count"], 1)

    def test_tick_delay_is_total_from_submit_not_extra_after_real_completion(self):
        class SlowWorker:
            def __init__(self):
                self.available = []

            def submit(self, _job):
                return True

            def poll_available(self):
                available, self.available = tuple(self.available), []
                return available

            def close(self):
                raise AssertionError("the fixture wrapper does not own the worker")

            def is_alive(self):
                return True

        real = SlowWorker()
        delayed = probe._MovementTickDelayedMotionWorker(real)
        scenario = probe._B10CoordinatorScenario(
            "worker-delay-2-east", "worker_delay", 0, 0,
            worker_delay_ticks=2,
        )
        job = SimpleNamespace(work_identity="identity")
        result = SimpleNamespace(work_identity="identity")

        delayed.begin_trial(scenario, movement_tick=40)
        self.assertTrue(delayed.submit(job))
        delayed.publish_movement_tick(44)
        real.available.append(result)

        self.assertEqual(delayed.poll_available(), (result,))
        evidence = delayed.end_trial()
        self.assertEqual(evidence["submissions"][0]["submit_tick"], 40)
        self.assertEqual(evidence["submissions"][0]["worker_observe_tick"], 44)
        self.assertEqual(evidence["submissions"][0]["wrapper_publish_tick"], 44)

    def test_formal_coordinator_uses_the_preregistered_tick_delay_port(self):
        source = inspect.getsource(probe._run_b10_gap_solver_runtime)

        self.assertIn("_selected_b10_coordinator_scenarios(targeted_probe)", source)
        self.assertEqual(probe._selected_b10_coordinator_scenarios(None),
                         probe._b10_coordinator_scenarios())
        self.assertIn("_MovementTickDelayedMotionWorker(motion_worker)", source)
        self.assertIn("delayed_motion_worker.begin_trial(", source)
        self.assertIn("delayed_motion_worker.publish_movement_tick(", source)
        self.assertIn("motion_worker=delayed_motion_worker", source)
        self.assertIn("delayed_motion_worker.end_trial", source)
        self.assertIn("delayed_motion_worker.install_read_gate(session._motion_result_inbox)", source)
        self.assertIn(')["deliveries"]', source)
        self.assertNotIn(')["delivery"]', source)
        self.assertNotIn("time.sleep(scenario.worker_delay", source)
        self.assertIn("coordinator_delivery_gate(trial)", source)
        self.assertIn('sample["actual_minus_expected_ticks"] >= 0', source)
        self.assertIn('sample["actual_minus_latest_ticks"] <= 0', source)


    def test_local_diagnostics_keep_strings_until_control_trials_finish(self):
        tree = ast.parse(inspect.getsource(probe._run_b10_gap_solver_runtime))
        diagnostic = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.FunctionDef) and node.name == "diagnostic")
        with tempfile.TemporaryDirectory() as temporary:
            rows = []
            backend = SimpleNamespace(last_diagnostics={"nested": [{"tick": 3}]})
            buffer = probe._B10EvidenceBuffer(Path(temporary))
            namespace = dict(vars(probe), rows=rows, backend=backend, evidence_buffer=buffer,
                             episode="e", pipeline_diagnostic=lambda: {"nested": [4]},
                             runtime=SimpleNamespace(observation=SimpleNamespace(sequence_id=2)))
            exec(compile(ast.fix_missing_locations(ast.Module(body=[diagnostic], type_ignores=[])),
                         "<B10 diagnostic>", "exec"), namespace)
            with patch.object(probe.json, "loads", side_effect=AssertionError("hot parse")):
                namespace["diagnostic"]()
            self.assertEqual(len(rows), 1)
            self.assertIs(type(rows[0]), str)
            backend.last_diagnostics["nested"][0]["tick"] = 999
            self.assertEqual(json.loads(rows[0]), buffer.rows("diagnostics.jsonl")[0])
            self.assertEqual(json.loads(rows[0])["diagnostics"]["nested"][0]["tick"], 3)

    def test_gc_pause_can_overlap_multiple_samples_and_stop_in_a_later_sample(self):
        timeline = probe._GCPauseTimeline()
        timeline.events.extend([(2, "start", 120)])
        trials = [{"solve_started_ns": start, "solve_ended_ns": end,
                   "solve_time_ns": end - start}
                  for start, end in ((100, 150), (160, 200), (250, 300))]
        timeline.events.append((2, "stop", 180))
        result = probe._attribute_solve_gc_pauses(trials, timeline)
        self.assertEqual([row["solve_gc_pause_overlap_ns"] for row in trials], [30, 20, 0])
        self.assertEqual([row["solve_wall_minus_gc_pause_ns"] for row in trials], [20, 20, 50])
        self.assertTrue(result["gc_pause_timeline"]["complete"])

    def test_callback_clock_failure_inside_solver_never_masks_solver_exception(self):
        timeline = probe._GCPauseTimeline()
        error = ValueError("solver failed")
        def solve(*_args):
            timeline.callback("start", {"generation": 2})
            raise error
        with (patch.object(probe, "solve_one_cell_gap", side_effect=solve),
              patch.object(probe.time, "perf_counter_ns", side_effect=[100, OSError("callback clock")]),
              self.assertRaises(ValueError) as raised):
            probe._measure_gap_solve(1, 2, 3)
        self.assertIs(raised.exception, error)
        self.assertEqual(timeline.measurement_error_count, 1)

    def test_full_probe_installs_gc_timeline_and_removes_only_it_on_all_exits(self):
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as temporary:
                runtime = unittest.mock.MagicMock()
                runtime.state.value = "ready"
                existing = lambda *_args: None
                callbacks = [existing]
                seen = []
                error = ValueError("original solver failure")
                def execute(*_args, **kwargs):
                    timeline = kwargs["gc_timeline"]
                    seen.append(timeline)
                    self.assertEqual(callbacks, [existing, timeline.callback])
                    if fails:
                        raise error
                    return "result"
                with (patch.object(probe.gc, "callbacks", callbacks),
                      patch.object(probe, "PlannerWorker"),
                      patch.object(probe, "MotionSolverWorker"),
                      patch.object(probe, "_run_b10_gap_solver_runtime", side_effect=execute)):
                    if fails:
                        with self.assertRaises(ValueError) as raised:
                            probe.run_b10_gap_solver_runtime(runtime, None, "episode", Path(temporary),
                                                           1, lambda *_: None, lambda *_: None)
                        self.assertIs(raised.exception, error)
                    else:
                        self.assertEqual(probe.run_b10_gap_solver_runtime(
                            runtime, None, "episode", Path(temporary), 1,
                            lambda *_: None, lambda *_: None), "result")
                self.assertEqual(len(seen), 1)
                self.assertEqual(callbacks, [existing])

    def test_gc_timeline_attributes_other_thread_and_late_stop_after_solve(self):
        timeline = probe._GCPauseTimeline()
        expected = object()
        def solve(*_args):
            thread = threading.Thread(target=lambda: timeline.callback("start", {"generation": 2}))
            thread.start()
            thread.join()
            return expected
        with patch.object(probe.time, "perf_counter_ns", side_effect=[100, 120, 160, 190]):
            with patch.object(probe, "solve_one_cell_gap", side_effect=solve):
                actual, measurement = probe._measure_gap_solve(1, 2, 3)
            self.assertEqual(len(timeline.events), 1)
            timeline.callback("stop", {"generation": 2})
        summary = probe._attribute_solve_gc_pauses([measurement], timeline)
        self.assertIs(actual, expected)
        self.assertEqual((measurement["solve_started_ns"], measurement["solve_ended_ns"]), (100, 160))
        self.assertEqual(measurement["solve_gc_pause_overlap_ns"], 40)
        self.assertEqual(measurement["solve_wall_minus_gc_pause_ns"], 20)
        self.assertEqual(measurement["solve_gc_pause_overlap_counts"], {"gen0": 0, "gen1": 0, "gen2": 1})
        self.assertEqual(summary["gc_pause_timeline"]["intervals"], [{"generation": 2, "start_ns": 120, "end_ns": 190}])

    def test_gc_timeline_unpaired_overflow_and_bad_clock_are_visible_unknown(self):
        for events, limit in (([("stop", 1), ("start", 2)], 10),
                              ([("start", 0), ("stop", 0)], 1)):
            with self.subTest(events=events):
                timeline = probe._GCPauseTimeline(max_events=limit)
                with patch.object(probe.time, "perf_counter_ns", side_effect=[110, 140]):
                    for phase, generation in events:
                        timeline.callback(phase, {"generation": generation})
                trial = {"solve_started_ns": 100, "solve_ended_ns": 200, "solve_time_ns": 100}
                result = probe._attribute_solve_gc_pauses([trial], timeline)
                self.assertFalse(result["gc_pause_timeline"]["complete"])
                self.assertIsNone(trial["solve_wall_minus_gc_pause_ns"])
        timeline = probe._GCPauseTimeline()
        with patch.object(probe.time, "perf_counter_ns", side_effect=OSError("clock")):
            timeline.callback("start", {"generation": 0})
        self.assertEqual(timeline.measurement_error_count, 1)

    def test_gc_callback_finally_preserves_existing_callbacks_and_solver_error(self):
        existing = lambda *_args: None
        callbacks = [existing]
        error = ValueError("solver failed")
        with patch.object(probe.gc, "callbacks", callbacks):
            timeline = probe._GCPauseTimeline()
            with self.assertRaises(ValueError) as raised:
                with timeline:
                    self.assertIs(callbacks[0], existing)
                    with patch.object(probe.time, "perf_counter_ns", side_effect=OSError("clock")):
                        timeline.callback("start", {"generation": 0})
                    raise error
            self.assertIs(raised.exception, error)
            self.assertEqual(callbacks, [existing])

    def test_gc_overlap_clips_boundaries_and_uses_union_for_corrected_wall(self):
        timeline = probe._GCPauseTimeline()
        timeline.events.extend([(0, "start", 80), (0, "stop", 120),
                                (1, "start", 110), (1, "stop", 140),
                                (2, "start", 200), (2, "stop", 220)])
        trial = {"solve_started_ns": 100, "solve_ended_ns": 200, "solve_time_ns": 100}
        result = probe._attribute_solve_gc_pauses([trial], timeline)
        self.assertEqual(trial["solve_gc_pause_overlap_ns"], 40)
        self.assertEqual(trial["solve_wall_minus_gc_pause_ns"], 60)
        self.assertEqual(trial["solve_gc_pause_overlap_counts"], {"gen0": 1, "gen1": 1, "gen2": 0})
        self.assertEqual(result["solve_wall_minus_gc_pause_ns"]["maximum"], 60)

    def test_buffer_keeps_only_canonical_strings_and_detaches_nested_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            buffer = probe._B10EvidenceBuffer(Path(temporary))
            original = {"nested": [{"value": "中文\n"}], "other": 1.25}
            expected = probe.strict_json_dumps(original)
            with patch.object(probe.json, "loads", side_effect=AssertionError("hot parse")):
                self.assertTrue(buffer.append("diagnostics.jsonl", original))
            self.assertEqual(buffer._records["diagnostics.jsonl"], [expected])
            original["nested"][0]["value"] = "changed"
            decoded = buffer.rows("diagnostics.jsonl")
            decoded[0]["nested"][0]["value"] = "mutated reader"
            self.assertEqual(buffer.rows("diagnostics.jsonl")[0]["nested"][0]["value"], "中文\n")
            self.assertEqual(buffer._bytes["diagnostics.jsonl"], len(expected.encode()) + 1)

    def test_buffer_jsonl_matches_legacy_roundtrip_bytes_for_all_streams(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            buffer = probe._B10EvidenceBuffer(root)
            source = [{"z": [True, None, {"text": "中文\n\""}], "a": -0.0},
                      {"integer": 12345678901234567890, "float": 1e-12}]
            for stream in probe._B10_STREAM_SPECS:
                for row in source:
                    buffer.append(stream, row)
            buffer.flush()
            for stream in probe._B10_STREAM_SPECS:
                legacy = root / (stream + ".legacy")
                probe.write_jsonl_atomic(legacy, [json.loads(probe.strict_json_dumps(row))
                                                for row in source])
                self.assertEqual((root / stream).read_bytes(), legacy.read_bytes())

    def test_buffer_row_byte_and_record_limits_keep_accepted_order(self):
        size = len(probe.strict_json_dumps({"value": 1}).encode()) + 1
        for spec in (probe._B10StreamSpec(1, size * 3, size * 3),
                     probe._B10StreamSpec(3, size, size),
                     probe._B10StreamSpec(3, size * 3, size)):
            with self.subTest(spec=spec), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                buffer = probe._B10EvidenceBuffer(root, stream_specs={"x.jsonl": spec})
                self.assertTrue(buffer.append("x.jsonl", {"value": 1}))
                self.assertFalse(buffer.append("x.jsonl", {"value": 222222}))
                self.assertEqual(buffer.rows("x.jsonl"), ({"value": 1},))
                with self.assertRaises(probe._B10EvidenceOverflow):
                    buffer.flush()
                rows = [json.loads(line) for line in (root / "x.jsonl").read_text().splitlines()]
                self.assertEqual(rows[0], {"value": 1})
                self.assertEqual(rows[1]["overflow_count"], 1)

    def test_solver_bound_keeps_candidate_limit_but_wall_time_is_report_only(self):
        tree = ast.parse(inspect.getsource(probe._run_b10_gap_solver_runtime))
        guard = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and any(keyword.arg == "name"
                             and isinstance(keyword.value, ast.Constant)
                             and keyword.value.value == "b10b_solver_is_bounded"
                             for keyword in node.keywords))
        passed = next(keyword.value for keyword in guard.keywords
                      if keyword.arg == "passed")
        code = compile(ast.Expression(passed), "<B10 solver bound>", "eval")
        for wall, candidates, expected in (
            (29_999_999, 12, True), (30_000_000, 12, True),
            (1, 13, False), (41_033_900, 1, True),
        ):
            with self.subTest(wall=wall, candidates=candidates):
                self.assertIs(eval(code, {
                    "trials": [{"candidates_evaluated": candidates,
                                "command_count": 20,
                                "solve_thread_cpu_ns": 1,
                                "solve_gc_overlap": True}],
                    "summary": {"solve_time_ns": {"maximum": wall}},
                }), expected)

        source = inspect.getsource(probe._run_b10_gap_solver_runtime)
        self.assertIn('scope="synchronous_probe_only"', source)
        self.assertIn('disposition="report_only"', source)
        self.assertIn('warning_threshold_ns=30_000_000', source)

    def test_gc_diagnostic_completeness_is_checked_separately_from_raw_wall(self):
        tree = ast.parse(inspect.getsource(probe._run_b10_gap_solver_runtime))
        guard = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and any(keyword.arg == "name"
                             and isinstance(keyword.value, ast.Constant)
                             and keyword.value.value == "b10b_gc_pause_timeline_complete"
                             for keyword in node.keywords))
        passed = next(keyword.value for keyword in guard.keywords if keyword.arg == "passed")
        code = compile(ast.Expression(passed), "<B10 GC completeness>", "eval")
        for complete in (False, True):
            self.assertIs(eval(code, {"summary": {"gc_pause_timeline": {"complete": complete}}}), complete)

    def test_final_trial_retains_identity_entry_and_solve_measurement_fields(self):
        from tests.motion_nav.test_b10_gap_solver import fixture, B10GapSolverTests

        anchor, world, target, _ = fixture()
        solved = probe.solve_one_cell_gap(
            anchor, world, B10GapSolverTests().request(target),
        )
        # Exercise the actual nested trial body without starting a Fabric batch.
        tree = ast.parse(inspect.getsource(probe._run_b10_gap_solver_runtime))
        trial_body = next(node for node in ast.walk(tree)
                          if isinstance(node, ast.FunctionDef) and node.name == "run_trial")
        trial_body.body = [node for node in trial_body.body
                           if not isinstance(node, ast.Nonlocal)]
        executor = unittest.mock.Mock()
        executor.state = VerifiedMotionExecutorState.COMPLETE
        executor.decide.return_value.state = VerifiedMotionExecutorState.COMPLETE
        trials = []
        namespace = dict(vars(probe))
        namespace.update(
            frame=SimpleNamespace(
                session=anchor.session, world=world,
                body=SimpleNamespace(
                    sequence_id=anchor.observation_sequence_id,
                    velocity_blocks_per_second=(0, 0, 0),
                    position=(.5, 64., 2.5), is_on_ground=True,
                ),
            ),
            latest_application=SimpleNamespace(
                movement_tick_id=10, request_sequence_id=1,
            ),
            targets=((0, 63, 2),), request=object(), episode="episode",
            solve_times=[], physics_snapshot_times=[], trials=trials,
            runtime=SimpleNamespace(input_ledger=InputApplicationLedger()),
            evidence_buffer=unittest.mock.Mock(),
            build_physics_state=lambda *_args: SimpleNamespace(
                status=probe.StateBuildStatus.READY, state=anchor.physics_state,
            ),
            _gap_physics_snapshot=lambda *_args: world,
            PhysicsWorldView=lambda *_args: world,
            solve_one_cell_gap=lambda *_args: solved,
            MotionCandidateAdmitter=lambda: SimpleNamespace(
                admit=lambda *_args, **_kwargs: SimpleNamespace(
                    status=MotionCandidateStatus.ACCEPTED, candidate=object(),
                ),
            ),
            VerifiedMotionExecutor=lambda: executor,
        )
        exec(compile(ast.fix_missing_locations(ast.Module(
            body=[trial_body], type_ignores=[],
        )), "<B10 run_trial>", "exec"), namespace)

        namespace["run_trial"](0, 4, "validation", prepared_entry=True)

        trial = trials[0]
        self.assertEqual((trial["name"], trial["group"], trial["direction_index"],
                          trial["repetition"]),
                         ("validation-0-4", "validation", 0, 4))
        self.assertEqual(trial["entry_position"], list(anchor.physics_state.position))
        self.assertEqual(trial["candidates_evaluated"], solved.candidates_evaluated)
        self.assertIn("solve_thread_cpu_ns", trial)
        self.assertIn("solve_gc_collections_delta", trial)
        self.assertIn("solve_gc_overlap", trial)

    def test_solve_measurement_keeps_frozen_input_and_complete_solver_result(self):
        from tests.motion_nav.test_b10_gap_solver import fixture, B10GapSolverTests

        anchor, world, target, _ = fixture()
        request = B10GapSolverTests().request(target)
        digest = lambda value: hashlib.sha256(
            json.dumps(asdict(value), sort_keys=True).encode()
        ).hexdigest()
        self.assertEqual(digest(request),
                         "856739c0e3c1d87f441167bff75f362ae9e13e94f7dd2ab566c152cd8973784a")
        baseline = probe.solve_one_cell_gap(anchor, world, request)
        self.assertEqual(digest(baseline),
                         "d0f5204a59c51da7598adabca7b745ea2123a60650920f39db3e92e00cdedc13")

        actual, measurement = probe._measure_gap_solve(anchor, world, request)

        self.assertEqual(actual, baseline)
        self.assertEqual(digest(request),
                         "856739c0e3c1d87f441167bff75f362ae9e13e94f7dd2ab566c152cd8973784a")
        self.assertGreaterEqual(measurement["solve_time_ns"], 0)
        self.assertGreaterEqual(measurement["solve_thread_cpu_ns"], 0)
        self.assertEqual(set(measurement["solve_gc_collections_delta"]),
                         {"gen0", "gen1", "gen2"})
        self.assertIs(type(measurement["solve_gc_overlap"]), bool)
        self.assertEqual(measurement["solve_measurement_errors"], [])

    def test_solve_measurement_records_cpu_and_each_completed_gc_generation(self):
        before = [{"collections": 7}, {"collections": 5}, {"collections": 2}]
        after = [{"collections": 9}, {"collections": 5}, {"collections": 3}]
        expected = object()
        anchor, world, request = object(), object(), object()
        with (
            patch.object(probe, "solve_one_cell_gap", return_value=expected) as solve,
            patch.object(probe.time, "perf_counter_ns", side_effect=[100, 160]),
            patch.object(probe.time, "thread_time_ns", side_effect=[10, 50]),
            patch("gc.get_stats", side_effect=[before, after]),
        ):
            actual, measurement = probe._measure_gap_solve(anchor, world, request)

        self.assertIs(actual, expected)
        solve.assert_called_once_with(anchor, world, request)
        self.assertEqual(measurement, {
            "solve_time_ns": 60,
            "solve_started_ns": 100,
            "solve_ended_ns": 160,
            "solve_thread_cpu_ns": 40,
            "solve_gc_collections_delta": {"gen0": 2, "gen1": 0, "gen2": 1},
            "solve_gc_overlap": True,
            "solve_measurement_errors": [],
        })
        self.assertEqual(json.loads(json.dumps(measurement)), measurement)

    def test_failed_optional_measurement_preserves_solve_result_and_reports_unknown(self):
        expected = object()
        with (
            patch.object(probe, "solve_one_cell_gap", return_value=expected) as solve,
            patch.object(probe.time, "thread_time_ns", side_effect=OSError("cpu clock")),
            patch("gc.get_stats", side_effect=RuntimeError("gc statistics")),
        ):
            actual, measurement = probe._measure_gap_solve(1, 2, 3)

        self.assertIs(actual, expected)
        solve.assert_called_once_with(1, 2, 3)
        self.assertIsNone(measurement["solve_thread_cpu_ns"])
        self.assertEqual(measurement["solve_gc_collections_delta"],
                         {"gen0": None, "gen1": None, "gen2": None})
        self.assertIs(measurement["solve_gc_overlap"], False)
        self.assertEqual({error["stage"] for error in
                          measurement["solve_measurement_errors"]},
                         {"thread_cpu_before", "thread_cpu_after", "gc_before", "gc_after"})

    def test_solver_error_survives_optional_measurement_errors(self):
        expected = ValueError("original solver failure")
        with (
            patch.object(probe, "solve_one_cell_gap", side_effect=expected) as solve,
            patch.object(probe.time, "thread_time_ns", side_effect=OSError("cpu clock")),
            patch("gc.get_stats", side_effect=RuntimeError("gc statistics")),
            self.assertRaises(ValueError) as raised,
        ):
            probe._measure_gap_solve(1, 2, 3)
        self.assertIs(raised.exception, expected)
        solve.assert_called_once_with(1, 2, 3)

    def test_solve_summary_counts_only_available_cpu_samples_and_gc_overlap(self):
        rows = [
            {"solve_thread_cpu_ns": value, "solve_gc_overlap": index == 2,
             "solve_measurement_errors": []}
            for index, value in enumerate(range(1, 101))
        ]
        rows.append({"solve_thread_cpu_ns": None, "solve_gc_overlap": False,
                     "solve_measurement_errors": [{"stage": "gc_before"}]})

        summary = probe._solve_measurement_summary(rows)

        self.assertEqual(summary, {
            "solve_thread_cpu_ns": {"count": 100, "min": 1, "max": 100,
                                    "p95": 95, "p99": 99},
            "solve_gc_overlap_count": 1,
            "solve_measurement_error_count": 1,
        })
        self.assertEqual(probe._solve_measurement_summary([])["solve_thread_cpu_ns"],
                         {"count": 0, "min": None, "max": None,
                          "p95": None, "p99": None})

    @staticmethod
    def _receipt(
            request_sequence_id: int,
            applications: tuple[ClientInputApplicationV1, ...],
    ) -> ClientBehaviorReceiptV3:
        return ClientBehaviorReceiptV3(
            "mc2p.client_action_receipt.v3",
            request_sequence_id + 1,
            "client_behavior_v1",
            "episode",
            request_sequence_id,
            "executed",
            "controls_applied",
            "Render thread",
            True,
            "client_tick_action_boundary",
            100,
            0,
            0,
            0,
            0,
            applications[-1].movement_tick_id,
            2,
            applications,
            0,
            applications[0].movement_tick_id,
        )

    def test_information_wait_keeps_the_route_look_and_requests_a_new_frame(self):
        route_control = ControlFrameProposalV1()
        proposal = SimpleNamespace(
            control_frame=route_control,
            route_decision=None,
        )
        frame = SimpleNamespace(world=SimpleNamespace(
            cell=lambda _position: SimpleNamespace(
                knowledge=CellKnowledge.UNKNOWN,
            ),
        ))
        request = ObservationRequestV3("navigation_v1", ((1, 2, 3),))

        controls = _waiting_control_proposals(proposal, frame, request)

        self.assertEqual(controls[0], route_control)
        self.assertEqual(controls[1].observation_request, request)

    def test_verified_motion_wait_does_not_keep_changing_the_world_basis(self):
        route_control = ControlFrameProposalV1()
        proposal = SimpleNamespace(
            control_frame=route_control,
            route_decision=SimpleNamespace(
                reason_code="awaiting_verified_motion",
            ),
        )

        controls = _waiting_control_proposals(proposal, object(), object())

        self.assertEqual(controls, (route_control,))

    def test_wait_without_a_control_frame_does_not_emit_an_empty_tick(self):
        self.assertEqual(
            _waiting_control_proposals(
                SimpleNamespace(control_frame=None), object(), None,
            ),
            (),
        )

    def test_information_retry_continues_while_solver_reveals_new_cells(self):
        seen = {(1, 2, 3), (2, 2, 3), (3, 2, 3)}

        self.assertTrue(_information_retry_allowed(
            ((4, 2, 3),), seen, completed_rounds=3, max_rounds=20,
        ))

    def test_information_retry_stops_when_no_new_fact_can_be_requested(self):
        seen = {(1, 2, 3)}

        self.assertFalse(_information_retry_allowed(
            ((1, 2, 3),), seen, completed_rounds=1, max_rounds=20,
        ))
        self.assertFalse(_information_retry_allowed(
            ((2, 2, 3),), seen, completed_rounds=20, max_rounds=20,
        ))

    def test_gap_fixture_is_lifted_above_the_world_floor(self):
        self.assertEqual(_bounded_fixture_feet_y(-60.0), 0)
        self.assertEqual(_bounded_fixture_feet_y(64.0), 64)

    def test_below_ground_fact_gets_a_side_inspection_support(self):
        self.assertEqual(_inspection_support((10, -2, -5)), (12, -3, -5))
        self.assertEqual(
            _inspection_supports((10, -2, -5)),
            (
                (12, -3, -5),
                (8, -3, -5),
                (10, -3, -3),
                (10, -3, -7),
            ),
        )
        self.assertNotIn(
            (12, -3, -5),
            _inspection_supports(
                (10, -2, -5),
                excluded_columns=frozenset({(12, -5)}),
            ),
        )

    def test_air_request_rechecks_only_unknown_or_previously_blocked_cells(self):
        knowledge = {
            (1, 64, 0): CellKnowledge.AIR,
            (2, 64, 0): CellKnowledge.UNKNOWN,
            (3, 64, 0): CellKnowledge.BLOCK,
        }
        frame = SimpleNamespace(world=SimpleNamespace(
            cell=lambda position: SimpleNamespace(knowledge=knowledge[position]),
        ))
        request = ObservationRequestV3(
            "navigation_v1",
            ((1, 64, 0), (2, 64, 0), (3, 64, 0)),
        )

        pending = _pending_air_request(frame, request)

        self.assertEqual(
            pending,
            ObservationRequestV3(
                "navigation_v1", ((2, 64, 0), (3, 64, 0)),
            ),
        )

    def test_air_request_batches_large_fixture_for_surface_frame_limit(self):
        positions = tuple((index, 64, 0) for index in range(200))
        frame = SimpleNamespace(world=SimpleNamespace(
            cell=lambda _position: SimpleNamespace(
                knowledge=CellKnowledge.UNKNOWN,
            ),
        ))
        request = SimpleNamespace(
            field_profile="navigation_v1",
            air_positions=positions,
            entity_track_id=None,
        )

        pending = _pending_air_request(frame, request)

        self.assertEqual(len(pending.air_positions), 128)
        self.assertEqual(pending.air_positions, positions[:128])

    def test_targeted_information_request_queries_only_still_unknown_cells(self):
        knowledge = {
            (0, 65, 0): CellKnowledge.AIR,
            (0, 66, 0): CellKnowledge.UNKNOWN,
            (1, 66, 0): CellKnowledge.BLOCK,
        }
        frame = SimpleNamespace(world=SimpleNamespace(
            cell=lambda position: SimpleNamespace(knowledge=knowledge[position]),
        ))

        request = _targeted_information_request(
            frame,
            ((0, 65, 0), (0, 66, 0), (1, 66, 0), (0, 66, 0)),
        )

        self.assertEqual(
            request,
            ObservationRequestV3("navigation_v1", ((0, 66, 0),)),
        )

    def test_information_vantage_is_a_distant_known_support(self):
        supports = ((0, 63, 0), (0, 63, 2), (2, 63, 0), (-2, 63, 0))

        actual = _observation_vantage((0, 65, 0), supports)

        self.assertIn(actual, supports)
        self.assertGreaterEqual(
            (actual[0] - .0) ** 2 + (actual[2] - .0) ** 2,
            4.0,
        )

    def test_information_vantage_uses_nearby_rim_for_a_cell_below_ground(self):
        supports = ((0, 63, 0), (0, 63, 2), (3, 63, 0))

        actual = _observation_vantage((1, 62, 0), supports)

        self.assertEqual(actual, (0, 63, 0))

    def test_live_probe_reuses_one_planner_and_motion_worker(self):
        planner = unittest.mock.MagicMock()
        motion = unittest.mock.MagicMock()
        runtime = unittest.mock.MagicMock()
        runtime.state.value = "ready"
        runtime.cleanup_failures = ()
        source = object()
        runtime.register_ordered_source.return_value = source
        planner.__enter__.return_value = planner
        motion.__enter__.return_value = motion
        expected = object()
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(probe, "PlannerWorker", return_value=planner, create=True),
                patch.object(probe, "MotionSolverWorker", return_value=motion, create=True),
                patch.object(
                    probe,
                    "_run_b10_gap_solver_runtime",
                    return_value=expected,
                    create=True,
                ) as delegated,
            ):
                actual = probe.run_b10_gap_solver_runtime(
                    runtime, object(), "episode", Path(temporary), 1,
                    lambda *_: None, lambda *_: None,
                )

        self.assertIs(actual, expected)
        runtime.register_ordered_source.assert_called_once_with("b10-gap-solver")
        runtime.unregister_ordered_source.assert_called_once_with(source)
        delegated.assert_called_once()
        self.assertIs(delegated.call_args.kwargs["planner_worker"], planner)
        self.assertIs(delegated.call_args.kwargs["motion_worker"], motion)
        self.assertIs(delegated.call_args.kwargs["manual_source"], source)

    def test_live_probe_unregisters_manual_source_once_when_inner_probe_fails(self):
        planner = unittest.mock.MagicMock()
        motion = unittest.mock.MagicMock()
        runtime = unittest.mock.MagicMock()
        runtime.state.value = "ready"
        runtime.cleanup_failures = ()
        source = object()
        runtime.register_ordered_source.return_value = source
        planner.__enter__.return_value = planner
        motion.__enter__.return_value = motion
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(probe, "PlannerWorker", return_value=planner),
                patch.object(probe, "MotionSolverWorker", return_value=motion),
                patch.object(
                    probe,
                    "_run_b10_gap_solver_runtime",
                    side_effect=RuntimeError("fixture failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "fixture failure"),
            ):
                probe.run_b10_gap_solver_runtime(
                    runtime, object(), "episode", Path(temporary), 1,
                    lambda *_: None, lambda *_: None,
                )

        runtime.unregister_ordered_source.assert_called_once_with(source)

    def test_failed_runtime_preserves_primary_and_defers_source_to_runtime_close(self):
        planner = unittest.mock.MagicMock()
        motion = unittest.mock.MagicMock()
        runtime = unittest.mock.MagicMock()
        runtime.state.value = "failed"
        source = unittest.mock.MagicMock(source_id="ordered/b10/0/1")
        runtime.register_ordered_source.return_value = source
        planner.__enter__.return_value = planner
        motion.__enter__.return_value = motion
        lifecycle = {}
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(probe, "PlannerWorker", return_value=planner),
                patch.object(probe, "MotionSolverWorker", return_value=motion),
                patch.object(
                    probe,
                    "_run_b10_gap_solver_runtime",
                    side_effect=TimeoutError("original deadline"),
                ),
                self.assertRaisesRegex(TimeoutError, "original deadline"),
            ):
                probe.run_b10_gap_solver_runtime(
                    runtime, object(), "episode", Path(temporary), 1,
                    lambda *_: None, lambda *_: None,
                    lifecycle_report=lifecycle,
                )

        runtime.unregister_ordered_source.assert_not_called()
        self.assertEqual(lifecycle["ordered_source_release"]["status"],
                         "pending_runtime_close")
        self.assertEqual(lifecycle["cleanup_failures"], [])

        probe.finalize_b10_source_release(lifecycle, runtime_state="closed")
        self.assertEqual(lifecycle["ordered_source_release"]["status"],
                         "cleared_on_runtime_close")

    def test_unregister_failure_is_secondary_and_does_not_replace_primary(self):
        planner = unittest.mock.MagicMock()
        motion = unittest.mock.MagicMock()
        runtime = unittest.mock.MagicMock()
        runtime.state.value = "ready"
        source = unittest.mock.MagicMock(source_id="ordered/b10/0/1")
        runtime.register_ordered_source.return_value = source
        runtime.unregister_ordered_source.side_effect = OSError("cleanup broke")
        planner.__enter__.return_value = planner
        motion.__enter__.return_value = motion
        lifecycle = {}
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(probe, "PlannerWorker", return_value=planner),
                patch.object(probe, "MotionSolverWorker", return_value=motion),
                patch.object(
                    probe,
                    "_run_b10_gap_solver_runtime",
                    side_effect=RuntimeError("primary broke"),
                ),
                self.assertRaisesRegex(RuntimeError, "primary broke"),
            ):
                probe.run_b10_gap_solver_runtime(
                    runtime, object(), "episode", Path(temporary), 1,
                    lambda *_: None, lambda *_: None,
                    lifecycle_report=lifecycle,
                )

        self.assertEqual(lifecycle["ordered_source_release"]["status"],
                         "pending_runtime_close")
        self.assertEqual(
            lifecycle["cleanup_failures"],
            [{"stage": "ordered_source_unregister", "type": "OSError",
              "message": "cleanup broke"}],
        )

    def test_multiple_cleanup_failures_remain_secondary_to_driver_failure(self):
        planner = unittest.mock.MagicMock()
        motion = unittest.mock.MagicMock()
        runtime = unittest.mock.MagicMock()
        runtime.state.value = "ready"
        source = unittest.mock.MagicMock(source_id="ordered/b10/0/1")
        runtime.register_ordered_source.return_value = source
        runtime.unregister_ordered_source.side_effect = OSError("unregister")
        planner.__enter__.return_value = planner
        motion.__enter__.return_value = motion
        planner.__exit__.side_effect = RuntimeError("planner close")
        motion.__exit__.side_effect = RuntimeError("motion close")
        lifecycle = {}
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(probe, "PlannerWorker", return_value=planner),
                patch.object(probe, "MotionSolverWorker", return_value=motion),
                patch.object(
                    probe, "_run_b10_gap_solver_runtime",
                    side_effect=ValueError("driver primary"),
                ),
                self.assertRaisesRegex(ValueError, "driver primary"),
            ):
                probe.run_b10_gap_solver_runtime(
                    runtime, object(), "episode", Path(temporary), 1,
                    lambda *_: None, lambda *_: None,
                    lifecycle_report=lifecycle,
                )

        self.assertEqual(
            [item["stage"] for item in lifecycle["cleanup_failures"]],
            ["ordered_source_unregister", "motion_worker_close",
             "planner_worker_close"],
        )

    def test_cleanup_only_failure_is_reported_without_discarding_result(self):
        planner = unittest.mock.MagicMock()
        motion = unittest.mock.MagicMock()
        runtime = unittest.mock.MagicMock()
        runtime.state.value = "ready"
        source = unittest.mock.MagicMock(source_id="ordered/b10/0/1")
        runtime.register_ordered_source.return_value = source
        runtime.unregister_ordered_source.side_effect = OSError("cleanup only")
        planner.__enter__.return_value = planner
        motion.__enter__.return_value = motion
        expected = object()
        lifecycle = {}
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(probe, "PlannerWorker", return_value=planner),
                patch.object(probe, "MotionSolverWorker", return_value=motion),
                patch.object(probe, "_run_b10_gap_solver_runtime",
                             return_value=expected),
            ):
                actual = probe.run_b10_gap_solver_runtime(
                    runtime, object(), "episode", Path(temporary), 1,
                    lambda *_: None, lambda *_: None,
                    lifecycle_report=lifecycle,
                )

        self.assertIs(actual, expected)
        self.assertEqual(len(lifecycle["cleanup_failures"]), 1)
        self.assertEqual(lifecycle["cleanup_failures"][0]["stage"],
                         "ordered_source_unregister")

    def test_nonclosed_runtime_cannot_claim_close_cleared_the_source(self):
        lifecycle = {
            "ordered_source_release": {
                "status": "pending_runtime_close",
                "source_id": "ordered/b10/0/1",
            },
            "cleanup_failures": [{
                "stage": "ordered_source_unregister",
                "type": "OSError",
                "message": "unregister broke",
            }],
        }

        probe.finalize_b10_source_release(lifecycle, runtime_state="failed")

        self.assertEqual(lifecycle["ordered_source_release"]["status"],
                         "not_released")
        self.assertEqual(len(lifecycle["cleanup_failures"]), 1)

    def test_successful_probe_unregister_failure_is_closed_in_formal_owner_path(self):
        from scripts.probe_fabric_deployment_observation import _close_client
        planner = unittest.mock.MagicMock()
        motion = unittest.mock.MagicMock()
        runtime = unittest.mock.MagicMock()
        runtime.state.value = "ready"
        runtime.cleanup_failures = ()
        source = unittest.mock.MagicMock(source_id="ordered/b10/0/1")
        runtime.register_ordered_source.return_value = source
        runtime.unregister_ordered_source.side_effect = OSError("unregister")
        planner.__enter__.return_value = planner
        motion.__enter__.return_value = motion
        expected = object()
        lifecycle = {}
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(probe, "PlannerWorker", return_value=planner),
                patch.object(probe, "MotionSolverWorker", return_value=motion),
                patch.object(probe, "_run_b10_gap_solver_runtime",
                             return_value=expected),
            ):
                self.assertIs(probe.run_b10_gap_solver_runtime(
                    runtime, object(), "episode", Path(temporary), 1,
                    lambda *_: None, lambda *_: None,
                    lifecycle_report=lifecycle,
                ), expected)
        runtime.close.side_effect = lambda: setattr(runtime.state, "value", "closed")
        transport = unittest.mock.MagicMock()

        _, close_failures = _close_client(
            runtime, None, transport, None, None,
            b10_lifecycle=lifecycle,
        )

        self.assertEqual(close_failures, [])
        self.assertEqual(lifecycle["ordered_source_release"]["status"],
                         "cleared_on_runtime_close")
        self.assertEqual(lifecycle["cleanup_failures"][0]["stage"],
                         "ordered_source_unregister")
        runtime.has_ordered_source.assert_not_called()

    def test_failed_probe_primary_survives_formal_owner_close(self):
        from scripts.probe_fabric_deployment_observation import _close_client
        planner = unittest.mock.MagicMock()
        motion = unittest.mock.MagicMock()
        runtime = unittest.mock.MagicMock()
        runtime.state.value = "ready"
        runtime.cleanup_failures = ()
        source = unittest.mock.MagicMock(source_id="ordered/b10/0/1")
        runtime.register_ordered_source.return_value = source
        planner.__enter__.return_value = planner
        motion.__enter__.return_value = motion
        lifecycle = {}

        def fail(*_args, **_kwargs):
            runtime.state.value = "failed"
            raise TimeoutError("primary deadline")

        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(probe, "PlannerWorker", return_value=planner),
                patch.object(probe, "MotionSolverWorker", return_value=motion),
                patch.object(probe, "_run_b10_gap_solver_runtime",
                             side_effect=fail),
                self.assertRaisesRegex(TimeoutError, "primary deadline"),
            ):
                probe.run_b10_gap_solver_runtime(
                    runtime, object(), "episode", Path(temporary), 1,
                    lambda *_: None, lambda *_: None,
                    lifecycle_report=lifecycle,
                )
        runtime.close.side_effect = lambda: setattr(runtime.state, "value", "closed")

        _close_client(
            runtime, None, unittest.mock.MagicMock(), None, None,
            b10_lifecycle=lifecycle,
        )

        self.assertEqual(lifecycle["ordered_source_release"]["status"],
                         "cleared_on_runtime_close")
        runtime.unregister_ordered_source.assert_not_called()
        runtime.has_ordered_source.assert_not_called()

    def test_manual_probe_uses_runtime_ledger_for_prior_and_current_samples(self):
        source = inspect.getsource(probe._run_b10_gap_solver_runtime)
        self.assertIn("ledger = _runtime_input_ledger(runtime)", source)
        self.assertNotIn("InputApplicationLedger(max_records=64)", source)
        self.assertNotIn("_observe_manual_executor_receipt", source)
        self.assertIn("input_execution_window=execution_window", source)

        from tests.motion_nav.test_b10_motion_candidate import solved_candidate

        anchor, reusable = solved_candidate()
        admission = MotionCandidateAdmitter().admit(
            reusable,
            anchor,
            planning_request_id="request-1",
            planning_generation=4,
            goal_id="goal-1",
            goal_revision=2,
            route_id="route-1",
            route_revision=3,
            action_index=1,
            candidate_revision=5,
            damage_budget=TaskDamageBudget(),
            intended_start_tick=11,
            changed_cells=(),
        )
        self.assertIs(admission.status, MotionCandidateStatus.ACCEPTED)
        executor = VerifiedMotionExecutor()
        executor.start(admission.candidate)
        ledger = InputApplicationLedger(max_records=64)
        ledger.establish_baseline(
            anchor.session,
            "episode",
            movement_tick_id=anchor.movement_tick_id,
        )

        first = executor.decide(anchor, ledger)
        previous_action = ActionSnapshotV1(
            "episode", 69, anchor.observation_sequence_id, 1_000_000,
            movement=MovementV1(), valid_for_ticks=1,
        )
        current_action = ActionSnapshotV1(
            "episode", 70, anchor.observation_sequence_id, 1_000_000,
            movement=first.movement, valid_for_ticks=1,
        )
        ledger.submit(
            anchor.session, previous_action,
            requested_first_tick=11,
        )
        ledger.submit(
            anchor.session, current_action,
            requested_first_tick=first.expected_movement_tick,
            latest_allowed_first_tick=first.latest_movement_tick,
        )
        executor.register_submission(
            first.command_index,
            control_sequence=70,
            requested_movement_tick=first.expected_movement_tick,
            requested_latest_movement_tick=first.latest_movement_tick,
        )
        receipt = self._receipt(
            70,
            (
                ClientInputApplicationV1(
                    "mc2p.input-application.v1",
                    11,
                    "episode",
                    69,
                    111,
                    "lease_exhausted",
                    0.0,
                    0.0,
                    False,
                    False,
                    False,
                ),
                ClientInputApplicationV1(
                    "mc2p.input-application.v1",
                    12,
                    "episode",
                    70,
                    112,
                    "leased",
                    float(first.movement.forward),
                    float(first.movement.strafe),
                    first.movement.jump,
                    first.movement.sneak,
                    first.movement.sprint,
                ),
            ),
        )

        ledger.observe_receipt(receipt)
        delayed = admission.candidate.proof.start_variant(12)
        self.assertIsNotNone(delayed)
        observed = replace(
            anchor,
            observation_sequence_id=anchor.observation_sequence_id + 1,
            movement_tick_id=12,
            physics_state=delayed.trajectory[1],
        )
        continued = executor.decide(observed, ledger)

        self.assertEqual(ledger.sample(11), receipt.input_applications[0])
        self.assertEqual(ledger.sample(12), receipt.input_applications[1])
        self.assertIs(ledger.record(70).status, InputApplicationStatus.APPLIED)
        self.assertIs(continued.state, VerifiedMotionExecutorState.RUNNING)
        self.assertEqual(continued.command_index, 1)
        self.assertEqual(continued.reason, "submit_verified_command")

    def test_uses_runtime_owned_navigation_frame_without_second_ingest(self):
        expected = SimpleNamespace(body=SimpleNamespace(sequence_id=7))
        adapter = _Adapter(expected)
        runtime = SimpleNamespace(
            navigation_observation_adapter=adapter,
            observation=SimpleNamespace(sequence_id=7),
        )

        actual = _runtime_navigation_frame(runtime)

        self.assertIs(actual, expected)
        self.assertEqual(adapter.ingest_calls, 0)

    def test_coordinator_uses_runtime_owned_input_ledger(self):
        expected = InputApplicationLedger()
        runtime = SimpleNamespace(input_ledger=expected)

        self.assertIs(_runtime_input_ledger(runtime), expected)

    def test_neutral_landing_responsibility_has_no_verified_submission_window(self):
        decision = SimpleNamespace(
            verified_command_index=None,
            expected_movement_tick=None,
            latest_movement_tick=None,
        )

        self.assertIsNone(_verified_submission_window(decision))

    def test_verified_command_requires_a_complete_submission_window(self):
        decision = SimpleNamespace(
            verified_command_index=3,
            expected_movement_tick=41,
            latest_movement_tick=42,
        )
        self.assertEqual(_verified_submission_window(decision), (3, 41, 42))

        incomplete = SimpleNamespace(
            verified_command_index=3,
            expected_movement_tick=41,
            latest_movement_tick=None,
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete verified command identity"):
            _verified_submission_window(incomplete)

    def test_manual_executor_coasts_without_registering_a_verified_command(self):
        coasting = SimpleNamespace(
            submittable_as_verified_command=False,
            reason="coast_to_verified_landing",
            movement=MovementV1(),
            command_index=20,
            expected_movement_tick=51,
            latest_movement_tick=51,
        )

        self.assertIsNone(_manual_executor_submission_window(coasting))

    def test_manual_executor_rejects_unverified_active_movement(self):
        invalid = SimpleNamespace(
            submittable_as_verified_command=False,
            reason="unexpected",
            movement=MovementV1(forward=1),
            command_index=2,
            expected_movement_tick=33,
            latest_movement_tick=33,
        )

        with self.assertRaisesRegex(RuntimeError, "unverified active movement"):
            _manual_executor_submission_window(invalid)

    def test_input_timing_keeps_actual_tick_and_window_distance(self):
        applications = (
            SimpleNamespace(movement_tick_id=13, state="leased"),
        )

        diagnostics = _input_window_diagnostics(applications, (0, 11, 12))

        self.assertEqual(diagnostics, {
            "verified_command_index": 0,
            "expected_movement_tick": 11,
            "latest_movement_tick": 12,
            "actual_movement_ticks": [13],
            "application_states": ["leased"],
            "actual_minus_expected_ticks": 2,
            "actual_minus_latest_ticks": 1,
            "remaining_to_latest_ticks": -1,
            "remaining_to_latest_milliseconds": -50.0,
        })

        neutral = _input_window_diagnostics(applications, None)
        self.assertIsNone(neutral["verified_command_index"])
        self.assertIsNone(neutral["actual_minus_latest_ticks"])
        self.assertEqual(neutral["actual_movement_ticks"], [13])
        self.assertIsNone(neutral["remaining_to_latest_ticks"])
        self.assertIsNone(neutral["remaining_to_latest_milliseconds"])


if __name__ == "__main__":
    unittest.main()
