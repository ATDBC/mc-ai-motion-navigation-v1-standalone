from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
import tempfile
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
