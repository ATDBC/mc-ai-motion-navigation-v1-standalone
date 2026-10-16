import math
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.ground_motion import (
    GroundControl, GroundMotionProfile, PlanarBodyState, control_world_direction,
    predict_ground, world_direction_control,
)


class GroundMotionTests(unittest.TestCase):
    def test_world_direction_round_trips_at_different_view_yaws(self):
        wanted = (0.6, 0.8)
        for yaw in (0.0, math.pi / 4, math.pi / 2, math.pi, -2.1):
            with self.subTest(yaw=yaw):
                control = world_direction_control(*wanted, yaw)
                actual = control_world_direction(control)
                self.assertAlmostEqual(actual[0], wanted[0])
                self.assertAlmostEqual(actual[1], wanted[1])

    def test_pitch_is_absent_from_horizontal_control_contract(self):
        control = world_direction_control(0.0, 1.0, math.pi / 3)
        self.assertAlmostEqual(math.hypot(control.forward, control.strafe), 1.0)
    def setUp(self):
        self.profile = GroundMotionProfile(
            tick_seconds=0.05,
            acceleration_blocks_per_second2=4.0,
            velocity_retention_per_tick=0.5,
            maximum_speed_blocks_per_second=5.0,
        )

    def test_forward_direction_uses_minecraft_yaw_in_radians(self):
        state = PlanarBodyState(0.0, 0.0, 0.0, 0.0, 0.0)
        south = predict_ground(state, (GroundControl(1.0, 0.0, 0.0),), self.profile)[-1]
        west = predict_ground(state, (GroundControl(1.0, 0.0, math.pi / 2),), self.profile)[-1]
        self.assertAlmostEqual(south.x, 0.0, places=9)
        self.assertGreater(south.z, 0.0)
        self.assertLess(west.x, 0.0)
        self.assertAlmostEqual(west.z, 0.0, places=9)

    def test_release_reduces_velocity_and_keeps_advancing(self):
        moving = PlanarBodyState(0.0, 0.0, 2.0, 0.0, 0.0)
        released = predict_ground(moving, (GroundControl(0.0, 0.0, 0.0),), self.profile)[-1]
        self.assertGreater(released.x, moving.x)
        self.assertLess(released.velocity_x, moving.velocity_x)

    def test_profile_rejects_ambiguous_or_invalid_units(self):
        with self.assertRaises(ContractViolation):
            GroundMotionProfile(0.0, 4.0, 0.5, 5.0)
        with self.assertRaises(ContractViolation):
            GroundControl(1.1, 0.0, 0.0)


if __name__ == "__main__":
    unittest.main()
