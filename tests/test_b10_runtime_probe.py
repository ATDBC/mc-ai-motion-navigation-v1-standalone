from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.motion_nav.online_motion import InputApplicationLedger

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
        planner.__enter__.return_value = planner
        motion.__enter__.return_value = motion
        expected = object()
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
                object(), object(), "episode", object(), 1,
                lambda *_: None, lambda *_: None,
            )

        self.assertIs(actual, expected)
        delegated.assert_called_once()
        self.assertIs(delegated.call_args.kwargs["planner_worker"], planner)
        self.assertIs(delegated.call_args.kwargs["motion_worker"], motion)

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
        })

        neutral = _input_window_diagnostics(applications, None)
        self.assertIsNone(neutral["verified_command_index"])
        self.assertIsNone(neutral["actual_minus_latest_ticks"])
        self.assertEqual(neutral["actual_movement_ticks"], [13])


if __name__ == "__main__":
    unittest.main()
