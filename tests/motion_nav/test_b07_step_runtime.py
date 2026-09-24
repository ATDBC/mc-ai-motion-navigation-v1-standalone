from __future__ import annotations

import unittest

from scripts.step_transition_runtime import summarize_step_trials


class B07StepRuntimeTests(unittest.TestCase):
    def test_summary_keeps_up_down_safety_and_control_timing_separate(self):
        summary = summarize_step_trials([
            {"final_state": "complete", "jump_pulses": 0,
             "horizontal_collisions": 0, "level_error_blocks": .01},
            {"final_state": "failed", "jump_pulses": 1,
             "horizontal_collisions": 2, "level_error_blocks": .2},
        ], [100, 200, 300, 400])

        self.assertEqual(summary["trial_count"], 2)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["jump_pulses"], 1)
        self.assertEqual(summary["horizontal_collisions"], 2)
        self.assertEqual(summary["control_time_ns"]["maximum"], 400)


if __name__ == "__main__":
    unittest.main()
