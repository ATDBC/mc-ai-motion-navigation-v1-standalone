from __future__ import annotations

import unittest


class MotionBenchmarkTests(unittest.TestCase):
    def test_calculator_timing_is_separate_from_memory_tracking(self):
        from scripts.benchmark_motion_calculator import run_benchmark

        result = run_benchmark(repetitions=1, warmups=0)

        self.assertFalse(result["timing_under_tracemalloc"])
        self.assertGreater(result["peak_traced_bytes"], 0)
        self.assertTrue(result["python_version"])
        self.assertIn("cpu", result)
        self.assertEqual(
            [(row["ticks"], row["branches"]) for row in result["results"]],
            [(1, 1), (6, 1), (20, 1), (60, 1), (20, 32), (20, 128)],
        )

    def test_legacy_known_map_benchmark_builds_a_complete_snapshot(self):
        from scripts.benchmark_known_map_planner import run_benchmark

        result = run_benchmark(size=4, runs=1, snapshot_batch_cells=16)

        self.assertEqual(result["node_count"], 16)
        self.assertTrue(result["passed"])
        self.assertGreater(result["path_nodes"], 0)

    def test_surface_planner_benchmark_uses_the_formal_snapshot_entry(self):
        from scripts.benchmark_surface_planner import run_benchmark

        result = run_benchmark(size=8, runs=2)

        self.assertEqual(result["node_count"], 64)
        self.assertTrue(result["passed"])
        self.assertGreater(result["path_nodes"], 0)
        self.assertLessEqual(result["expanded_nodes"], 16)


if __name__ == "__main__":
    unittest.main()
