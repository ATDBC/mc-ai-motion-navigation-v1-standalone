"""D122-3 formal-chain timing projection tests."""
from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementTickWindowV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from scripts.d121_handoff_execution_probe import ExecutionCase
from tests.sim.backend import CalculatorBackend
from scripts.d122_formal_timing_probe import POLICIES, run_case, run_matrix
from scripts import d120_momentum_entry_calibration as d120


def _timed_backend(*args, **kwargs):
    return CalculatorBackend(*args, **kwargs)


class D122FormalTimingProjectionTests(unittest.TestCase):
    def test_never_transport_failure_closes_the_formal_driver_lifecycle(self):
        row = run_case("never_delivered", direction_index=0)
        self.assertIsNone(row["error"])
        self.assertTrue(row["source_released"])
        self.assertFalse(row["prepared_frame"])
        self.assertIn(row["driver_state"], {"failed", "stopped", "cancelled"})

    def test_formal_reachable_reject_and_cancel_are_bounded(self):
        for policy in ("explicit_reject", "cancel"):
            with self.subTest(policy=policy):
                row = run_case(policy, direction_index=0)
                self.assertTrue(row["passed"], row)
                self.assertIsNone(row["error"])
                self.assertTrue(row["source_released"])
                self.assertFalse(row["prepared_frame"])
                self.assertFalse(any(sample["jump"] for sample in row["successor_samples"]))

    def test_formal_producer_first_creates_the_successor_at_l(self):
        row = run_case("timely_l1", direction_index=0)
        successor = row["successor_action"]
        self.assertEqual(successor["first_effect_window"], {
            "earliest_tick": 27,
            "latest_tick": 27,
        })
        successor_sequence = successor["request_sequence_id"]
        successor_frames = [frame for frame in row["frames"]
                            if frame["sequence"] == successor_sequence]
        self.assertEqual(len(successor_frames), 1)
        self.assertEqual(successor_frames[0]["source_tick"], 26)
        self.assertFalse(any(frame["source_tick"] < 26
                             and frame["sequence"] == successor_sequence
                             for frame in row["frames"]))

    def test_prepared_driver_execution_discards_on_runtime_exception(self):
        case = ExecutionCase(action_timing_v2=True)
        try:
            proposals = case.driver.prepare_proposals(
                case.clock[0] + case.step_window_ns,
            )
            with patch.object(
                case.runtime, "control_frame",
                side_effect=RuntimeError("injected transport failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected transport"):
                    case.driver.advance_prepared(
                        BehaviorProfileV0(), proposals=proposals,
                    )
            self.assertFalse(case.driver.has_prepared_frame)
        finally:
            case.close()

    def test_parent_default_is_derived_from_the_admitted_exit_fallback(self):
        with patch(
            "scripts.d121_handoff_execution_probe.CalculatorBackend",
            _timed_backend,
        ):
            case = ExecutionCase(
                step_window_ns=1_000_000_000,
                action_timing_v2=True,
            )
        try:
            case.prepare_successor()
            parent = None
            successor = None
            for _ in range(40):
                source_tick = case.backend.movement_tick
                submitted_at = case.clock[0]
                result = case.tick()
                if source_tick == 25:
                    parent = result.decision.action
                    parent_submitted_at = submitted_at
                elif source_tick == 26:
                    successor = result.decision.action
                    break
            self.assertIsNotNone(parent)
            self.assertIsNotNone(successor)
            tail = case.proof.fallback_tails[-1]
            self.assertEqual(tail.boundary_index, len(case.proof.commands))
            self.assertEqual(tail.recovery_boundary_index, len(case.proof.commands))
            self.assertIsNotNone(parent.safe_default)
            self.assertEqual(parent.safe_default.activation_tick, 27)
            self.assertEqual(parent.safe_default.valid_for_ticks, len(tail.inputs))
            self.assertGreaterEqual(parent.safe_default.valid_for_ticks, 8)
            self.assertEqual(parent.safe_default.movement, MovementV1(sneak=True))
            self.assertEqual(parent.first_effect_window.earliest_tick, 26)
            self.assertEqual(parent.first_effect_window.latest_tick, 26)
            self.assertEqual(successor.first_effect_window.earliest_tick, 27)
            self.assertEqual(successor.first_effect_window.latest_tick, 27)
            self.assertIsNone(successor.safe_default)
            # One incumbent tick plus the complete proof-selected fallback.
            self.assertGreaterEqual(
                parent.deadline_monotonic_ns - parent_submitted_at,
                (1 + len(tail.inputs)) * 50_000_000,
            )
        finally:
            case.close()

    def test_formal_runtime_reachable_state_matrix(self):
        result = run_matrix()
        self.assertEqual(result["result"], "PASS", result)
        self.assertEqual(len(result["runs"]), 4 * len(POLICIES))
        self.assertTrue(result["performance"]["passed"], result["performance"])
        self.assertEqual(
            {(row["direction_index"], row["policy"])
             for row in result["runs"]},
            {(direction, policy) for direction in range(4)
             for policy in POLICIES},
        )
        timely = [row for row in result["runs"]
                  if row["policy"] == "timely_l1"]
        bounded = [row for row in result["runs"]
                   if row["policy"] != "timely_l1"]
        fallback = [row for row in bounded if row["policy"] != "cancel"]
        for row in timely:
            self.assertEqual(len(row["successor_samples"]), 1)
            self.assertEqual(row["successor_samples"][0]["movement_tick_id"], 27)
        for row in bounded:
            self.assertEqual(len(row["successor_samples"]), 0)
            self.assertTrue(row["final_body"]["on_ground"])
            self.assertEqual(row["final_body"]["damage"], 0.0)
            self.assertTrue(row["source_released"])
            self.assertFalse(row["prepared_frame"])
            self.assertIsNone(row["error"])
        for row in fallback:
            self.assertEqual(len(row["default_samples"]), 9)

    def test_v1_component_keeps_the_legacy_unwindowed_projection(self):
        case = ExecutionCase()
        try:
            self.assertFalse(case.runtime.input_ledger.action_timing_v2)
            case.prepare_successor()
            internal_windows = []
            for _ in range(26):
                result = case.tick()
                self.assertIsNone(result.report.failure)
                internal_windows.append(result.decision.movement_tick_window)
            self.assertTrue(any(window is not None for window in internal_windows))
            self.assertTrue(case.backend.actions)
            for action in case.backend.actions:
                self.assertIsNone(action.first_effect_window)
                self.assertIsNone(action.safe_default)
        finally:
            case.close()

    def test_v2_owner_deadline_requires_margin_beyond_the_default(self):
        with patch(
            "scripts.d121_handoff_execution_probe.CalculatorBackend",
            _timed_backend,
        ):
            case = ExecutionCase(action_timing_v2=True)
        try:
            case.prepare_successor()
            for _ in range(26):
                case.tick()
            self.assertEqual(case.driver.state.value, "stopping")
            self.assertEqual(
                case.driver.reason, "navigation_internal_contract_failure",
            )
        finally:
            case.close()

    def test_all_d120_entries_have_at_least_eight_proved_sneak_ticks(self):
        manifest = d120.build_manifest()
        rows = []
        for direction in range(4):
            coverage = d120.scan_direction(manifest, direction)
            rows.extend(point["suffixes"]["fallback-sneak"]
                        for point in coverage["samples"])
        self.assertEqual(len(rows), 972)
        self.assertTrue(all(row["passed"] for row in rows))
        self.assertEqual({row["steps"] for row in rows}, {8})

    def test_widening_successor_to_l_plus_two_is_detected(self):
        widened = MovementTickWindowV1(27, 28)
        with patch(
            "mc2p.motion_nav.navigation_session.decision_input_window",
            return_value=widened,
        ):
            result = run_case("late_l2")
        self.assertFalse(result["passed"])

    def test_replacing_the_proved_default_with_neutral_is_detected(self):
        from mc2p.motion_nav.motion_candidate import VerifiedMotionExecutor
        original = VerifiedMotionExecutor.prearmed_exit_default

        def neutral(executor, decision):
            value = original(executor, decision)
            return None if value is None else replace(
                value, movement=MovementV1(),
            )

        with patch.object(
            VerifiedMotionExecutor, "prearmed_exit_default", neutral,
        ):
            result = run_case("late_l2")
        self.assertFalse(result["passed"])

    def test_fallback_armed_and_window_pending_are_not_applications(self):
        from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
        from mc2p.contracts.action_v1 import ActionSnapshotV1, SafeDefaultV1
        from mc2p.motion_nav.online_motion import (
            InputApplicationLedger, input_application_has_finished,
        )
        from mc2p.motion_nav.world_model import WorldSessionId
        from tests.test_action_receipt import receipt_value

        parent = ActionSnapshotV1(
            "ep", 25, 0, 2_000_000_000,
            safe_default=SafeDefaultV1(
                27, MovementV1(sneak=True), 9, 450_000_000,
            ),
        )
        successor = ActionSnapshotV1(
            "ep", 26, 1, 2_000_000_000,
            movement=MovementV1(jump=True),
            first_effect_window=MovementTickWindowV1(27, 27),
        )
        ledger = InputApplicationLedger(action_timing_v2=True)
        session = WorldSessionId("w")
        ledger.submit(session, parent, requested_first_tick=26)
        ledger.submit(session, successor, requested_first_tick=27)
        receipt = behavior_receipt_from_mapping(dict(
            receipt_value(
                schema_version="mc2p.client_action_receipt.v4",
                episode_id="ep", request_sequence_id=26,
                input_samples=26, leased_input_samples=1,
            ),
            input_applications=[], dropped_input_samples=0,
            oldest_retained_input_tick=26,
            timing_events=[
                dict(status="fallback_armed", movement_tick_id=26,
                     episode_id="ep", request_sequence_id=25),
                dict(status="window_pending", movement_tick_id=26,
                     episode_id="ep", request_sequence_id=26),
            ],
        ))
        ledger.observe_receipt(receipt)
        self.assertFalse(input_application_has_finished(ledger.record(25)))
        self.assertFalse(input_application_has_finished(ledger.record(26)))
        self.assertEqual(ledger.record(25).applied_ticks, ())
        self.assertEqual(ledger.record(26).applied_ticks, ())


if __name__ == "__main__":
    unittest.main()
