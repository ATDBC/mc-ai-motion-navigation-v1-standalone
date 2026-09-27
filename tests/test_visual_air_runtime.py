import unittest


class VisualAirRuntimeTests(unittest.TestCase):
    def test_fixture_freezes_visual_air_boundaries(self):
        from scripts.visual_air_runtime import FIXED_CASES

        by_name = {case.name: case for case in FIXED_CASES}
        self.assertEqual(
            {name for name, case in by_name.items() if case.expected_air},
            {"front_open", "behind_glass", "barrier_visual_air"},
        )
        self.assertEqual(
            {name for name, case in by_name.items() if not case.expected_air},
            {
                "behind_stone", "behind_lava", "behind_fence",
                "behind_camera", "beyond_16", "view_edge_partial",
            },
        )

    def test_benchmark_contains_exactly_128_in_view_candidates(self):
        from scripts.visual_air_runtime import benchmark_positions

        positions = benchmark_positions(400, 65, 400)
        self.assertEqual(len(positions), 128)
        self.assertEqual(len(set(positions)), 128)
        self.assertTrue(all(404 <= z <= 411 for _, _, z in positions))

    def test_evaluator_requires_exact_fixed_result_and_reports_air_p95(self):
        from scripts.visual_air_runtime import FIXED_CASES, evaluate_visual_air

        expected = {case.position for case in FIXED_CASES if case.expected_air}
        report, checks = evaluate_visual_air(
            benchmark_expected=tuple((x, 65, 404) for x in range(128)),
            benchmark_frames=[
                {
                    "confirmed": tuple((x, 65, 404) for x in range(128)),
                    "air_query_ns": value,
                }
                for value in range(1, 101)
            ],
            fixed_confirmed=tuple(sorted(expected)),
            occupied_before_sources=("surface_depth",),
            removed_after_sources=("air_query",),
            contact_block_id="minecraft:barrier",
            contact_sources=("body_contact",),
        )
        self.assertEqual(report["air_query_ns"]["p95"], 95)
        self.assertTrue(all(check["passed"] for check in checks), checks)

    def test_evaluator_rejects_a_hidden_positive(self):
        from scripts.visual_air_runtime import FIXED_CASES, evaluate_visual_air

        expected = {case.position for case in FIXED_CASES if case.expected_air}
        hidden = next(case.position for case in FIXED_CASES if case.name == "behind_stone")
        _, checks = evaluate_visual_air(
            benchmark_expected=((0, 0, 0),),
            benchmark_frames=[{"confirmed": ((0, 0, 0),), "air_query_ns": 1}],
            fixed_confirmed=tuple(sorted((*expected, hidden))),
            occupied_before_sources=("surface_depth",),
            removed_after_sources=("air_query",),
            contact_block_id="minecraft:barrier",
            contact_sources=("body_contact",),
        )
        fixed = next(check for check in checks if check["name"] == "visual_air_fixed_cases_exact")
        self.assertFalse(fixed["passed"])


if __name__ == "__main__":
    unittest.main()
