"""D097 M0 Windows feasibility-probe contract."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experiments.motion_navigation.trajectory_proto.m0_probe import (
    FORMAL_OPTIONS,
    NEGATIVE_CASES,
    POSITIVE_CASES,
    ProbeStatus,
    build_mixed_action_fixture,
    measure_known_input,
    run_case,
    summarize_durations,
)


class TrajectoryProtoM0ProbeTests(unittest.TestCase):
    def test_frozen_inventory_and_formal_options_are_explicit(self):
        self.assertEqual(len(POSITIVE_CASES), 22)
        self.assertEqual(len(NEGATIVE_CASES), 6)
        self.assertFalse(FORMAL_OPTIONS.floor_prune)
        self.assertTrue(FORMAL_OPTIONS.grounded_jump_only)

    def test_mixed_ground_jump_air_counterexample_is_found(self):
        fixture = build_mixed_action_fixture()
        result = run_case("mixed_ground_jump_air", None, options=FORMAL_OPTIONS)
        self.assertIs(result.status, ProbeStatus.FOUND)
        self.assertEqual(result.inputs, fixture.expected_inputs)
        self.assertTrue(result.input_hash)

    def test_formal_matrix_finds_positives_and_types_negatives(self):
        for scenario_id, tier_id in POSITIVE_CASES:
            with self.subTest(scenario=scenario_id, tier=tier_id):
                result = run_case(scenario_id, tier_id, options=FORMAL_OPTIONS)
                self.assertIs(result.status, ProbeStatus.FOUND)
                self.assertGreater(result.physics_steps, 0)
        expected = {
            "gap_start_4_width_3_a3": ProbeStatus.SEARCH_EXHAUSTED,
            "one_twelfth_support": ProbeStatus.SEARCH_EXHAUSTED,
            "resource_goal": ProbeStatus.NEEDS_INFORMATION,
            "unknown_landing": ProbeStatus.NEEDS_INFORMATION,
            "tiny_budget": ProbeStatus.BUDGET_EXHAUSTED,
            "collision_only": ProbeStatus.SEARCH_EXHAUSTED,
        }
        for scenario_id, tier_id in NEGATIVE_CASES:
            with self.subTest(scenario=scenario_id):
                self.assertIs(run_case(scenario_id, tier_id, options=FORMAL_OPTIONS).status,
                              expected[scenario_id])

    def test_known_input_cost_keeps_full_scan_and_lazy_estimate_separate(self):
        case = run_case("jump_gap_1", "A3", options=FORMAL_OPTIONS)
        ticks = iter((0, 2_000_000, 3_000_000, 8_000_000))
        cost = measure_known_input("jump_gap_1", "A3", case.inputs,
                                   clock_ns=lambda: next(ticks))
        self.assertGreater(cost.full_scan_steps, 0)
        self.assertGreater(cost.rollout_steps, 0)
        self.assertGreater(cost.estimated_per_tick_steps, 0)
        self.assertGreater(cost.estimated_commit_steps, 0)
        self.assertLess(cost.estimated_per_tick_steps, cost.full_scan_steps)
        self.assertLess(cost.estimated_commit_steps, cost.full_scan_steps)
        self.assertEqual((cost.rollout_ms, cost.goal_ms, cost.full_scan_ms),
                         (2., 1., 5.))
        self.assertEqual(case.input_hash,
                         run_case("jump_gap_1", "A3", options=FORMAL_OPTIONS).input_hash)

    def test_nearest_rank_summary_uses_twenty_samples(self):
        summary = summarize_durations(tuple(float(value) for value in range(1, 21)))
        self.assertEqual((summary.samples, summary.p50_ms, summary.p95_ms,
                          summary.p99_ms, summary.max_ms),
                         (20, 10., 19., 20., 20.))

    def test_hashseed_does_not_change_formal_result_or_counts(self):
        script = (
            "import json; "
            "from experiments.motion_navigation.trajectory_proto.m0_probe import "
            "FORMAL_OPTIONS,run_case; "
            "r=run_case('turn_90','A15',options=FORMAL_OPTIONS); "
            "print(json.dumps([r.status.value,r.input_hash,r.physics_steps,r.scan_count]))"
        )
        rows = []
        for seed in ("1", "77", "991"):
            completed = subprocess.run(
                [sys.executable, "-c", script], cwd=os.getcwd(),
                env=dict(os.environ, PYTHONHASHSEED=seed), capture_output=True,
                text=True, check=True,
            )
            rows.append(json.loads(completed.stdout))
        self.assertEqual(rows[0], rows[1])
        self.assertEqual(rows[0], rows[2])

    def test_measurement_module_writes_a_separated_quick_report(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "m0.json"
            subprocess.run(
                [sys.executable, "-m", "experiments.motion_navigation.trajectory_proto.m0_probe",
                 "--warmups", "0", "--rounds", "1",
                 "--case", "jump_gap_1:A3", "--output", str(output)],
                cwd=root, check=True, capture_output=True, text=True,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["measurement"]["warmups"], 0)
        self.assertEqual(payload["measurement"]["rounds"], 1)
        self.assertIn("formal", payload["modes"])
        row = payload["modes"]["formal"]["cases"][0]
        self.assertEqual(len(row["controller_ms"]["samples_ms"]), 1)
        self.assertIn("full_scan_steps", row["known_input_cost"])
        self.assertIn("estimated_per_tick_steps", row["known_input_cost"])
        self.assertEqual(len(row["known_input_cost"]["rollout_ms"]["samples_ms"]), 1)


if __name__ == "__main__":
    unittest.main()
