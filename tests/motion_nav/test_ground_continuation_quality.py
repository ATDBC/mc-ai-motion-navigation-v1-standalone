"""Ground preparation must follow its actual projected input and new proof."""
from dataclasses import replace
import math
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.fixed_route import (
    FixedRoute, FixedRouteController, FixedRouteState, GroundHandoffTarget, RoutePoint,
)
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.ground_traversal import GroundTraversalStatus, verify_ground_traversal
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.segment_entry import MotionContinuationRequirement, SegmentEntryWindow
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp, WorldKnowledge
from tests.motion_nav.test_continuous_height_execution import frame_from_state
from tests.motion_nav.test_fixed_route_walk import FlatFixture, profile
from tests.motion_nav.test_ground_traversal import traversal_fixture


class GroundPreparationInputTests(unittest.TestCase):
    def test_proved_ground_consumes_prepared_neutral_instead_of_accelerating(self):
        entry, route, world = traversal_fixture()
        plan = verify_ground_traversal(
            entry, route, world, profile(), maximum_ticks=80,
        ).plan
        self.assertIsNotNone(plan)
        state = replace(entry, position=(.5, 1.5, 1.65))
        frame = frame_from_state(state, world, 0)
        frame = replace(frame, body=replace(frame.body, movement_tick_id=0))
        controller = FixedRouteController(profile())
        controller.start(route, frame, traversal_plan=plan)
        controller.set_handoff_target(GroundHandoffTarget(
            state.position, 1, (MovementV1(),), 3,
            movement_yaws_radians=(0.0,), mode=MovementMode.WALK,
        ))

        decision = controller.decide(frame, physics_state=state)

        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.handoff_disposition.value, "consumed")

    def test_ordinary_ground_reports_that_the_prepared_input_was_consumed(self):
        fixture = FlatFixture()
        frame = fixture.frame(0, PlanarBodyState(.5, .9, 0, 0, 0))
        frame = replace(frame, body=replace(frame.body, movement_tick_id=0))
        controller = FixedRouteController(profile())
        controller.start(FixedRoute("prepare-ordinary", (
            RoutePoint(.5, 1, .5), RoutePoint(.5, 1, 1.5),
        )), frame)
        controller.set_handoff_target(GroundHandoffTarget(
            frame.body.position, 1, (MovementV1(),), 3,
            movement_yaws_radians=(0.0,), mode=MovementMode.WALK,
        ))

        decision = controller.decide(frame)

        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.handoff_disposition.value, "consumed")

    def test_ordinary_preparation_rejects_changed_yaw_mode_and_strict_input(self):
        for hint_changes in (
            {"movement_yaws_radians": (math.pi / 2,)},
            {"mode": MovementMode.SPRINT},
            {"movements": (MovementV1(jump=True),)},
            {"latest_tick": 1, "first_tick": 1},
        ):
            with self.subTest(hint=hint_changes):
                fixture = FlatFixture()
                frame = fixture.frame(0, PlanarBodyState(.5, .9, 0, 0, 0))
                tick = 2 if "latest_tick" in hint_changes else 0
                frame = replace(frame, body=replace(frame.body, movement_tick_id=tick))
                controller = FixedRouteController(profile())
                controller.start(FixedRoute("reject-ordinary", (
                    RoutePoint(.5, 1, .5), RoutePoint(.5, 1, 1.5),
                )), frame)
                hint = GroundHandoffTarget(frame.body.position, 1, (MovementV1(),), 3,
                    movement_yaws_radians=(0.0,), mode=MovementMode.WALK)
                controller.set_handoff_target(replace(hint, **hint_changes))

                decision = controller.decide(frame)

                self.assertEqual(decision.handoff_disposition.value, "rejected")
                self.assertFalse(decision.movement.jump)

    def test_proved_preparation_rejects_changed_yaw_and_cancellation(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                entry, route, world = traversal_fixture()
                plan = verify_ground_traversal(entry, route, world, profile(), maximum_ticks=80).plan
                state = replace(entry, position=(.5, 1.5, 1.65))
                frame = frame_from_state(state, world, 0)
                frame = replace(frame, body=replace(frame.body, movement_tick_id=0))
                controller = FixedRouteController(profile())
                controller.start(route, frame, traversal_plan=plan)
                controller.set_handoff_target(GroundHandoffTarget(state.position, 1,
                    (MovementV1(forward=1),), 3,
                    movement_yaws_radians=(0.0 if cancel else math.pi / 2,), mode=MovementMode.WALK))
                if cancel:
                    controller.cancel()

                decision = controller.decide(frame, physics_state=state)

                self.assertEqual(decision.handoff_disposition.value, "rejected")

    def test_ordinary_prepared_forward_still_cannot_release_into_a_pit(self):
        fixture = FlatFixture()
        fixture.world.confirm_air(ObservationStamp(fixture.session, 1, 1, "test-clock", 1),
            tuple((x, 0, z) for x in (-1, 0, 1) for z in range(1, 5)))
        frame = fixture.frame(0, PlanarBodyState(.5, .74, 0, 0, 0))
        frame = replace(frame, body=replace(frame.body, movement_tick_id=0))
        controller = FixedRouteController(profile())
        controller.start(FixedRoute("prepared-pit", (
            RoutePoint(.5, 1, .5), RoutePoint(.5, 1, 1.5),
        )), frame)
        controller.set_handoff_target(GroundHandoffTarget(frame.body.position, 1,
            (MovementV1(forward=1),), 3, movement_yaws_radians=(0.,), mode=MovementMode.WALK))

        decision = controller.decide(frame)

        self.assertEqual(decision.handoff_disposition.value, "rejected")
        self.assertNotEqual(decision.movement, MovementV1(forward=1))

    def test_proved_prepared_sideways_input_cannot_leave_its_corridor(self):
        entry, route, world = traversal_fixture()
        plan = verify_ground_traversal(entry, route, world, profile(), maximum_ticks=80).plan
        state = replace(entry, position=(.8, 1.5, 1.65))
        frame = frame_from_state(state, world, 0)
        frame = replace(frame, body=replace(frame.body, movement_tick_id=0))
        controller = FixedRouteController(profile())
        controller.start(route, frame, traversal_plan=plan)
        controller.set_handoff_target(GroundHandoffTarget(state.position, 1,
            (MovementV1(strafe=1),), 3, movement_yaws_radians=(0.,), mode=MovementMode.WALK))

        decision = controller.decide(frame, physics_state=state)

        self.assertEqual(decision.handoff_disposition.value, "rejected")
        self.assertNotEqual(decision.movement, MovementV1(strafe=1))


def continuation_fixture(*, hide=()):
    entry, route, _ = traversal_fixture()
    knowledge = WorldKnowledge(entry.session)
    stamp = ObservationStamp(entry.session, 0, 0, "continuation-test", 0)
    knowledge.confirm_air(stamp, tuple((x, y, z)
        for x in range(-2, 3) for y in range(-2, 7) for z in range(-2, 9)
        if (x, y, z) not in hide))
    blocks = {(x, 0, z): BlockGeometry.full_cube("minecraft:grass_block")
              for x in range(-2, 3) for z in range(-2, 9)}
    blocks[(0, 1, 1)] = BlockGeometry("minecraft:smooth_stone_slab", "boxes",
        (Aabb(0, 0, 0, 1, .5, 1),))
    blocks.update({(x, 1, z): BlockGeometry.full_cube("minecraft:stone")
                   for x in (-1, 0, 1) for z in range(2, 7)})
    knowledge.observe_blocks(stamp, {key: value for key, value in blocks.items() if key not in hide})
    window = SegmentEntryWindow((.5, 2, 2.5), (0., 1.), 0., .65, .25,
        1.9, 2.1, .5, profile().maximum_speed_blocks_per_second + .1,
        math.radians(35), frozenset({"standing"}), frozenset({MovementMode.WALK}),
        None, None, "ordinary-ground-continuation")
    return entry, route, PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET), \
        MotionContinuationRequirement(window, MovementMode.WALK, "off-center-tail")


