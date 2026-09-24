import math
import unittest

from scripts.calibrate_b02_ground_motion import rollout_error_summary
from mc2p.motion_nav.evidence.ground_calibration import (
    GroundMotionSample, calibrate_ground_motion,
)
from mc2p.motion_nav.ground_motion import (
    GroundControl, GroundMotionProfile, PlanarBodyState, predict_ground,
)


class GroundCalibrationTests(unittest.TestCase):
    def test_recovers_profile_and_reports_validation_error(self):
        wanted = GroundMotionProfile(0.05, 60.0, 0.55, 4.2)
        controls = (
            *(GroundControl(1.0, 0.0, 0.0) for _ in range(18)),
            *(GroundControl(0.0, 0.0, 0.0) for _ in range(12)),
            *(GroundControl(0.0, 1.0, math.pi / 3) for _ in range(16)),
            *(GroundControl(0.0, 0.0, math.pi / 3) for _ in range(12)),
        )
        states = predict_ground(PlanarBodyState(0, 0, 0, 0, 0), controls, wanted)
        samples = tuple(GroundMotionSample(before, control, after)
                        for before, control, after in zip(states, controls, states[1:]))
        calibration = calibrate_ground_motion(samples, tick_seconds=0.05)
        self.assertAlmostEqual(calibration.profile.acceleration_blocks_per_second2, 60.0, delta=0.05)
        self.assertAlmostEqual(calibration.profile.velocity_retention_per_tick, 0.55, delta=0.005)
        self.assertAlmostEqual(calibration.profile.maximum_speed_blocks_per_second, 4.2, delta=0.05)
        self.assertLess(calibration.position_error_p95_blocks, 1e-6)
        self.assertLess(calibration.velocity_error_p95_blocks_per_second, 1e-6)

    def test_requires_both_powered_and_release_samples(self):
        state = PlanarBodyState(0, 0, 0, 0, 0)
        control = GroundControl(1, 0, 0)
        after = PlanarBodyState(0, 0.1, 0, 1, 0)
        with self.assertRaises(ValueError):
            calibrate_ground_motion((GroundMotionSample(state, control, after),), tick_seconds=0.05)

    def test_six_step_validation_uses_only_contiguous_samples(self):
        wanted = GroundMotionProfile(0.05, 60.0, 0.55, 4.2)
        controls = tuple(GroundControl(1 if index < 5 else 0, 1 if index % 3 == 0 else 0, 0.4)
                         for index in range(12))
        states = predict_ground(PlanarBodyState(0, 0, 0, 0, 0.4), controls, wanted)
        samples = tuple(GroundMotionSample(before, control, after)
                        for before, control, after in zip(states, controls, states[1:]))
        summary = rollout_error_summary(samples, wanted, horizon=6)
        self.assertEqual(summary["sample_count"], 7)
        self.assertLess(summary["position_p99_blocks"], 1e-9)
        broken = samples[:6] + (GroundMotionSample(
            PlanarBodyState(99, 99, 0, 0, .4), samples[6].control, samples[6].after,
        ),) + samples[7:]
        self.assertLess(rollout_error_summary(broken, wanted, horizon=6)["sample_count"], 7)


if __name__ == "__main__":
    unittest.main()
