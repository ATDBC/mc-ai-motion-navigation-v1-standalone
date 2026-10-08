"""F2-SC contracts separating recoverable ground safety from completion."""
from __future__ import annotations

from dataclasses import replace
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import (
    FixedRoute,
    FixedRouteConfig,
    FixedRouteController,
    FixedRouteState,
    RoutePoint,
)
from mc2p.motion_nav.ground_motion import (
    GroundControl,
    PlanarBodyState,
    predict_ground,
)
from mc2p.motion_nav.ground_route_execution import (
    GroundCompletionRegion,
    GroundRouteExecutionContract,
)
from mc2p.motion_nav.ground_tracking_policy import (
    GroundTrackingContext,
    GroundTrackingLimits,
    GroundTrackingPolicy,
    GroundTrackingRolloutStatus,
    GroundTrackingRoute,
    rollout_ground_tracking_route,
)
from mc2p.motion_nav.physics_types import PhysicsState
from mc2p.motion_nav.world_model import Aabb, WorldQueryCache
from tests.motion_nav.test_fixed_route_walk import FlatFixture, profile
from tests.motion_nav.test_ground_traversal import traversal_fixture


class F2SCSafeTailCompletionTests(unittest.TestCase):
    def _context(
        self,
        *,
        z: float,
        velocity_z: float,
        completion: tuple[float, float] = (2.20, 2.80),
        input_lease_ticks: int = 2,
    ) -> tuple[FlatFixture, GroundTrackingContext]:
        fixture = FlatFixture()
        body = PlanarBodyState(.5, z, 0., velocity_z, 0.)
        frame = fixture.frame(1, body)
        route = GroundTrackingRoute(
            ((.5, 1., .5), (.5, 1., 2.5)),
            Aabb(.25, .95, completion[0], .75, 1.05, completion[1]),
        )
        limits = replace(
            GroundTrackingLimits.from_fixed_route_config(FixedRouteConfig()),
            input_lease_ticks=input_lease_ticks,
        )
        return fixture, GroundTrackingContext(
            body=body,
            body_box=frame.body.body_box,
            feet_y=frame.body.position[1],
            route=route,
            progress=max(0., min(2., z-.5)),
            segment_index=0,
            previous_movement=MovementV1(),
            target=(.5, 2.5),
            braking=True,
            profile=profile(),
            limits=limits,
            world=frame.world,
            query_cache=WorldQueryCache(frame.world),
        )

    @staticmethod
    def _completion(context: GroundTrackingContext) -> GroundCompletionRegion:
        bounds = context.route.completion_bounds
        assert bounds is not None
        return GroundCompletionRegion(
            bounds=bounds,
            reference_point=(.5, context.feet_y, 2.5),
            support_height=context.feet_y,
            surface_identity=(0, 2, 1, 0),
            dependencies=((0, 0, 2),),
        )

    def _safe_tail(self, context: GroundTrackingContext, movement: MovementV1, ticks: int):
        self.assertTrue(hasattr(GroundTrackingPolicy, "safe_tail"))
        return GroundTrackingPolicy.safe_tail(context, movement, ticks)

    def _completion_evaluation(self, context: GroundTrackingContext):
        self.assertTrue(hasattr(GroundTrackingPolicy, "completion"))
        return GroundTrackingPolicy.completion(
            context,
            maximum_speed_blocks_per_second=(
                context.limits.stopped_speed_blocks_per_second
            ),
        )

    def test_safe_tail_can_stop_outside_completion_without_becoming_unsafe(self):
        _, context = self._context(z=1.0, velocity_z=0.0)

        result = self._safe_tail(context, MovementV1(forward=1), 1)

        self.assertFalse(result.blocked)
        self.assertFalse(result.unsupported)
        self.assertEqual(result.missing_cells, ())
        self.assertFalse(self._completion(context).contains((
            result.predicted_states[-1].x,
            context.feet_y,
            result.predicted_states[-1].z,
        )))

    def test_two_tick_safe_tail_records_zero_one_and_two_tick_prefixes(self):
        _, context = self._context(z=1.0, velocity_z=0.0)

        result = self._safe_tail(context, MovementV1(forward=1), 2)

        self.assertEqual(result.control_ticks, 2)
        self.assertEqual(result.verified_prefix_ticks, (0, 1, 2))

    def test_one_tick_safe_tail_records_zero_and_one_tick_prefixes(self):
        _, context = self._context(z=1.0, velocity_z=0.0)

        result = self._safe_tail(context, MovementV1(forward=1), 1)

        self.assertEqual(result.control_ticks, 1)
        self.assertEqual(result.verified_prefix_ticks, (0, 1))

    def test_prediction_entering_completion_does_not_complete_current_outside_state(self):
        _, context = self._context(z=2.15, velocity_z=2.0)

        result = self._completion_evaluation(context)

        self.assertFalse(result.current_inside)
        self.assertTrue(result.predicted_stop_inside)
        self.assertFalse(result.neutral_stop_inside)
        self.assertFalse(result.satisfied)

    def test_current_inside_but_neutral_tail_exits_is_not_complete(self):
        _, context = self._context(z=2.75, velocity_z=1.0)

        result = self._completion_evaluation(context)

        self.assertTrue(result.current_inside)
        self.assertFalse(result.neutral_stop_inside)
        self.assertFalse(result.satisfied)

    def test_stopped_current_state_and_neutral_tail_inside_is_complete(self):
        _, context = self._context(z=2.5, velocity_z=0.0)

        result = self._completion_evaluation(context)

        self.assertTrue(result.current_inside)
        self.assertTrue(result.speed_satisfied)
        self.assertTrue(result.neutral_stop_inside)
        self.assertTrue(result.satisfied)

    def test_terminal_policy_uses_two_ticks_far_away_and_one_tick_near_braking_point(self):
        _, far = self._context(z=1.0, velocity_z=0.0)
        _, near = self._context(z=2.15, velocity_z=2.0)

        far_winner = min(
            (item for item in GroundTrackingPolicy.candidates(far)
             if not item.blocked and not item.unsupported and not item.missing_cells),
            key=GroundTrackingPolicy._candidate_key,
        )
        near_winner = min(
            (item for item in GroundTrackingPolicy.candidates(near)
             if not item.blocked and not item.unsupported and not item.missing_cells),
            key=GroundTrackingPolicy._candidate_key,
        )

        self.assertEqual(far_winner.movement, MovementV1(forward=1))
        self.assertEqual(far_winner.control_ticks, 2)
        self.assertEqual(near_winner.movement, MovementV1(forward=1))
        self.assertEqual(near_winner.control_ticks, 1)

    def test_rollout_advances_one_player_tick_even_when_decision_allows_two(self):
        fixture, context = self._context(z=1.0, velocity_z=0.0)
        state, _, _ = traversal_fixture()
        state = replace(
            state,
            session=fixture.session,
            position=(.5, 1., 1.0),
            velocity_blocks_per_tick=(0., 0., 0.),
            yaw_radians=0.,
        )
        completion = self._completion(context)

        result = rollout_ground_tracking_route(
            entry_state=state,
            route=context.route,
            completion_region=completion,
            world=fixture.world.view(),
            profile=context.profile,
            limits=context.limits,
            maximum_ticks=1,
        )
        one_tick = predict_ground(
            context.body,
            (GroundControl(1, 0, context.body.yaw_radians),),
            context.profile,
        )[-1]

        self.assertIs(result.status, GroundTrackingRolloutStatus.BUDGET_EXHAUSTED)
        self.assertEqual(result.estimated_ticks, 1)
        self.assertEqual(len(result.commands), 1)
        self.assertAlmostEqual(result.final_position[2], one_tick.z, places=9)

    def test_overshoot_can_choose_a_proved_reverse_correction(self):
        _, context = self._context(z=2.90, velocity_z=0.0)

        winner = min(
            (item for item in GroundTrackingPolicy.candidates(context)
             if not item.blocked and not item.unsupported and not item.missing_cells),
            key=GroundTrackingPolicy._candidate_key,
        )

        self.assertEqual(winner.movement, MovementV1(forward=-1))
        self.assertGreater(winner.verified_prefix_ticks[-1], 0)

    def _fixed_route_decision(self, *, z: float, velocity_z: float):
        fixture, context = self._context(z=z, velocity_z=velocity_z)
        completion = self._completion(context)
        contract = GroundRouteExecutionContract.for_completion(
            completion, completion.dependencies, context.profile.profile_id,
        )
        route = FixedRoute(
            "f2sc-formal-ground",
            (RoutePoint(.5, 1., .5), RoutePoint(.5, 1., 2.5)),
            contract,
        )
        controller = FixedRouteController(context.profile, FixedRouteConfig())
        frame = fixture.frame(1, context.body)
        controller.start(route, fixture.frame(0, context.body))
        return controller, frame, controller.decide(frame)

    def test_fixed_route_submits_the_policy_control_duration(self):
        _, _, far = self._fixed_route_decision(z=1.0, velocity_z=0.0)
        _, _, near = self._fixed_route_decision(z=2.15, velocity_z=2.0)

        self.assertEqual(far.input_lease_ticks, 2)
        self.assertEqual(near.input_lease_ticks, 1)

    def test_fixed_route_reports_complete_only_after_current_neutral_tail_fits(self):
        _, _, moving = self._fixed_route_decision(z=2.75, velocity_z=1.0)
        _, _, stopped = self._fixed_route_decision(z=2.5, velocity_z=0.0)

        self.assertIsNot(moving.state, FixedRouteState.SUCCEEDED)
        self.assertIs(stopped.state, FixedRouteState.SUCCEEDED)
        self.assertEqual(stopped.movement, MovementV1())

    def test_fixed_route_uses_terminal_policy_order_after_overshoot(self):
        _, context = self._context(z=2.90, velocity_z=0.0)
        expected = min(
            (item for item in GroundTrackingPolicy.candidates(context)
             if not item.blocked and not item.unsupported and not item.missing_cells),
            key=GroundTrackingPolicy._candidate_key,
        )
        _, _, decision = self._fixed_route_decision(z=2.90, velocity_z=0.0)

        self.assertEqual(decision.movement, expected.movement)
        self.assertEqual(decision.input_lease_ticks, expected.control_ticks)


if __name__ == "__main__":
    unittest.main()
