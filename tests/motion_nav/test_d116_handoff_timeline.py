"""D116-0 read-only attribution through the formal navigation call chain."""
from __future__ import annotations

import unittest

from scripts.d116_handoff_timeline import run_representative_timeline


class D116HandoffTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.evidence = run_representative_timeline()
        cls.red_evidence = run_representative_timeline(
            dependency_gate="legacy_full_cell_fact",
        )
        cls.rows = {
            row["movement_tick"]: row
            for row in cls.evidence["timeline"]
        }
        cls.red_rows = {
            row["movement_tick"]: row
            for row in cls.red_evidence["timeline"]
        }

    def test_d116_zero_red_is_an_explicit_legacy_gate_replay(self):
        row = self.red_rows[20]
        self.assertEqual(
            self.red_evidence["phase"],
            "D116-0-red-replay",
        )
        self.assertEqual(
            self.red_evidence["dependency_gate"],
            "legacy_full_cell_fact",
        )
        self.assertIn("original uncommitted D116-0 file was overwritten",
                      self.red_evidence["evidence_provenance"])
        self.assertEqual(row["prepared_entry_status"], "dependency_changed")
        self.assertFalse(row["dependency_facts_equal"])
        self.assertTrue(row["dependency_payloads_equal"])
        self.assertTrue(row["dependency_versions_equal"])
        self.assertEqual(row["supervisor_status"], "rejected")
        self.assertFalse(self.red_evidence["second_jump_started"])
        self.assertFalse(
            self.red_evidence["milestones"]["tick21_second_jump_applied"],
        )
        self.assertEqual(
            [(code, detail) for _tick, code, detail in self.red_evidence["violations"]],
            [(code, detail) for _tick, code, detail in self.evidence["violations"]],
        )

    def test_representative_uses_the_formal_chain_and_applies_second_jump(self):
        self.assertEqual(self.evidence["dependency_gate"], "semantic")
        self.assertEqual(self.evidence["phase"], "D116-1")
        self.assertEqual(
            self.evidence["formal_chain"],
            [
                "PlayerRuntimeV1",
                "RuntimeNavigationDriver",
                "NavigationSession",
                "ExecutionSupervisor",
                "ActionRouteExecutor",
                "MotionRouteCoordinator",
                "CalculatorBackend",
            ],
        )
        self.assertEqual(
            self.evidence["planned_actions"],
            ["WalkSegment", "JumpGapSegment", "JumpGapSegment"],
        )
        self.assertEqual(self.evidence["damage"], 0.0)
        self.assertTrue(self.evidence["second_jump_started"])
        self.assertEqual(self.evidence["milestones"]["owner_gap_ticks"], [])

    def test_tick_twenty_publishes_boundary_before_session_approval(self):
        row = self.rows[20]
        self.assertEqual(row["controller_state_before"], "complete")
        self.assertTrue(row["executor_advance_called"])
        self.assertEqual(row["boundary_before_advance"], None)
        self.assertEqual(row["boundary_after_advance"], 2)
        self.assertEqual(row["session_route_reason"], "prepared_action_boundary")
        self.assertTrue(row["prepared_boundary_reached"])
        self.assertEqual(row["supervisor_status"], "approved")
        order = row["event_order"]
        self.assertLess(
            order.index("executor_advance_exit"),
            order.index("session_proposal_enter"),
        )
        self.assertLess(
            order.index("prepared_action_staged"),
            order.index("supervisor_approval"),
        )
        self.assertEqual(
            row["runtime_selected_movement"],
            {"forward": 1, "strafe": 0, "jump": True,
             "sneak": False, "sprint": True},
        )
        self.assertEqual(row["runtime_effect_tick"], 21)
        self.assertEqual(row["runtime_selected_groups"].count("movement"), 1)

    def test_tick_twenty_accepts_refreshed_dependency_stamps(self):
        row = self.rows[20]
        self.assertTrue(row["body_matches_prepared_entry"])
        self.assertTrue(row["ledger_is_current"])
        self.assertTrue(row["landing_observed"])
        self.assertTrue(row["requested_window_allows_next_tick"])
        self.assertEqual(row["prepared_entry_status"], "ready_match")
        self.assertFalse(row["dependency_facts_equal"])
        self.assertTrue(row["dependency_payloads_equal"])
        self.assertTrue(row["dependency_versions_equal"])
        self.assertGreater(row["stamp_only_dependency_differences"], 0)
        self.assertIsNone(row["supervisor_reason"])

    def test_tick_twenty_one_applies_one_owned_second_jump_input(self):
        row = self.rows[21]
        self.assertEqual(
            row["applied_movement_at_observation"],
            {"forward": 1, "strafe": 0, "jump": True,
             "sneak": False, "sprint": True},
        )
        self.assertEqual(row["prepared_application_status"], "applied")
        self.assertFalse(row["owner_gap"])
        self.assertTrue(row["active_and_applied_owner_match"])
        self.assertEqual(self.evidence["damage"], 0.0)

    def test_new_tracker_and_async_lifecycle_blockers_are_typed_and_bounded(self):
        row = self.rows[21]
        self.assertEqual(row["incumbent_validation"], "stop")
        self.assertEqual(row["incumbent_validation_reason"], "route_identity_changed")
        self.assertTrue(row["route_cancel_after_validation"])
        self.assertEqual(row["route_stop_cause_after_validation"], "dependency_changed")
        self.assertEqual(row["prepared_controller_state_after"], "recovering")
        self.assertEqual(self.evidence["outcome"], "executing")
        self.assertEqual(self.evidence["reason"], "BODY_RESPONSIBILITY_UNRESOLVED")
        self.assertEqual(len(self.evidence["violations"]), 1)
        violation_tick, violation_code, violation_detail = self.evidence["violations"][0]
        self.assertLess(violation_tick, 20)
        self.assertEqual(violation_code, "I19")
        self.assertEqual(
            violation_detail,
            "result applied without current identity, facts or time",
        )
        self.assertEqual(
            self.evidence["stop"]["async_monitor"]["movement_tick"],
            violation_tick,
        )
        self.assertEqual(
            self.evidence["attribution"],
            "semantic_dependency_handoff_passed_then_route_tracker_identity_"
            "and_async_apply_invariants_blocked",
        )
        self.assertEqual(self.evidence["production_behavior_changed"], True)


if __name__ == "__main__":
    unittest.main()
