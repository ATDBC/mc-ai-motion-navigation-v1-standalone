from dataclasses import replace
import math
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.fixed_route import (
    FixedRoute, FixedRouteConfig, FixedRouteController, FixedRouteState,
    RoutePoint,
)
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.segment_entry import (
    SegmentEntryWindow,
    body_fits_segment_entry,
    physics_fits_segment_entry,
)
from tests.motion_nav.test_b07_step_transition import frame
from tests.motion_nav.test_b07_support_surfaces import surface_world
from tests.motion_nav.test_b10_gap_solver import fixture
from tests.motion_nav.test_fixed_route_walk import (
    FlatFixture, profile as ground_profile,
)


def window() -> SegmentEntryWindow:
    return SegmentEntryWindow(
        reference_point=(1.5, 1.0, 0.5),
        horizontal_approach_direction=(1.0, 0.0),
        minimum_longitudinal_offset_blocks=-0.2,
        maximum_longitudinal_offset_blocks=0.1,
        maximum_lateral_offset_blocks=0.12,
        minimum_feet_y=0.95,
        maximum_feet_y=1.05,
        minimum_speed_blocks_per_second=0.05,
        maximum_speed_blocks_per_second=0.5,
        maximum_velocity_direction_error_radians=math.radians(12.0),
        allowed_poses=frozenset({"standing"}),
        allowed_modes=frozenset({MovementMode.WALK}),
        required_yaw_radians=None,
        maximum_yaw_error_radians=None,
        profile_id="entry-test-v1",
    )


class SegmentEntryWindowTests(unittest.TestCase):
    def setUp(self):
        self.world = surface_world({})
        self.base = frame(self.world, 1, (1.4, 1.0, 0.5)).body

    def body(self, *, position=(1.4, 1.0, 0.5), velocity=(0.2, 0.0, 0.0)):
        return replace(
            self.base,
            position=position,
            velocity_blocks_per_second=velocity,
            body_box=self.base.body_box.moved(
                position[0] - self.base.position[0],
                position[1] - self.base.position[1],
                position[2] - self.base.position[2],
            ),
        )

    def test_accepts_directional_entry_inside_all_ranges(self):
        self.assertTrue(body_fits_segment_entry(
            window(), self.body(), MovementMode.WALK,
        ))

    def test_rejects_wrong_side_lateral_error_speed_and_direction(self):
        cases = (
            self.body(position=(1.29, 1.0, 0.5)),
            self.body(position=(1.4, 1.0, 0.63)),
            self.body(velocity=(0.6, 0.0, 0.0)),
            self.body(velocity=(-0.2, 0.0, 0.0)),
        )
        for body in cases:
            with self.subTest(position=body.position,
                              velocity=body.velocity_blocks_per_second):
                self.assertFalse(body_fits_segment_entry(
                    window(), body, MovementMode.WALK,
                ))

    def test_nearly_stopped_entry_has_no_meaningful_velocity_direction(self):
        self.assertTrue(body_fits_segment_entry(
            window(), self.body(velocity=(0.0, 0.0, -0.08)),
            MovementMode.WALK,
        ))

    def test_physics_state_uses_per_tick_velocity_without_changing_semantics(self):
        anchor, _, _, _ = fixture()
        state = replace(
            anchor.physics_state,
            position=(1.4, 1.0, 0.5),
            velocity_blocks_per_tick=(0.01, 0.0, 0.0),
        )

        self.assertTrue(physics_fits_segment_entry(
            window(), state, MovementMode.WALK,
        ))

    def test_invalid_ranges_and_unpaired_yaw_are_rejected(self):
        with self.assertRaises(ContractViolation):
            replace(window(), minimum_speed_blocks_per_second=1.0)
        with self.assertRaises(ContractViolation):
            replace(window(), required_yaw_radians=0.0)

    def test_fixed_route_does_not_replace_directional_window_with_circle(self):
        fixture = FlatFixture()
        entry = replace(
            window(),
            minimum_speed_blocks_per_second=0.0,
        )
        route = FixedRoute("directional-handoff", (
            RoutePoint(0.5, 1.0, 0.5),
            RoutePoint(1.5, 1.0, 0.5),
        ))
        config = FixedRouteConfig(handoff_entry_window=entry)
        outside = FixedRouteController(ground_profile(), config)
        outside.start(route, fixture.frame(
            0, PlanarBodyState(0.5, 0.5, 0.0, 0.0, -math.pi / 2),
        ))

        rejected = outside.decide(fixture.frame(
            1, PlanarBodyState(1.25, 0.5, 0.0, 0.0, -math.pi / 2),
        ))

        self.assertIsNot(rejected.state, FixedRouteState.SUCCEEDED)

        inside = FixedRouteController(ground_profile(), config)
        inside.start(route, fixture.frame(
            2, PlanarBodyState(0.5, 0.5, 0.0, 0.0, -math.pi / 2),
        ))
        accepted = inside.decide(fixture.frame(
            3, PlanarBodyState(1.35, 0.5, 0.0, 0.0, -math.pi / 2),
        ))

        self.assertIs(accepted.state, FixedRouteState.SUCCEEDED)
        self.assertEqual(accepted.reason, "goal_reached_for_handoff")


if __name__ == "__main__":
    unittest.main()
