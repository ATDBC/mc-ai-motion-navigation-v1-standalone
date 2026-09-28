from __future__ import annotations

import unittest

from scripts.continuous_height_runtime import (
    _air_positions,
    _damage_budget,
    _diagnostic_row,
    _fixture_commands,
    _goal,
    _start_and_goal,
    _supports,
    continuous_height_trial_plan,
    navigation_coordination_hardening_plan,
)


class ContinuousHeightRuntimeTests(unittest.TestCase):
    def test_diagnostic_row_keeps_pipeline_metrics_at_the_exported_level(self):
        from types import SimpleNamespace

        runtime = SimpleNamespace(
            observation=SimpleNamespace(sequence_id=7),
            navigation_observation_adapter=SimpleNamespace(
                latest_frame=SimpleNamespace(
                    body=SimpleNamespace(position=(1.0, 2.0, 3.0)),
                ),
            ),
        )
        backend = SimpleNamespace(last_diagnostics={"client_tick": 9})

        row = _diagnostic_row(
            runtime,
            backend,
            "episode",
            "trial",
            pipeline_diagnostic=lambda: {"payload_bytes": 123},
        )

        self.assertEqual(row["observation_pipeline"], {"payload_bytes": 123})
        self.assertEqual(row["diagnostics"], {"client_tick": 9})

    def test_trial_plan_covers_four_directions_and_three_motion_shapes(self):
        trials = continuous_height_trial_plan()

        self.assertEqual(len(trials), 12)
        self.assertEqual(
            {trial["kind"] for trial in trials},
            {"low_height_stairs", "stair_descent", "direct_drop"},
        )
        for kind in {trial["kind"] for trial in trials}:
            self.assertEqual(
                {trial["direction_index"] for trial in trials
                 if trial["kind"] == kind},
                {0, 1, 2, 3},
            )

    def test_only_the_five_block_direct_drop_receives_damage_authority(self):
        trials = continuous_height_trial_plan()
        direct = [trial for trial in trials if trial["kind"] == "direct_drop"]

        self.assertEqual(
            [trial["drop_blocks"] for trial in direct], [1, 2, 3, 5],
        )
        for trial in trials:
            budget = _damage_budget(trial)
            expected = (
                2.0 if trial["kind"] == "direct_drop"
                and trial.get("drop_blocks") == 5 else 0.0
            )
            self.assertEqual(budget.maximum_expected_damage_points, expected)

    def test_hardening_plan_freezes_three_formal_path_gates(self):
        trials = navigation_coordination_hardening_plan()

        self.assertEqual(
            [trial["trial_id"] for trial in trials],
            [
                "runup-step-down",
                "landing-support-removed",
                "fixed-one-tick-late-drop",
            ],
        )
        self.assertEqual(
            [trial["injection"] for trial in trials],
            [None, "remove_landing_support", "late_first_verified_input"],
        )
        self.assertEqual(
            [trial["expected_terminal"] for trial in trials],
            ["success", "failed", "success"],
        )
        self.assertEqual(
            trials[1]["expected_reason"], "landing_support_missing",
        )

    def test_hardening_fixtures_are_disjoint_and_runup_precedes_descent(self):
        trials = navigation_coordination_hardening_plan()
        occupied = []
        for trial in trials:
            positions = {position for position, _ in _supports(trial)}
            self.assertTrue(all(positions.isdisjoint(old) for old in occupied))
            occupied.append(positions)

        runup = trials[0]
        supports = sorted(position for position, _ in _supports(runup))
        start, goal = _start_and_goal(runup)
        self.assertEqual(start[1] - goal[1], 1.0)
        self.assertGreaterEqual(len(supports), 7)
        self.assertEqual(runup["runup_blocks"], 4)

    def test_each_trial_uses_a_disjoint_world_region(self):
        occupied_regions = []
        for trial in continuous_height_trial_plan():
            positions = {position for position, _ in _supports(trial)}
            for previous in occupied_regions:
                self.assertTrue(positions.isdisjoint(previous))
            occupied_regions.append(positions)

    def test_goal_region_accepts_verified_landing_scatter_inside_the_block(self):
        trial = next(
            trial for trial in continuous_height_trial_plan()
            if trial["kind"] == "stair_descent"
            and trial["direction_index"] == 0
        )
        _, (x, y, z) = _start_and_goal(trial)
        goal = _goal(trial)

        self.assertLessEqual(goal.region.min_x, x - .15)
        self.assertGreaterEqual(goal.region.max_x, x + .15)
        self.assertLessEqual(goal.region.min_z, z - .15)
        self.assertGreaterEqual(goal.region.max_z, z + .15)

    def test_fixture_uses_real_player_commands_and_bounded_air_queries(self):
        for trial in continuous_height_trial_plan():
            commands = _fixture_commands(trial)
            self.assertTrue(any(command.startswith("tp MC2PProbe")
                                for command in commands))
            self.assertTrue(any("minecraft:smooth_stone_slab[type=bottom]" in command
                                for command in commands)
                            if trial["kind"] == "low_height_stairs" else True)
            self.assertLessEqual(len(_air_positions(trial)), 256)

            highest_requested_y = max(y for _, y, _ in _air_positions(trial))
            clear_command = next(
                command for command in commands if command.startswith("fill ")
            )
            self.assertIn(f" {highest_requested_y} ", clear_command)


if __name__ == "__main__":
    unittest.main()
