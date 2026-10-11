import unittest


class IncrementalMotionPerformanceTests(unittest.TestCase):
    def test_decision_measurement_sums_lookup_proof_and_permit(self):
        from scripts.measure_incremental_motion_r98 import DecisionParts

        parts = DecisionParts(lookup_ns=10, controller_ns=20, proof_ns=30, permit_ns=40)
        self.assertEqual(parts.total_ns, 100)

    def test_measurement_script_does_not_depend_on_review_tree(self):
        from pathlib import Path
        from scripts import measure_incremental_motion_r98

        source = Path(measure_incremental_motion_r98.__file__).read_text(encoding="utf-8")
        self.assertNotIn("reviews", source)
        self.assertNotIn(".tmp", source)

    def test_complete_decision_has_one_shared_budget_owner(self):
        from dataclasses import replace
        from experiments.motion_navigation.trajectory_proto import m0_probe
        from experiments.motion_navigation.trajectory_proto.decision import (
            DecisionContext,
            FrozenInputAdapter,
            complete_decision,
        )
        from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget

        fixture = m0_probe.build_mixed_action_fixture().fixture
        inputs = m0_probe.build_mixed_action_fixture().expected_inputs
        # Each half would fit in an independent 400-step counter.  One shared
        # counter must exhaust when controller-trace and proof are combined.
        request = replace(fixture.request, budget=SearchBudget(400, 400, 40, 2))
        context = DecisionContext.create(request)
        result = complete_decision(context, request, fixture.world,
                                   FrozenInputAdapter(inputs))
        self.assertEqual(result.reason.value, "physics_step_budget")
        self.assertEqual(result.physics_steps, 400)
        self.assertEqual(sum(result.stage_physics_steps.values()), result.physics_steps)
        self.assertEqual(result.counter_identity, id(context.counter))

    def test_frozen_and_controller_paths_report_stage_ownership(self):
        from experiments.motion_navigation.trajectory_proto.measurement import (
            _set_a,
            _set_d,
            measure_case,
        )
        from experiments.motion_navigation.trajectory_proto.motion_table import load_frozen_table

        table = load_frozen_table()
        frozen = measure_case(next(_set_a(1)), table)
        generated = measure_case(next(_set_d(1)), table)
        self.assertEqual(frozen["input_source"], "frozen_input")
        self.assertEqual(frozen["parts_ms"]["lookup_ms"], 0.)
        self.assertEqual(frozen["parts_ms"]["controller_ms"], 0.)
        self.assertEqual(generated["input_source"], "table_controller")
        self.assertGreater(generated["stage_physics_steps"]["controller"], 0)
        self.assertEqual(sum(generated["stage_physics_steps"].values()),
                         generated["physics_steps"])
        self.assertGreater(frozen["damage_state_updates"], 0)
        self.assertGreater(generated["damage_state_updates"], 0)
        self.assertEqual(frozen["trace_reuse"], "VALID")
        self.assertEqual(generated["trace_reuse"], "VALID")
        self.assertEqual(frozen["stage_physics_steps"]["proof"],
                         frozen["physics_steps"] - frozen["stage_physics_steps"]["trace"])

    def test_fixed_runahead_reports_complete_per_tick_decisions(self):
        from experiments.motion_navigation.trajectory_proto.measurement import (
            _set_a,
            measure_case,
        )
        from experiments.motion_navigation.trajectory_proto.motion_table import load_frozen_table

        row = measure_case(next(_set_a(1)), load_frozen_table(), runahead_steps=24)
        self.assertEqual(row["proof_mode"], "strict_runahead")
        self.assertEqual(row["runahead_steps"], 24)
        self.assertTrue(row["decision_samples"])
        self.assertEqual(sum(sample["physics_steps"] for sample in row["decision_samples"]),
                         row["physics_steps"])
        self.assertEqual(sum(sample["wall_ns"] for sample in row["decision_samples"]),
                         round(row["measured_parts_ms"] * 1_000_000))

    def test_calibration_uses_only_a_and_c_before_freezing_budget(self):
        from experiments.motion_navigation.trajectory_proto.measurement import calibrate_runahead
        from experiments.motion_navigation.trajectory_proto.motion_table import load_frozen_table

        result = calibrate_runahead(load_frozen_table(), budgets=(24, 32), limit=1, rounds=1)
        self.assertEqual(result["calibration_sets"], ["A", "C"])
        self.assertEqual([row["runahead_steps"] for row in result["budgets"]], [24, 32])
        self.assertTrue(all({sample["set"] for sample in row["rows"]} <= {"A", "C"}
                            for row in result["budgets"]))
        selected = result["selected_runahead_steps"]
        self.assertTrue(selected is None or selected in (24, 32))


if __name__ == "__main__":
    unittest.main()
