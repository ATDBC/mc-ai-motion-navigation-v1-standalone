from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.step_transition import (
    StepController, StepProfile, StepState, load_step_profile, query_step,
)
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.support_surfaces import HorizontalRegion, query_support_surfaces
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp
from tests.motion_nav.test_b07_support_surfaces import surface_world


def profile() -> StepProfile:
    return StepProfile(
        "fabric-1_21-step-b07-test", "fabric-1_21-motion-v1",
        .6, .6, 4.370969732409911, .1, .28, .08, 30, .35,
    )


def step_world(low_height=.5):
    return surface_world({
        (0, 0, 0): BlockGeometry(
            "minecraft:smooth_stone_slab", "boxes",
            (Aabb(0, 0, 0, 1, low_height, 1),),
        ),
        (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
    })


def surfaces(world):
    start = query_support_surfaces(world.view(), 0, 0, 0.0, 2.0).surfaces[0]
    end = query_support_surfaces(world.view(), 1, 0, 0.0, 2.0).surfaces[0]
    return start, end


def frame(world, sequence, position, *, velocity=(0.0, 0.0, 0.0),
          yaw=math.pi * 1.5, on_ground=True):
    session = world.session
    observed = ObservationStamp(
        session, sequence, sequence, "test-clock", sequence * 50_000_000,
    )
    x, y, z = position
    body = BodyState(
        session, sequence, observed, position, velocity, yaw, 0.0, "standing",
        Aabb(x - .3, y, z - .3, x + .3, y + 1.8, z + .3),
        on_ground, False, False,
    )
    return NavigationFrame(session, body, world.view(), "fabric")


class B07StepTransitionTests(unittest.TestCase):
    def test_repository_profile_is_bound_to_the_frozen_environment(self):
        root = Path(__file__).resolve().parents[2]
        environment = load_frozen_environment(
            root / "config/motion-navigation/environment-v1.json"
        )
        loaded = load_step_profile(
            root / "config/motion-navigation/step-b07-v1.json",
            environment=environment,
        )

        self.assertEqual(loaded.environment_id, environment.environment_id)
        self.assertEqual(loaded.minecraft_version, "1.21")
        self.assertEqual(loaded.tick_seconds, .05)
        self.assertEqual(loaded.maximum_up_height_blocks, .5)

    def test_half_block_up_and_down_are_feasible_without_jump(self):
        world = step_world()
        low, high = surfaces(world)

        upward = query_step(world.view(), low, high, profile())
        downward = query_step(world.view(), high, low, profile())

        self.assertIs(upward.status, QueryStatus.FEASIBLE)
        self.assertEqual(upward.direction, "up")
        self.assertAlmostEqual(upward.height_delta_blocks, .5)
        self.assertIs(downward.status, QueryStatus.FEASIBLE)
        self.assertEqual(downward.direction, "down")
        self.assertTrue(upward.dependencies)

    def test_height_outside_profile_is_unsupported(self):
        world = step_world(.25)
        low, high = surfaces(world)

        result = query_step(world.view(), low, high, profile())

        self.assertIs(result.status, QueryStatus.UNSUPPORTED)
        self.assertEqual(result.reason_code, "step_height_outside_profile")

    def test_controller_uses_observed_endpoint_and_never_sends_jump(self):
        world = step_world()
        low, high = surfaces(world)
        controller = StepController(profile())
        initial = frame(world, 0, low.position)
        controller.start(low, high, initial)

        moving = controller.decide(initial)

        self.assertIs(moving.state, StepState.MOVING)
        self.assertNotEqual(moving.movement, MovementV1())
        self.assertFalse(moving.movement.jump)
        landed = frame(world, 1, high.position)
        complete = controller.decide(landed)
        self.assertIs(complete.state, StepState.COMPLETE)

    def test_controller_rechecks_changed_step_geometry_before_sending_more_input(self):
        world = step_world()
        low, high = surfaces(world)
        controller = StepController(profile())
        initial = frame(world, 0, low.position)
        controller.start(low, high, initial)
        self.assertIs(controller.decide(initial).state, StepState.MOVING)
        stamp = ObservationStamp(
            world.session, 2, 2, "test-clock", 100_000_000,
        )
        world.observe_blocks(stamp, {
            (1, 1, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        changed = replace(
            frame(world, 2, low.position), changed_cells=((1, 1, 0),),
        )

        decision = controller.decide(changed)

        self.assertIs(decision.state, StepState.BLOCKED)
        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.reason_code, "step_blocked")

    def test_controller_rejects_a_body_that_is_not_on_the_declared_start_surface(self):
        world = step_world()
        low, high = surfaces(world)
        controller = StepController(profile())
        wrong_start = frame(world, 0, high.position)
        controller.start(low, high, wrong_start)

        decision = controller.decide(wrong_start)

        self.assertIs(decision.state, StepState.UNSUPPORTED)
        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.reason_code, "invalid_entry_surface")

    def test_controller_allows_the_body_to_overhang_a_narrow_start_tread(self):
        world = step_world()
        low, high = surfaces(world)
        narrow = replace(
            low,
            position=(.5, low.position[1], .2),
            region=HorizontalRegion(0.0, 0.0, 1.0, .5),
        )
        controller = StepController(profile())
        initial = frame(world, 0, narrow.position)
        controller.start(narrow, high, initial)

        decision = controller.decide(initial)

        self.assertIs(decision.state, StepState.MOVING)
        self.assertNotEqual(decision.movement, MovementV1())

    def test_cancel_waits_for_observed_ground_stop(self):
        world = step_world()
        low, high = surfaces(world)
        controller = StepController(profile())
        initial = frame(world, 0, low.position)
        controller.start(low, high, initial)
        controller.decide(initial)
        controller.cancel()

        slowing = controller.decide(frame(
            world, 1, (.7, .6, .5), velocity=(.5, 0.0, 0.0),
        ))
        stopped = controller.decide(frame(
            world, 2, (.72, .6, .5), velocity=(0.0, 0.0, 0.0),
        ))

        self.assertIs(slowing.state, StepState.CANCELLING)
        self.assertEqual(slowing.movement, MovementV1())
        self.assertIs(stopped.state, StepState.CANCELLED)

    def test_world_session_change_and_input_loss_fail_closed(self):
        world = step_world()
        low, high = surfaces(world)
        initial = frame(world, 0, low.position)

        lost = StepController(profile())
        lost.start(low, high, initial)
        self.assertIs(lost.decide(initial, input_confirmed=False).state,
                      StepState.INPUT_LOST)

        changed = StepController(profile())
        changed.start(low, high, initial)
        replacement = replace(initial, session=type(world.session)("replacement"))
        decision = changed.decide(replacement)
        self.assertIs(decision.state, StepState.FAILED)
        self.assertEqual(decision.reason_code, "world_session_changed")

    def test_airborne_input_loss_waits_for_observed_safe_landing(self):
        world = step_world()
        low, high = surfaces(world)
        controller = StepController(profile())
        initial = frame(world, 0, low.position)
        controller.start(low, high, initial)
        controller.decide(initial)

        lost = controller.decide(frame(
            world, 1, (.9, .8, .5), velocity=(1.0, -.4, 0.0),
            on_ground=False,
        ), input_confirmed=False)
        landed = controller.decide(frame(world, 2, high.position))

        self.assertIs(lost.state, StepState.CANCELLING)
        self.assertEqual(lost.movement, MovementV1())
        self.assertIs(landed.state, StepState.INPUT_LOST)

    def test_step_down_releases_input_after_crossing_edge_until_landing(self):
        world = step_world()
        low, high = surfaces(world)
        controller = StepController(profile())
        initial = frame(world, 0, high.position)
        controller.start(high, low, initial)
        controller.decide(initial)

        airborne = controller.decide(frame(
            world, 1, (low.position[0] + .05, low.position[1] + .3,
                       low.position[2]),
            velocity=(.4, -1.0, 0.0), on_ground=False,
        ))

        self.assertIs(airborne.state, StepState.VERIFY_LANDING)
        self.assertEqual(airborne.movement, MovementV1())


if __name__ == "__main__":
    unittest.main()
