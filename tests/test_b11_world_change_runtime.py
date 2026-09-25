from __future__ import annotations

import unittest

from scripts.b11_world_change_runtime import (
    b11_negative_trial_plan,
    b11_trial_plan,
    _fixture_commands,
)


class B11WorldChangeRuntimeTests(unittest.TestCase):
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
        self.assertFalse(any("setblock 1 99 0 minecraft:dirt" in command
                             for command in commands))

    def test_negative_plan_freezes_two_runs_for_each_failure_class(self):
        trials = b11_negative_trial_plan()
        groups = {}
        for trial in trials:
            groups[trial["case"]] = groups.get(trial["case"], 0) + 1
        self.assertEqual(len(trials), 20)
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
            "bridge_cell_claimed",
        })


if __name__ == "__main__":
    unittest.main()