class GroundContinuationProofTests(unittest.TestCase):
    def test_new_proof_exits_moving_and_proves_its_complete_stop_tail(self):
        entry, route, world, continuation = continuation_fixture()
        old = verify_ground_traversal(entry, route, world, profile(), maximum_ticks=80).plan

        result = verify_ground_traversal(entry, route, world, profile(), maximum_ticks=80,
                                       continuation=continuation)

        self.assertIs(result.status, GroundTraversalStatus.VERIFIED, result.reasons)
        plan = result.plan
        self.assertEqual(plan.continuation, continuation)
        self.assertTrue(continuation.accepts(plan.trajectory[-1]))
        self.assertGreater(math.hypot(*plan.trajectory[-1].velocity_blocks_per_tick[::2]) * 20, .5)
        self.assertLess(plan.estimated_ticks, old.estimated_ticks)
        self.assertEqual(plan.neutral_stop_trajectory[0], plan.trajectory[-1])
        final = plan.neutral_stop_trajectory[-1]
        self.assertTrue(final.on_ground)
        self.assertEqual(final.velocity_blocks_per_tick[::2], (0., 0.))
        self.assertIn((0, 2, 3), plan.dependencies)

    def test_missing_stop_tail_cell_cannot_publish_a_moving_exit(self):
        entry, route, world, continuation = continuation_fixture(hide=((0, 2, 3),))

        result = verify_ground_traversal(entry, route, world, profile(), maximum_ticks=80,
                                       continuation=continuation)

        self.assertIs(result.status, GroundTraversalStatus.NEEDS_WORLD)
        self.assertIn((0, 2, 3), result.missing_cells)
        self.assertIsNone(result.plan)

    def test_old_stopping_proof_does_not_gain_a_moving_exit_by_configuration(self):
        entry, route, world, continuation = continuation_fixture()
        old = verify_ground_traversal(entry, route, world, profile(), maximum_ticks=80).plan
        controller = FixedRouteController(profile())

        with self.assertRaises(ContractViolation):
            controller.start(route, frame_from_state(entry, world, 0),
                             traversal_plan=old, continuation=continuation)

    def test_new_proved_ground_controller_hands_off_with_actual_momentum(self):
        entry, route, world, continuation = continuation_fixture()
        plan = verify_ground_traversal(entry, route, world, profile(), maximum_ticks=80,
                                       continuation=continuation).plan
        controller = FixedRouteController(profile())
        controller.start(route, frame_from_state(entry, world, 0),
                         traversal_plan=plan, continuation=continuation)
        state = entry
        for sequence in range(1, 80):
            decision = controller.decide(frame_from_state(state, world, sequence), physics_state=state)
            if decision.state is FixedRouteState.SUCCEEDED:
                self.assertTrue(continuation.accepts(state))
                self.assertGreater(math.hypot(*state.velocity_blocks_per_tick[::2]) * 20, .5)
                return
            self.assertEqual(decision.state, FixedRouteState.RUNNING, decision.reason)
            projected = project_movement_command(state, decision.movement)
            state = step(state, projected.tick_input, world, JAVA_1_21_RULESET).next_state
            self.assertIsNotNone(state)
        self.fail("new continuation proof did not hand off")

    def test_new_continuation_does_not_replace_the_real_stop_required_by_cancel(self):
        entry, route, world, continuation = continuation_fixture()
        continuation = replace(continuation, entry_window=replace(continuation.entry_window,
            minimum_longitudinal_offset_blocks=.30, maximum_longitudinal_offset_blocks=1.1))
        plan = verify_ground_traversal(entry, route, world, profile(), maximum_ticks=80,
                                       continuation=continuation).plan
        controller = FixedRouteController(profile())
        controller.start(route, frame_from_state(entry, world, 0),
                         traversal_plan=plan, continuation=continuation)
        state = entry
        sequence = 1
        while not continuation.accepts(state) and sequence < 60:
            decision = controller.decide(frame_from_state(state, world, sequence), physics_state=state)
            self.assertEqual(decision.state, FixedRouteState.RUNNING, decision.reason)
            projected = project_movement_command(state, decision.movement)
            state = step(state, projected.tick_input, world, JAVA_1_21_RULESET).next_state
            sequence += 1
        self.assertTrue(continuation.accepts(state))
        self.assertGreater(math.hypot(*state.velocity_blocks_per_tick[::2]) * 20, .5)
        controller.cancel()
        for sequence in range(sequence, sequence + 25):
            decision = controller.decide(frame_from_state(state, world, sequence), physics_state=state)
            if decision.state is FixedRouteState.CANCELLED:
                self.assertLessEqual(math.hypot(*state.velocity_blocks_per_tick[::2]) * 20, .1)
                self.assertTrue(state.on_ground)
                return
            self.assertEqual(decision.state, FixedRouteState.CANCELLING, decision.reason)
            projected = project_movement_command(state, decision.movement)
            state = step(state, projected.tick_input, world, JAVA_1_21_RULESET).next_state
        self.fail("cancellation did not stop the actual body")


if __name__ == "__main__":
    unittest.main()
