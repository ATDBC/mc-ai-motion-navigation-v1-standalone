"""F2-PC control-duration contracts for ordinary standing WALK."""
from __future__ import annotations

from dataclasses import replace
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import FixedRouteConfig
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.ground_tracking_policy import (
    GroundTrackingContext,
    GroundTrackingLimits,
    GroundTrackingPolicy,
    GroundTrackingRoute,
)
from mc2p.motion_nav.world_model import Aabb, WorldQueryCache
from tests.motion_nav.test_fixed_route_walk import FlatFixture, profile


class F2PCGroundPrecisionControlTests(unittest.TestCase):
    def _context(self, *, z: float, velocity_z: float) -> GroundTrackingContext:
        fixture = FlatFixture()
        body = PlanarBodyState(.5, z, 0., velocity_z, 0.)
        frame = fixture.frame(1, body)
        route = GroundTrackingRoute(
            ((.5, 1., .5), (.5, 1., 2.5)),
            Aabb(.25, .95, 2.20, .75, 1.05, 2.80),
        )
        return GroundTrackingContext(
            body=body,
            body_box=frame.body.body_box,
            feet_y=frame.body.position[1],
            route=route,
            progress=max(0., z - .5),
            segment_index=0,
            previous_movement=MovementV1(),
            target=(.5, 2.5),
            braking=False,
            profile=profile(),
            limits=GroundTrackingLimits.from_fixed_route_config(
                FixedRouteConfig(),
            ),
            world=frame.world,
            query_cache=WorldQueryCache(frame.world),
        )

    @staticmethod
    def _winner(context: GroundTrackingContext):
        feasible = tuple(
            candidate for candidate in GroundTrackingPolicy.candidates(context)
            if not candidate.blocked
            and not candidate.unsupported
            and not candidate.missing_cells
        )
        return min(feasible, key=GroundTrackingPolicy._candidate_key)

    def test_far_from_completion_preserves_two_tick_envelope(self):
        winner = self._winner(self._context(z=1.0, velocity_z=0.0))

        self.assertEqual(winner.movement, MovementV1(forward=1))
        self.assertEqual(winner.control_ticks, 2)

    def test_one_tick_is_selected_only_when_two_tick_stop_tail_overshoots(self):
        context = self._context(z=2.15, velocity_z=2.0)
        winner = self._winner(context)

        self.assertEqual(winner.movement, MovementV1(forward=1))
        self.assertEqual(winner.control_ticks, 1)
        bounds = context.route.completion_bounds
        self.assertIsNotNone(bounds)
        stopped = winner.predicted_states[-1]
        self.assertTrue(
            bounds.min_x <= stopped.x <= bounds.max_x
            and bounds.min_y <= context.feet_y <= bounds.max_y
            and bounds.min_z <= stopped.z <= bounds.max_z
        )

    def test_one_tick_still_includes_the_complete_neutral_stop_tail(self):
        context = self._context(z=2.15, velocity_z=2.0)
        winner = self._winner(context)

        self.assertEqual(winner.control_ticks, 1)
        self.assertGreater(len(winner.predicted_states), winner.control_ticks)
        stopped = winner.predicted_states[-1]
        self.assertLessEqual(
            (stopped.velocity_x**2 + stopped.velocity_z**2) ** .5,
            context.limits.stopped_speed_blocks_per_second,
        )

    def test_configured_single_tick_does_not_silently_expand_to_two(self):
        context = self._context(z=1.0, velocity_z=0.0)
        context = replace(
            context,
            limits=replace(context.limits, input_lease_ticks=1),
        )

        winner = self._winner(context)

        self.assertEqual(winner.control_ticks, 1)


if __name__ == "__main__":
    unittest.main()
