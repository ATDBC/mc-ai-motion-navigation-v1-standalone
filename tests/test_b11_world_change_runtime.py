from __future__ import annotations

import unittest

from scripts.b11_world_change_runtime import (
    b11_negative_trial_plan,
    b11_trial_plan,
    _fixture_commands,
    _diagnostic_row,
    _start_commands,
)


class B11WorldChangeRuntimeTests(unittest.TestCase):
    def test_diagnostic_row_exposes_pipeline_sample_at_top_level(self):
        row = _diagnostic_row(
            "episode-1", 7, {"client_tick": 12},
            {"schema_version": "mc2p.observation-pipeline-diagnostics.v1"},
        )
        self.assertEqual(row["observation_sequence_id"], 7)
        self.assertEqual(row["diagnostics"], {"client_tick": 12})
        self.assertEqual(
            row["observation_pipeline"]["schema_version"],
            "mc2p.observation-pipeline-diagnostics.v1",
        )

    def test_frozen_positive_plan_has_the_declared_sixty_trials(self):
        trials = b11_trial_plan()
        self.assertEqual(len(trials), 60)
        groups = {}
        for trial in trials:
            key = (trial["kind"], trial["gap_count"])
            groups[key] = groups.get(key, 0) + 1
        self.assertEqual(groups, {
            ("fixed_placement", 1): 20,
            ("bridge", 1): 20,
            ("bridge", 2): 10,
            ("bridge", 3): 10,
        })
        self.assertEqual(len({trial["trial_id"] for trial in trials}), 60)

    def test_fixture_uses_survival_real_inventory_and_no_world_edit_actor(self):
        trial = next(item for item in b11_trial_plan()
                     if item["kind"] == "bridge" and item["gap_count"] == 3)
        commands = _fixture_commands(trial)
        self.assertIn("gamemode survival MC2PProbe", commands)
        self.assertIn(
            "item replace entity MC2PProbe weapon.mainhand with minecraft:dirt 3",
            commands,
        )
        self.assertTrue(any(command.startswith("tp MC2PProbe ") for command in commands))
        self.assertTrue(any(command.endswith("0.0 20.0") for command in commands))
        start_commands = _start_commands(trial)
        self.assertTrue(any(command.endswith("-90.0 70.0")
                            for command in start_commands))
        self.assertTrue(any(command.endswith("minecraft:air replace")
                            for command in start_commands))
        self.assertFalse(any("setblock 1 99 0 minecraft:dirt" in command
                             for command in commands))

    def test_negative_plan_freezes_two_runs_for_each_failure_class(self):
        trials = b11_negative_trial_plan()
        groups = {}
        for trial in trials:
            groups[trial["case"]] = groups.get(trial["case"], 0) + 1
        self.assertEqual(len(trials), 24)
        self.assertEqual(set(groups.values()), {2})
        self.assertEqual(set(groups), {
            "no_authorization",
            "wrong_item",
            "insufficient_items",
            "misaligned_target",
            "destination_occupied",
            "cancel_before_submit",
            "cancel_after_first_confirmation",
            "confirmation_timeout",
            "goal_revision_changed",
            "goal_revision_at_edge_before_dispatch",
            "goal_revision_after_dispatch",
            "bridge_cell_claimed",
        })


if __name__ == "__main__":
    unittest.main()
