from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.motion_nav.online_motion import InputApplicationLedger

from scripts import b10_gap_solver_runtime as probe
from scripts.b10_gap_solver_runtime import (
    _input_window_diagnostics,
    _runtime_input_ledger,
    _runtime_navigation_frame,
    _verified_submission_window,
)


class _Adapter:
    def __init__(self, frame) -> None:
        self.latest_frame = frame
        self.ingest_calls = 0

    def ingest(self, _observation):
        self.ingest_calls += 1
        raise AssertionError("B10 must not ingest a Runtime-owned observation twice")


class B10RuntimeProbeTests(unittest.TestCase):
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
