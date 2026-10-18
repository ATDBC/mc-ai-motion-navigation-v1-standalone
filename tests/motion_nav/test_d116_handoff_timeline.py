"""D116-0 read-only attribution through the formal navigation call chain."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from scripts.d116_handoff_timeline import (
    _prepared_lifecycle_summary,
    run_representative_timeline,
)


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

    @staticmethod
    def _row(evidence, predicate):
        return next(row for row in evidence["timeline"] if predicate(row))

    def test_d116_zero_red_is_an_explicit_legacy_gate_replay(self):
        row = self._row(
            self.red_evidence,
            lambda item: item.get("session_route_reason")
                == "prepared_action_boundary",
        )
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
            self.red_evidence["milestones"]["second_jump_applied"],
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

    def test_boundary_is_published_before_session_approval(self):
        row = self._row(
            self.evidence,
            lambda item: item.get("session_route_reason")
                == "prepared_action_boundary",
        )
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
        self.assertGreater(row["runtime_effect_tick"], row["movement_tick"])
        self.assertLessEqual(
            row["runtime_effect_tick"] - row["movement_tick"], 2,
        )
        self.assertEqual(row["runtime_selected_groups"].count("movement"), 1)

    def test_boundary_accepts_refreshed_dependency_stamps(self):
        row = self._row(
            self.evidence,
            lambda item: item.get("prepared_entry_status") == "ready_match",
        )
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

    def test_next_bounded_tick_applies_one_owned_second_jump_input(self):
        row = self._row(
            self.evidence,
            lambda item: item.get("prepared_application_status") == "applied",
        )
        boundary_tick = self.evidence["milestones"]["handoff_boundary_tick"]
        self.assertGreater(row["movement_tick"], boundary_tick)
        self.assertLessEqual(row["movement_tick"] - boundary_tick, 2)
        self.assertEqual(
            row["applied_movement_at_observation"],
            {"forward": 1, "strafe": 0, "jump": True,
             "sneak": False, "sprint": True},
        )
        self.assertEqual(row["prepared_application_status"], "applied")
        self.assertFalse(row["owner_gap"])
        self.assertTrue(row["active_and_applied_owner_match"])
        self.assertEqual(self.evidence["damage"], 0.0)

    def test_apply_lifecycle_tracker_and_numeric_entry_all_continue(self):
        row = self._row(
            self.evidence,
            lambda item: item.get("prepared_application_status") == "applied",
        )
        self.assertEqual(row["incumbent_validation"], "unaffected")
        self.assertEqual(row["incumbent_validation_reason"], "no_intersection")
        self.assertFalse(row["route_cancel_after_validation"])
        self.assertIsNone(row["route_stop_cause_after_validation"])
        self.assertEqual(row["prepared_controller_state_after"], "running")
        self.assertEqual(self.evidence["outcome"], "executing")
        self.assertEqual(self.evidence["reason"], "prepared_stream_command")
        self.assertEqual(self.evidence["violations"], [])
        lifecycle = self.evidence["prepared_apply_lifecycle"]
        self.assertEqual(lifecycle["operations"], ["begin", "apply", "finish"])
        self.assertEqual(
            lifecycle["admission_accepted_monotonic_ns"],
            lifecycle["apply_monotonic_ns"],
        )
        self.assertLessEqual(lifecycle["apply_tick"], lifecycle["finish_tick"])
        self.assertIsNone(
            self.evidence["stop"]["async_monitor"]["movement_tick"],
        )
        self.assertEqual(
            self.evidence["attribution"],
            "semantic_dependency_async_apply_route_tracker_and_numeric_state_"
            "passed",
        )
        self.assertEqual(self.evidence["production_behavior_changed"], True)

    def test_prepared_commit_advances_executor_and_tracker_together(self):
        commit_row = self._row(
            self.evidence,
            lambda item: item.get("action_index_after_prepared_commit")
                is not None,
        )
        application_row = self._row(
            self.evidence,
            lambda item: item.get("prepared_application_status") == "applied",
        )
        self.assertEqual(commit_row.get("action_index_after_prepared_commit"), 2)
        self.assertEqual(
            commit_row.get("tracker_action_index_after_prepared_commit"), 2,
        )
        self.assertNotEqual(
            application_row.get("incumbent_validation_reason"),
            "route_identity_changed",
        )
        self.assertFalse(application_row["route_cancel_after_validation"])

    def test_prepared_lifecycle_binding_ignores_later_same_revision_work(self):
        prepared = "async-work-prepared"
        later = "async-work-later"
        records = [
            (10, {"identity_token": prepared, "record_type": "event",
                  "operation": "begin", "monotonic_ns": 10}),
            (11, {"identity_token": prepared, "record_type": "event",
                  "operation": "apply", "monotonic_ns": 11}),
            (11, {"identity_token": prepared, "record_type": "event",
                  "operation": "finish", "monotonic_ns": 11}),
            (11, {"identity_token": prepared, "record_type": "admission",
                  "disposition": "applied", "accepted_monotonic_ns": 11}),
            # A later ordinary SOLVE deliberately reuses the same visible
            # revision shape. Exact identity, rather than ordering, binds it.
            (12, {"identity_token": later, "record_type": "event",
                  "operation": "begin", "monotonic_ns": 12,
                  "revision": 3, "work_kind": "motion_solve"}),
            (13, {"identity_token": later, "record_type": "event",
                  "operation": "apply", "monotonic_ns": 13,
                  "revision": 3, "work_kind": "motion_solve"}),
            (13, {"identity_token": later, "record_type": "event",
                  "operation": "finish", "monotonic_ns": 13,
                  "revision": 3, "work_kind": "motion_solve"}),
        ]

        summary = _prepared_lifecycle_summary(records, prepared)

        self.assertEqual(summary["identity_token"], prepared)
        self.assertEqual(summary["operations"], ["begin", "apply", "finish"])
        self.assertEqual(summary["admission_accepted_monotonic_ns"], 11)

    def test_formal_runner_falls_back_when_prepared_snapshot_stays_busy(self):
        for mode in ("always_busy", "prepared_always_busy"):
            with self.subTest(mode=mode):
                evidence = run_representative_timeline(
                    max_ticks=80,
                    snapshot_borrow_mode=mode,
                )
                self.assertEqual(evidence["outcome"], "success")
                self.assertEqual(evidence["damage"], 0.0)
                self.assertEqual(evidence["violations"], [])
                if mode == "prepared_always_busy":
                    operations = evidence["prepared_apply_lifecycle"][
                        "operations"
                    ]
                    # The component clock may reach the bounded ordinary
                    # fallback before the receiver starts the prepared job.
                    # If it did start, cancellation must still close it; in
                    # either schedule an unverified result must never apply.
                    self.assertIn(operations, ([], ["begin", "finish"]))
                    self.assertNotIn("apply", operations)
                    if operations:
                        self.assertIsNotNone(
                            evidence["milestones"]["handoff_boundary_tick"],
                        )

    def test_installed_entry_mismatch_expires_and_falls_back(self):
        evidence = run_representative_timeline(
            max_ticks=80,
            force_prepared_entry_mismatch=True,
        )

        self.assertEqual(evidence["outcome"], "success")
        self.assertEqual(evidence["damage"], 0.0)
        self.assertEqual(evidence["violations"], [])
        self.assertEqual(
            evidence["prepared_apply_lifecycle"]["operations"],
            ["begin", "apply", "finish"],
        )

        with patch.object(
                MotionRouteCoordinator,
                "_prepared_requirement_expired",
                return_value=False):
            mutation = run_representative_timeline(
                max_ticks=80,
                force_prepared_entry_mismatch=True,
            )
        self.assertIn(
            "prepared action boundary wait exceeded",
            " ".join(
                detail for _tick, code, detail in mutation["violations"]
                if code == "I23"
            ),
        )

    def test_formal_runner_finishes_after_numeric_representation_match(self):
        evidence = run_representative_timeline(max_ticks=80)
        recovery = [
            row for row in evidence["timeline"]
            if row.get("prepared_controller_state_after") == "recovering"
        ]
        self.assertEqual(recovery, [])
        self.assertEqual(evidence["outcome"], "success")
        self.assertEqual(evidence["reason"], "goal_state_satisfied")
        self.assertEqual(evidence["violations"], [])


class D117SnapshotBusyTimelineTests(unittest.TestCase):
    def test_transient_snapshot_busy_finishes_through_the_formal_chain(self):
        for mode in ("busy_1", "busy_3", "busy_5"):
            with self.subTest(mode=mode):
                evidence = run_representative_timeline(max_ticks=80, snapshot_borrow_mode=mode)
                self.assertEqual(evidence["outcome"], "success")
                self.assertTrue(evidence["milestones"]["second_jump_applied"])
                self.assertEqual(evidence["milestones"]["owner_gap_ticks"], [])
                self.assertEqual(evidence["damage"], 0.0)
                self.assertEqual(evidence["violations"], [])


if __name__ == "__main__":
    unittest.main()
