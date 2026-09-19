import unittest

from scripts.jump_up_calibration_runtime import summarize_jump_trial


def sample(sequence, y, z, on_ground, *, applied=True):
    return {
        "sequence": sequence,
        "position": [0.5, y, z],
        "velocity_blocks_per_second": [0.0, 0.0, 0.0],
        "is_on_ground": on_ground,
        "horizontal_collision": False,
        "vertical_collision": on_ground,
        "requested": {"forward": 1, "strafe": 0, "jump": sequence == 1,
                      "sneak": False, "sprint": False},
        "input_confirmed": applied,
    }


class JumpUpCalibrationTests(unittest.TestCase):
    def test_summary_uses_observed_takeoff_and_target_landing(self):
        samples = [
            sample(0, 10.0, .5, True),
            sample(1, 10.0, .58, True),
            sample(2, 10.42, .72, False),
            sample(3, 11.18, 1.12, False),
            sample(4, 11.0, 1.48, True),
            sample(5, 11.0, 1.50, True),
        ]

        result = summarize_jump_trial(samples, start_y=10.0, target_y=11.0,
                                      target_center=(.5, 1.5))

        self.assertEqual(result["takeoff_sequence"], 2)
        self.assertEqual(result["landing_sequence"], 4)
        self.assertAlmostEqual(result["maximum_rise_blocks"], 1.18)
        self.assertAlmostEqual(result["landing_horizontal_error_blocks"], .02)
        self.assertTrue(result["succeeded"])

    def test_input_confirmation_does_not_count_as_takeoff(self):
        samples = [sample(0, 10.0, .5, True), sample(1, 10.0, .6, True)]
        result = summarize_jump_trial(samples, start_y=10.0, target_y=11.0,
                                      target_center=(.5, 1.5))
        self.assertIsNone(result["takeoff_sequence"])
        self.assertIsNone(result["landing_sequence"])
        self.assertFalse(result["succeeded"])

    def test_wrong_level_landing_is_not_success(self):
        samples = [
            sample(0, 10.0, .5, True), sample(1, 10.3, .7, False),
            sample(2, 10.0, 1.5, True),
        ]
        result = summarize_jump_trial(samples, start_y=10.0, target_y=11.0,
                                      target_center=(.5, 1.5))
        self.assertEqual(result["landing_sequence"], 2)
        self.assertFalse(result["succeeded"])


if __name__ == "__main__":
    unittest.main()
