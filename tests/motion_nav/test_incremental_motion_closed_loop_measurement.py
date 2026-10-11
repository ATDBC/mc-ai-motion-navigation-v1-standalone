import unittest


class IncrementalMotionClosedLoopMeasurementTests(unittest.TestCase):
    def test_runner_is_project_owned_and_does_not_touch_production_chain(self):
        from pathlib import Path
        from scripts import measure_incremental_motion_p3

        source = Path(measure_incremental_motion_p3.__file__).read_text(encoding="utf-8")
        self.assertNotIn("reviews", source)
        self.assertNotIn("mc2p", source)

    def test_runner_can_be_invoked_directly_from_repository_root(self):
        from pathlib import Path
        import subprocess
        import sys

        root = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            [sys.executable, str(root / "scripts/measure_incremental_motion_p3.py"), "--help"],
            cwd=root, capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_frozen_matrix_covers_every_deviation_at_all_three_phases(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop_measurement import matrix_specs

        specs = matrix_specs()
        deviations = {
            "position_0_01", "position_0_03", "yaw_2deg", "yaw_5deg",
            "unrelated_world", "dependency_changed", "dependency_unknown",
            "goal_revision", "cancel", "late_application", "missing_application",
        }
        phases = {"pre_commit", "commit_boundary", "post_commit"}
        self.assertEqual({row.injection for row in specs if row.phase is not None}, deviations)
        for deviation in deviations:
            self.assertEqual({row.phase.value for row in specs if row.injection == deviation}, phases)
        self.assertTrue(any(row.case_id == "jump_up_after_turn:A15" for row in specs))
        self.assertTrue(any(row.case_id == "jump_gap_1:A3" and row.injection == "exact"
                            for row in specs))

    def test_small_matrix_reports_bounded_results_and_detects_all_mutations(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop_measurement import run_matrix

        result = run_matrix(rounds=1, compact=True)
        self.assertGreater(result["summary"]["decision_samples"], 0)
        self.assertGreater(result["summary"]["body_responsibility_unresolved"], 0)
        self.assertEqual(result["summary"]["unbounded_results"], 0)
        self.assertEqual(set(result["mutation_checks"]), {
            "unknown_as_air", "one_timing_branch", "no_landing_proof",
            "post_commit_command_swap",
        })
        self.assertTrue(all(not row["gate_passed"] and row["submission_blocked"]
                            for row in result["mutation_checks"].values()))
        self.assertTrue(all(
            row["gate_source"] == "rolling_owner_submission_gate"
            and row["ledger_submission_count"] == 0
            for row in result["mutation_checks"].values()
        ))
        self.assertTrue(all(value > 0 for row in result["rows"]
                            for value in row.get("decision_ms", ())))
        self.assertTrue(all(row.get("all_backend_advances_authorized")
                            for row in result["rows"]))
        self.assertTrue(all(
            row["injection_actual_phase"] == row["phase"]
            for row in result["rows"]
            if row.get("phase") is not None and row.get("injected")
        ))
        self.assertIn("passes_p3", result["summary"])
        self.assertIn("p95_ms", result["summary"]["decision_latency_ms"])
        self.assertIn("p99_ms", result["summary"]["decision_latency_ms"])
        self.assertIn("max_ms", result["summary"]["decision_latency_ms"])

    def test_goal_revision_changes_goal_geometry_and_continuity_has_direct_evidence(self):
        from experiments.motion_navigation.trajectory_proto.closed_loop_measurement import (
            matrix_specs,
            run_scenario,
        )

        goal = run_scenario(next(row for row in matrix_specs()
                                 if row.scenario_id == "goal_revision-pre_commit"))
        self.assertTrue(goal["goal_state_changed"])
        continuity = run_scenario(next(row for row in matrix_specs()
                                       if row.scenario_id == "continuity-gap-walk"))
        self.assertIsNotNone(continuity["continuity"]["landing_boundary"])
        self.assertGreater(continuity["continuity"]["landing_speed_blocks_per_second"], 0.)
        self.assertEqual(continuity["continuity"]["owner_handoffs"], 0)
        self.assertTrue(continuity["continuity"]["post_landing_command_exists"])


if __name__ == "__main__":
    unittest.main()
