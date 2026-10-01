"""The R28 ruler must measure behaviour, not a version's retry counters."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import gzip

from tests.sim.product_metrics import extract_metrics, compare_metrics, tango_interval, sequential_verdict
from scripts.navigation_coordination_metrics import baseline, compare, load_manifest, quantile, _run_one
from tests.sim.product_cases import product_scenario
from scripts.navigation_migration_evidence import jobs, observe


def frames():
    return [dict(
        movement_tick=tick, position=(0.0, 64.0, z),
        driver_state="success" if tick == 8 else "running",
        source_bound=tick < 8, controller_ids=("route_executor",) if tick < 8 else (),
        route_id="route-a", planning_submissions=["job-a"] if tick == 2 else [],
        goal_revision=1, goal_position=(0.0, 64.0, 1.0),
        goal_revision_requests=[],
        goal_satisfied=tick == 8, on_ground=True,
        applied_movement={"forward": 1, "strafe": 0, "jump": False, "sneak": False, "sprint": False},
    ) for tick, z in zip(range(2, 9), (.1, .2, .2, .2, .2, .5, 1.0))]


def measure(rows):
    return extract_metrics(rows, start_tick=1, start_position=(0.0, 64.0, 0.0), outcome="success")


class ProductMetricTests(unittest.TestCase):
    def test_migration_set_keeps_all_three_frozen_denominators(self):
        cases = list(jobs())
        self.assertEqual(len(cases), 192 + 1000 + 256)
        self.assertEqual(len({item[0] for item in cases}), len(cases))
        result = observe(cases[0])
        self.assertTrue(result["passed"], result["exception"])
        self.assertIn("navigation_session.propose", result["functions_entered"])
        self.assertIn("navigation_session.observe", result["functions_entered"])
        self.assertTrue(result["signature"]["trace"])

    def test_window_detects_oscillation_without_reason_strings(self):
        rows = [dict(frames()[0], movement_tick=tick,
                     position=(.03 if tick % 2 else -.03, 64., 0.),
                     planning_submissions=[], driver_reason="arbitrary text")
                for tick in range(2, 26)]
        rows.append(dict(frames()[-1], movement_tick=26))
        metrics = measure(rows)
        self.assertEqual(metrics["zero_displacement_ticks"], 0)
        self.assertGreater(metrics["net_stall_ticks"]["walking"], 0)
        for row in rows[:-1]:
            row["controller_ids"] = ["landing_edge_probe"]
        prepared = measure(rows)
        self.assertEqual(prepared["net_stall_ticks"]["walking"], 0)
        self.assertGreater(prepared["net_stall_ticks"]["strict_preparation"], 0)

    def test_continuous_seed_parameters_are_unique_and_delay_pairs_share_body(self):
        manifest, _ = load_manifest(Path("tests/sim/manifests/navigation-product-r28-v2.json"))
        normal, late = manifest["groups"][:2]
        parameters = []
        for seed in range(1500):
            _, value = product_scenario(manifest, normal, seed)
            parameters.append(json.dumps(value, sort_keys=True))
        self.assertEqual(len(set(parameters)), 1500)
        for seed in range(12):
            first, a = product_scenario(manifest, normal, seed)
            second, b = product_scenario(manifest, late, seed)
            self.assertEqual(first.start, second.start)
            self.assertEqual(first.goal, second.goal)
            self.assertEqual(first.start_velocity_blocks_per_tick, second.start_velocity_blocks_per_tick)
            self.assertEqual({k: v for k, v in a.items() if k != "late_ticks"},
                             {k: v for k, v in b.items() if k != "late_ticks"})

    def test_internal_recovery_count_does_not_change_common_ruler(self):
        old, new = frames(), frames()
        for row in old:
            row["retry_total_failures"] = 1
            row["session_reason"] = "old recovery vocabulary"
        for row in new:
            row["retry_total_failures"] = 12
            row["session_reason"] = "new vocabulary"
        self.assertEqual(measure(old), measure(new))

    def test_actual_submit_pause_and_controller_change_are_measured(self):
        old = measure(frames())
        changed = frames()
        changed[3]["planning_submissions"] = ["job-b"]
        for row in changed[4:]:
            row["route_id"] = "route-b"
        metrics = measure(changed)
        self.assertEqual(old["planning_requests"], 1)
        self.assertEqual(metrics["planning_requests"], 2)
        self.assertEqual(metrics["controller_switches"], 1)
        self.assertEqual(metrics["zero_displacement_intervals"], [[4, 6]])
        self.assertEqual(metrics["first_movement_ticks"], 1)
        self.assertEqual(metrics["arrival_ticks"], 7)

    def test_terminal_tail_and_satisfied_target_are_not_movement_pauses(self):
        rows = frames()
        for row in rows[2:]:
            row["goal_satisfied"] = True
        self.assertEqual(measure(rows)["zero_displacement_intervals"], [])

    def test_missing_tick_or_evidence_is_not_silently_success(self):
        rows = frames()
        rows.pop(3)
        del rows[2]["planning_submissions"]
        result = measure(rows)
        self.assertIn("movement_tick_gap", result["coverage_gaps"])
        self.assertIn("planning_submissions_missing", result["coverage_gaps"])
        self.assertFalse(result["evidence_complete"])

    def test_duplicate_and_out_of_order_ticks_are_coverage_gaps(self):
        rows = frames()
        rows.insert(2, deepcopy(rows[1]))
        self.assertIn("movement_tick_not_increasing", measure(rows)["coverage_gaps"])

    def test_external_counter_difference_rejects_ground_equivalence(self):
        old, new = measure(frames()), measure(frames())
        new["planning_requests"] += 1
        self.assertEqual(compare_metrics(old, new, tick_tolerance=2)["status"], "different")

    def test_incomplete_evidence_cannot_pass_equivalence(self):
        old, new = measure(frames()), measure(frames())
        new["evidence_complete"] = False
        self.assertEqual(compare_metrics(old, new, tick_tolerance=2)["status"], "insufficient_evidence")

    def test_tango_interval_matches_independent_review_reference(self):
        lo, hi = tango_interval(3, 1, 200)
        self.assertAlmostEqual(lo, -0.03880994775892571, places=10)
        self.assertAlmostEqual(hi, 0.014303213524627628, places=10)
        zero = tango_interval(0, 0, 200)
        self.assertLess(zero[0], 0)
        self.assertGreater(zero[1], 0)
        reversed_interval = tango_interval(1, 3, 200)
        self.assertAlmostEqual(reversed_interval[0], -hi)
        self.assertAlmostEqual(reversed_interval[1], -lo)

    def test_predeclared_two_looks_and_direction(self):
        self.assertEqual(sequential_verdict(0, 0, 200, first_look=200)["status"], "inconclusive")
        first = sequential_verdict(0, 0, 1500, first_look=1500)
        self.assertEqual(first["status"], "pass")
        self.assertFalse(first["may_append"])
        self.assertEqual(sequential_verdict(100, 0, 1500, first_look=1500)["status"], "regression")
        with self.assertRaises(ValueError):
            sequential_verdict(0, 0, 4500, first_look=1500)

    def test_report_percentile_uses_observations_not_average_percentiles(self):
        self.assertEqual(quantile([1, 100, 2, 3], .95), 100)
        self.assertIsNone(quantile([]))

    def test_revision_response_starts_at_command_entry_not_later_observation(self):
        rows = frames()
        rows[1]["goal_revision_requests"] = [{"revision": 2, "movement_tick": 2}]
        for row in rows[1:]:
            row["goal_revision"] = 2
        metrics = measure(rows)
        self.assertEqual(metrics["revision_responses"], [{"revision": 2, "response_ticks": 1, "end": "movement"}])

    def test_official_runtime_trace_can_be_extracted_and_compared(self):
        manifest_path = Path("tests/sim/manifests/navigation-product-r28.json")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old, new = root / "old", root / "new"
            result = baseline(manifest_path, old, seed_count=1, groups=["point-normal"])
            self.assertEqual(result["groups"][0]["success"], 1)
            self.assertEqual(result["groups"][0]["planning_requests"], 1)
            self.assertFalse(json.loads((old / "metadata.json").read_text("utf-8"))["complete_manifest"])
            baseline(manifest_path, new, seed_count=1, groups=["point-normal"])
            report = compare(old, new, root / "comparison")
            self.assertEqual(report["equivalent"], 1)
            with self.assertRaises(FileExistsError):
                baseline(manifest_path, old, seed_count=1, groups=["point-normal"])
            # Raw failure/evidence records cannot be changed unnoticed.
            trace = next((new / "traces").iterdir())
            trace.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum"):
                compare(old, new, root / "tampered-comparison")
            for folder in (old, new):
                (folder / "runs.jsonl").write_text("", "utf-8")
            with self.assertRaisesRegex(ValueError, "denominator"):
                compare(old, new, root / "deleted-failures")

    def test_long_follow_budget_is_frozen_but_not_claimed_implemented(self):
        manifest, _ = load_manifest(Path("tests/sim/manifests/navigation-product-r28.json"))
        probe = manifest["budgets"]["follow_probe"]
        self.assertEqual(probe["duration_seconds"], 1800)
        self.assertGreater(probe["duration_seconds"] / probe["periodic_recovery_spacing_seconds"], manifest["budgets"]["finite_task_recoveries"])
        self.assertGreater(probe["burst_recovery_count"], probe["recoveries_per_window"])
        self.assertIn("not implemented", probe["status"])

    def test_exception_keeps_partial_trace_and_cannot_claim_complete_evidence(self):
        manifest, _ = load_manifest(Path("tests/sim/manifests/navigation-product-r28.json"))
        def broken_run(scenario, *, trace_sink):
            for row in frames():
                trace_sink(row)
            raise RuntimeError("injected formal-call failure")
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "traces").mkdir()
            with patch("scripts.navigation_coordination_metrics.run", broken_run):
                result = _run_one((manifest, manifest["groups"][0], 0, temp))
            with gzip.open(Path(temp) / result["trace_file"], "rt", encoding="utf-8") as stream:
                record = json.load(stream)
            self.assertEqual(len(record["trace"]), 7)
            self.assertIn("injected formal-call failure", result["exception"])
            self.assertFalse(result["metrics"]["evidence_complete"])

    def test_same_total_pause_time_does_not_hide_more_stop_start_intervals(self):
        old, new = measure(frames()), measure(frames())
        new["zero_displacement_intervals"] = [[10, 12], [20, 22]]
        new["zero_displacement_ticks"] = old["zero_displacement_ticks"]
        self.assertEqual(compare_metrics(old, new, tick_tolerance=2)["status"], "different")


if __name__ == "__main__":
    unittest.main()
