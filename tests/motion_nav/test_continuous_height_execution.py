from dataclasses import replace
import unittest

from mc2p.motion_nav.fixed_route import FixedRouteController, FixedRouteState
from mc2p.motion_nav.ground_traversal import verify_ground_traversal
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.world_model import ObservationStamp
from tests.motion_nav.test_fixed_route_walk import profile
from tests.motion_nav.test_ground_traversal import traversal_fixture


def frame_from_state(state, world, sequence):
    stamp = ObservationStamp(
        state.session, sequence, sequence, "continuous-height-test", 50_000_000,
    )
    body = BodyState(
        state.session, sequence, stamp, state.position,
        tuple(value * 20.0 for value in state.velocity_blocks_per_tick),
        state.yaw_radians, state.pitch_radians, state.pose, state.body_box,
        state.on_ground, state.horizontal_collision, state.vertical_collision,
        state.sprinting, state.sneaking, state.food_points,
        state.saturation_points, state.swimming, state.submerged_in_water,
        state.game_mode, state.fall_distance_blocks, (), state.climbing,
        state.fall_flying, state.flying, state.allow_flying,
        state.is_using_item,
    )
    return NavigationFrame(state.session, body, world._world, "test")


class ContinuousHeightExecutionTests(unittest.TestCase):
    def _run(self, state, route, world, *, delay_first_release=False):
        proof = verify_ground_traversal(
            state, route, world, profile(), maximum_ticks=80,
        ).plan
        self.assertIsNotNone(proof)
        controller = FixedRouteController(profile())
        controller.start(route, frame_from_state(state, world, 0),
                         traversal_plan=proof)
        reasons = []
        airborne = False
        previous_movement = None
        delayed = False
        for sequence in range(1, 90):
            decision = controller.decide(
                frame_from_state(state, world, sequence),
            )
            reasons.append(decision.reason)
            if decision.state is FixedRouteState.SUCCEEDED:
                return state, reasons, airborne
            self.assertNotIn(decision.state, {
                FixedRouteState.FAILED, FixedRouteState.UNSUPPORTED,
                FixedRouteState.BLOCKED, FixedRouteState.INPUT_LOST,
            }, msg=(sequence, decision.reason, state.position,
                    state.velocity_blocks_per_tick, state.on_ground))
            applied = decision.movement
            if (delay_first_release and not delayed
                    and previous_movement is not None
                    and previous_movement.forward != 0
                    and decision.movement.forward == 0):
                applied = previous_movement
                delayed = True
            projected = project_movement_command(state, applied)
            self.assertIsNotNone(projected.tick_input)
            calculated = step(
                state, projected.tick_input, world, JAVA_1_21_RULESET,
            )
            self.assertIsNotNone(calculated.next_state)
            state = calculated.next_state
            airborne = airborne or not state.on_ground
            previous_movement = decision.movement
        self.fail("continuous-height route did not finish")

    def test_two_half_block_ascents_execute_without_static_step_stop(self):
        state, route, world = traversal_fixture()

        final, reasons, _ = self._run(state, route, world)

        self.assertAlmostEqual(final.position[1], 2.0)
        self.assertIn("tracking_verified_ground_traversal", reasons)
        self.assertNotIn("ordinary_ground_state_lost", reasons)

    def test_two_half_block_descents_allow_planned_short_airborne_motion(self):
        state, route, world = traversal_fixture()
        downhill = replace(
            route,
            route_id="continuous-downhill",
            points=tuple(reversed(route.points)),
        )
        state = replace(
            state,
            position=(0.5, 2.0, 2.5),
            yaw_radians=3.141592653589793,
        )

        final, reasons, airborne = self._run(state, downhill, world)

        self.assertTrue(airborne)
        self.assertAlmostEqual(final.position[1], 1.0)
        self.assertNotIn("ordinary_ground_state_lost", reasons)

    def test_one_tick_late_release_stays_inside_verified_tracking_envelope(self):
        state, route, world = traversal_fixture()

        final, reasons, _ = self._run(
            state, route, world, delay_first_release=True,
        )

        self.assertAlmostEqual(final.position[1], 2.0)
        self.assertNotIn("ground_traversal_left_verified_envelope", reasons)


if __name__ == "__main__":
    unittest.main()
