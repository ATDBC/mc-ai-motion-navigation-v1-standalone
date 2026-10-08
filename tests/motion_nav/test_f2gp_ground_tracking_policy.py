"""F2-GP shared ordinary-ground tracking policy contracts."""
from __future__ import annotations

from dataclasses import replace
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import (
    FixedRoute, FixedRouteConfig, FixedRouteController, RoutePoint,
)
from mc2p.motion_nav.ground_tracking_policy import (
    GROUND_TRACKING_POLICY_VERSION,
    GroundTrackingRolloutStatus,
    GroundTrackingContext,
    GroundTrackingLimits,
    GroundTrackingPolicy,
    GroundTrackingRoute,
    candidate_ground_tracking_routes,
    rollout_ground_tracking_route,
)
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from tests.motion_nav.test_ground_traversal import traversal_fixture
from mc2p.motion_nav.world_model import WorldQueryCache
from tests.motion_nav.test_fixed_route_walk import FlatFixture, profile


class GroundTrackingPolicyContractTests(unittest.TestCase):
    def _started(self):
        fixture = FlatFixture()
        route = FixedRoute("f2gp-route", (
            RoutePoint(.5, 1., .5), RoutePoint(.5, 1., 2.5),
        ))
        config = FixedRouteConfig()
        controller = FixedRouteController(profile(), config)
        frame = fixture.frame(1, PlanarBodyState(.5, .5, 0., 0., 0.))
        controller.start(route, frame)
        return fixture, route, config, controller, frame

    def test_policy_version_is_explicit(self):
        self.assertEqual(GROUND_TRACKING_POLICY_VERSION,
                         "ground-tracking-policy-v1")

    def test_policy_candidate_order_matches_fixed_route(self):
        fixture, route, config, controller, frame = self._started()
        body = controller._planar(frame)
        target = (.5, 1.15)
        cache = WorldQueryCache(frame.world)
        legacy = controller._ranked_tracking_candidates(
            frame, body, target, cache,
        )
        context = GroundTrackingContext(
            body=body,
            body_box=frame.body.body_box,
            feet_y=frame.body.position[1],
            route=GroundTrackingRoute.from_fixed_route(route),
            progress=controller._progress,
            segment_index=controller._segment_index,
            previous_movement=controller._previous_movement,
            target=target,
            braking=False,
            profile=controller.profile,
            limits=GroundTrackingLimits.from_fixed_route_config(config),
            world=frame.world,
            query_cache=cache,
        )
        actual = GroundTrackingPolicy.candidates(context)
        self.assertEqual(
            [(item.movement, item.score, item.progress_gain,
              item.missing_cells, item.blocked, item.unsupported)
             for item in actual],
            [(item.movement, item.score, item.progress_gain,
              item.missing, item.blocked, item.unsupported)
             for item in legacy],
        )

    def test_policy_does_not_authorize_jump_sneak_or_sprint(self):
        fixture, route, config, controller, frame = self._started()
        context = GroundTrackingContext(
            body=controller._planar(frame),
            body_box=frame.body.body_box,
            feet_y=frame.body.position[1],
            route=GroundTrackingRoute.from_fixed_route(route),
            progress=0., segment_index=0,
            previous_movement=MovementV1(), target=(.5, 1.15),
            braking=False, profile=controller.profile,
            limits=GroundTrackingLimits.from_fixed_route_config(config),
            world=frame.world, query_cache=WorldQueryCache(frame.world),
        )
        for candidate in GroundTrackingPolicy.candidates(context):
            self.assertFalse(candidate.movement.jump)
            self.assertFalse(candidate.movement.sneak)
            self.assertFalse(candidate.movement.sprint)

    def test_policy_rejects_cache_from_another_world(self):
        fixture, route, config, controller, frame = self._started()
        other = FlatFixture().frame(
            1, PlanarBodyState(.5, .5, 0., 0., 0.),
        )
        with self.assertRaises(Exception):
            GroundTrackingContext(
                body=controller._planar(frame), body_box=frame.body.body_box,
                feet_y=frame.body.position[1],
                route=GroundTrackingRoute.from_fixed_route(route), progress=0.,
                segment_index=0, previous_movement=MovementV1(),
                target=(.5, 1.15), braking=False, profile=controller.profile,
                limits=GroundTrackingLimits.from_fixed_route_config(config),
                world=frame.world, query_cache=WorldQueryCache(other.world),
            )

    def test_candidate_routes_include_straight_and_both_cardinal_l_shapes(self):
        routes = candidate_ground_tracking_routes(
            (0.5, 1.0, 0.5), (2.5, 1.0, 3.5), "f2gp",
        )
        self.assertEqual(tuple(kind for kind, _ in routes), (
            "straight", "x_then_z", "z_then_x",
        ))
        degenerate = candidate_ground_tracking_routes(
            (.5, 1., .5), (.5, 1., 3.5), "f2gp",
        )
        self.assertEqual(tuple(kind for kind, _ in degenerate), ("straight",))

    def test_rollout_uses_policy_until_stopped_inside_completion(self):
        fixture, route, config, _, frame = self._started()
        state, _, _ = traversal_fixture()
        state = replace(
            state, session=frame.session, position=(.5, 1., .5),
            velocity_blocks_per_tick=(0., 0., 0.), yaw_radians=0.,
        )
        completion = GroundCompletionRegion(
            bounds=__import__("mc2p.motion_nav.world_model", fromlist=["Aabb"]).Aabb(
                .25, .95, 2.20, .75, 1.05, 2.80,
            ),
            reference_point=(.5, 1., 2.5), support_height=1.,
            surface_identity=(0, 2, 1, 0), dependencies=((0, 0, 2),),
        )
        result = rollout_ground_tracking_route(
            entry_state=state,
            route=GroundTrackingRoute.from_fixed_route(route),
            completion_region=completion,
            world=frame.world,
            profile=profile(),
            limits=GroundTrackingLimits.from_fixed_route_config(config),
            maximum_ticks=100,
        )
        self.assertIs(result.status, GroundTrackingRolloutStatus.COMPLETE)
        self.assertGreater(result.estimated_ticks, 0)
        self.assertEqual(len(result.commands), result.estimated_ticks)
        self.assertTrue(completion.contains(result.final_position))
        self.assertEqual(result.policy_version, GROUND_TRACKING_POLICY_VERSION)


if __name__ == "__main__":
    unittest.main()
