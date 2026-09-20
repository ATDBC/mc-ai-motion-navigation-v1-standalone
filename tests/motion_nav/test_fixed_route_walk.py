from __future__ import annotations

import json
import math
from pathlib import Path
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import (
    FixedRoute, FixedRouteConfig, FixedRouteController, FixedRouteState, RoutePoint,
)
from mc2p.motion_nav.ground_motion import (
    GroundControl, GroundMotionProfile, PlanarBodyState, predict_ground,
)
from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)
from scripts.fixed_route_runtime_core import l_corridor_wall_positions


ROOT = Path(__file__).resolve().parents[2]


def profile() -> GroundMotionProfile:
    document = json.loads((ROOT / "config/motion-navigation/ordinary-ground-v1.json").read_text("utf-8"))
    value = document["profile"]
    return GroundMotionProfile(
        0.05,
        value["acceleration_blocks_per_second2"],
        value["velocity_retention_per_tick"],
        value["maximum_speed_blocks_per_second"],
        frozenset(document["scope"]["support_materials"]),
    )


class FlatFixture:
    def __init__(self, material: str = "minecraft:grass_block") -> None:
        self.session = WorldSessionId("b03-flat")
        self.world = WorldKnowledge(self.session)
        stamp = ObservationStamp(self.session, 0, 0, "test-clock", 0)
        floors = {(x, y, z): BlockGeometry.full_cube(material)
                  for x in range(-16, 17) for y in (-1, 0) for z in range(-16, 17)}
        self.world.observe_blocks(stamp, floors)
        self.world.confirm_air(stamp, tuple(
            (x, y, z) for x in range(-16, 17) for y in (1, 2, 3) for z in range(-16, 17)
        ))

    def frame(self, sequence: int, body: PlanarBodyState, *, body_y: float = 1.0) -> NavigationFrame:
        stamp = ObservationStamp(self.session, sequence, sequence, "test-clock", sequence * 50_000_000)
        x, z = body.x, body.z
        box = Aabb(x - 0.3, body_y, z - 0.3, x + 0.3, body_y + 1.8, z + 0.3)
        value = BodyState(
            self.session, sequence, stamp, (x, body_y, z),
            (body.velocity_x, 0.0, body.velocity_z), body.yaw_radians, 0.0,
            "standing", box, True, False, False,
        )
        return NavigationFrame(self.session, value, self.world.view(), "fabric")


def apply(body: PlanarBodyState, movement: MovementV1, motion: GroundMotionProfile) -> PlanarBodyState:
    control = GroundControl(movement.forward, -movement.strafe, body.yaw_radians)
    return predict_ground(body, (control,), motion)[-1]


class FixedRouteWalkTests(unittest.TestCase):
    def test_physical_l_corridor_has_two_high_walls_and_open_centerline(self) -> None:
        walls = set(l_corridor_wall_positions(.5, 1.0, .5, 1))
        self.assertTrue(walls)
        self.assertEqual({y for _, y, _ in walls}, {1, 2})
        for z in range(0, 5):
            self.assertNotIn((0, 1, z), walls)
            self.assertNotIn((1, 1, z), walls)
        for x in range(0, 5):
            self.assertNotIn((x, 1, 3), walls)
            self.assertNotIn((x, 1, 4), walls)
        base_y = min(y for _, y, _ in walls)
        restored_ground = {(x, base_y - 1, z) for x, _, z in walls}
        self.assertEqual(len(restored_ground), len(walls) // 2)

    def test_nonzero_closed_route_cannot_succeed_at_its_start(self) -> None:
        fixture, motion = FlatFixture(), profile()
        body = PlanarBodyState(0, 0, 0, 0, 0)
        route = FixedRoute("closed", (
            RoutePoint(0, 1, 0), RoutePoint(0, 1, 3), RoutePoint(3, 1, 3),
            RoutePoint(3, 1, 0), RoutePoint(0, 1, 0),
        ))
        controller = FixedRouteController(motion)
        controller.start(route, fixture.frame(0, body))
        decision = controller.decide(fixture.frame(1, body))
        self.assertNotEqual(decision.state, FixedRouteState.SUCCEEDED)
        self.assertLess(decision.progress_blocks, 1.0)

    def test_wrong_level_unverified_material_and_excess_speed_are_unsupported(self) -> None:
        motion = profile()
        cases = (
            (FlatFixture(), PlanarBodyState(0, 0, 0, 0, 0), 2.0, "level"),
            (FlatFixture("minecraft:ice"), PlanarBodyState(0, 0, 0, 0, 0), 1.0, "material"),
            (FlatFixture(), PlanarBodyState(0, 0, motion.maximum_speed_blocks_per_second + .2, 0, 0),
             1.0, "speed"),
        )
        for fixture, body, route_y, label in cases:
            with self.subTest(label=label):
                controller = FixedRouteController(motion)
                frame = fixture.frame(0, body)
                controller.start(FixedRoute(label, (
                    RoutePoint(0, route_y, 0), RoutePoint(0, route_y, 3))), frame)
                decision = controller.decide(frame)
                self.assertEqual(decision.state, FixedRouteState.UNSUPPORTED)
                self.assertEqual(decision.movement, MovementV1())

    def test_candidate_does_not_enter_an_uncalibrated_floor_material(self) -> None:
        fixture, motion = FlatFixture(), profile()
        stamp = ObservationStamp(fixture.session, 1, 1, "test-clock", 50_000_000)
        fixture.world.observe_blocks(stamp, {
            (x, 0, z): BlockGeometry.full_cube("minecraft:ice")
            for x in range(-2, 3) for z in range(1, 4)
        })
        body = PlanarBodyState(0, .55, 0, 1.0, 0)
        frame = fixture.frame(0, body)
        controller = FixedRouteController(motion)
        controller.start(FixedRoute("material-boundary", (
            RoutePoint(0, 1, .55), RoutePoint(0, 1, 4))), frame)
        for sequence in range(1, 20):
            decision = controller.decide(fixture.frame(sequence, body))
            if decision.state is FixedRouteState.UNSUPPORTED:
                break
            self.assertNotEqual(decision.state, FixedRouteState.BLOCKED)
            body = apply(body, decision.movement, motion)
        self.assertEqual(decision.state, FixedRouteState.UNSUPPORTED)

    def test_full_input_lease_and_release_horizon_are_safe_near_a_wall(self) -> None:
        fixture, motion = FlatFixture(), profile()
        stamp = ObservationStamp(fixture.session, 1, 1, "test-clock", 50_000_000)
        fixture.world.observe_blocks(stamp, {
            (x, y, 1): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-2, 3) for y in (1, 2)
        })
        body = PlanarBodyState(0, .35, 0, .2, 0)
        frame = fixture.frame(0, body)
        controller = FixedRouteController(motion)
        controller.start(FixedRoute("lease-wall", (
            RoutePoint(0, 1, .35), RoutePoint(0, 1, 4))), frame)
        decision = controller.decide(frame)
        control = GroundControl(decision.movement.forward, -decision.movement.strafe,
                                body.yaw_radians)
        neutral = GroundControl(0, 0, body.yaw_radians)
        states = predict_ground(
            body,
            (control,) * decision.input_lease_ticks + (neutral,) * 4,
            motion,
        )
        box = frame.body.body_box
        for previous, current in zip(states, states[1:]):
            delta = (current.x - previous.x, 0.0, current.z - previous.z)
            self.assertIsNot(sweep(box, delta, frame.world).status, QueryStatus.BLOCKED)
            box = box.moved(*delta)

    def test_cancel_checks_the_full_stop_tail_without_soft_wall_cost(self) -> None:
        fixture, motion = FlatFixture(), profile()
        stamp = ObservationStamp(fixture.session, 1, 1, "test-clock", 50_000_000)
        fixture.world.observe_blocks(stamp, {
            (x, y, 1): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-2, 3) for y in (1, 2)
        })
        body = PlanarBodyState(0, .598, 0, 1.0, 0)
        frame = fixture.frame(0, body)
        controller = FixedRouteController(
            motion, FixedRouteConfig(
                wall_soft_margin_blocks=0,
                motion_prediction_margin_blocks=0,
            ),
        )
        controller.start(FixedRoute("cancel-tail", (
            RoutePoint(0, 1, .598), RoutePoint(0, 1, 4))), frame)
        controller.cancel()
        decision = controller.decide(fixture.frame(1, body))
        self.assertEqual(decision.movement, MovementV1(forward=-1))
        control = GroundControl(
            decision.movement.forward, -decision.movement.strafe, body.yaw_radians,
        )
        neutral = GroundControl(0, 0, body.yaw_radians)
        controls = [control] * decision.input_lease_ticks
        state = predict_ground(body, tuple(controls), motion)[-1]
        while math.hypot(state.velocity_x, state.velocity_z) > .1:
            controls.append(neutral)
            state = predict_ground(state, (neutral,), motion)[-1]
        # The body still drifts after crossing the stopped-speed threshold.
        # Keep checking the physical tail rather than ending the assertion at
        # the controller's state-transition threshold.
        controls.extend((neutral,) * 30)
        states = predict_ground(body, tuple(controls), motion)
        box = frame.body.body_box
        for previous, current in zip(states, states[1:]):
            delta = (current.x - previous.x, 0.0, current.z - previous.z)
            self.assertIsNot(sweep(box, delta, frame.world).status, QueryStatus.BLOCKED)
            box = box.moved(*delta)

    def test_collision_margin_cannot_inflate_hard_pit_support(self) -> None:
        fixture, motion = FlatFixture(), profile()
        stamp = ObservationStamp(fixture.session, 1, 1, "test-clock", 50_000_000)
        fixture.world.confirm_air(stamp, tuple(
            (x, 0, z) for x in range(-2, 3) for z in range(1, 5)
        ))
        body = PlanarBodyState(0, .79, 0, 0, 0)
        frame = fixture.frame(0, body)
        config = FixedRouteConfig(
            minimum_support_fraction=.15,
            preferred_support_fraction=.15,
            wall_soft_margin_blocks=0,
            motion_prediction_margin_blocks=.03,
        )
        controller = FixedRouteController(motion, config)
        controller.start(FixedRoute("pit-margin", (
            RoutePoint(0, 1, .79), RoutePoint(0, 1, 4),
        )), frame)
        decision = controller.decide(frame)
        control = GroundControl(
            decision.movement.forward, -decision.movement.strafe, body.yaw_radians,
        )
        neutral = GroundControl(0, 0, body.yaw_radians)
        states = predict_ground(
            body,
            (control,) * decision.input_lease_ticks + (neutral,) * 30,
            motion,
        )
        box = frame.body.body_box
        for previous, current in zip(states, states[1:]):
            delta = (current.x - previous.x, 0.0, current.z - previous.z)
            box = box.moved(*delta)
            support = query_support(box, frame.world)
            self.assertGreaterEqual(
                support.support_fraction,
                config.minimum_support_fraction,
                "selected input lets the real footprint cross the hard support limit",
            )

    def test_terminal_coincident_and_repeated_points_finish_without_division(self) -> None:
        fixture, motion = FlatFixture(), profile()
        body = PlanarBodyState(2.0, 3.0, 0.0, 0.0, 0.7)
        route = FixedRoute("coincident", (
            RoutePoint(2.0, 1.0, 3.0), RoutePoint(2.0, 1.0, 3.0),
        ))
        controller = FixedRouteController(motion)
        controller.start(route, fixture.frame(0, body))
        decision = controller.decide(fixture.frame(1, body))
        self.assertEqual(decision.state, FixedRouteState.SUCCEEDED)
        self.assertEqual(decision.movement, MovementV1())

    def test_cancel_is_not_complete_until_actual_body_is_stopped(self) -> None:
        fixture, motion = FlatFixture(), profile()
        body = PlanarBodyState(0.0, 0.0, 2.0, 0.0, -math.pi / 2)
        route = FixedRoute("cancel", (RoutePoint(0, 1, 0), RoutePoint(8, 1, 0)))
        controller = FixedRouteController(motion)
        controller.start(route, fixture.frame(0, body))
        controller.cancel()
        first = controller.decide(fixture.frame(1, body))
        self.assertEqual(first.state, FixedRouteState.CANCELLING)
        self.assertNotEqual(first.state, FixedRouteState.CANCELLED)
        for sequence in range(2, 30):
            body = apply(body, first.movement, motion)
            first = controller.decide(fixture.frame(sequence, body))
            if first.state is FixedRouteState.CANCELLED:
                break
        self.assertEqual(first.state, FixedRouteState.CANCELLED)
        self.assertLessEqual(math.hypot(body.velocity_x, body.velocity_z), 0.1)

    def test_lost_input_confirmation_fails_closed(self) -> None:
        fixture, motion = FlatFixture(), profile()
        body = PlanarBodyState(0, 0, 0, 0, 0)
        route = FixedRoute("lost", (RoutePoint(0, 1, 0), RoutePoint(0, 1, 5)))
        controller = FixedRouteController(motion)
        controller.start(route, fixture.frame(0, body))
        controller.decide(fixture.frame(1, body))
        lost = controller.decide(fixture.frame(2, body), input_confirmed=False)
        self.assertEqual(lost.state, FixedRouteState.INPUT_LOST)
        self.assertEqual(lost.movement, MovementV1())
        self.assertEqual(controller.decide(fixture.frame(3, body)).state, FixedRouteState.INPUT_LOST)

    def test_unknown_body_volume_is_requested_instead_of_treated_as_air(self) -> None:
        session = WorldSessionId("b03-unknown")
        world = WorldKnowledge(session)
        stamp = ObservationStamp(session, 0, 0, "clock", 0)
        world.observe_blocks(stamp, {
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        body = PlanarBodyState(0.5, 0.5, 0, 0, 0)
        state = BodyState(session, 0, stamp, (0.5, 1, 0.5), (0, 0, 0), 0, 0, "standing",
                          Aabb(0.2, 1, 0.2, 0.8, 2.8, 0.8), True, False, False)
        frame = NavigationFrame(session, state, world.view(), "fabric")
        controller = FixedRouteController(profile())
        controller.start(FixedRoute("unknown", (RoutePoint(.5, 1, .5), RoutePoint(.5, 1, 3))), frame)
        decision = controller.decide(frame)
        self.assertEqual(decision.state, FixedRouteState.NEEDS_INFORMATION)
        self.assertTrue(decision.missing_cells)
        self.assertEqual(decision.movement, MovementV1())

    def test_forward_information_is_reported_before_safe_motion_runs_out(self) -> None:
        session = WorldSessionId("b03-forward-gap")
        world = WorldKnowledge(session)
        stamp = ObservationStamp(session, 0, 0, "clock", 0)
        world.observe_blocks(stamp, {
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        world.confirm_air(stamp, ((0, 1, 0), (0, 2, 0)))
        body = PlanarBodyState(.5, .5, 0, 0, 0)
        state = BodyState(session, 0, stamp, (.5, 1, .5), (0, 0, 0), 0, 0, "standing",
                          Aabb(.2, 1, .2, .8, 2.8, .8), True, False, False)
        frame = NavigationFrame(session, state, world.view(), "fabric")
        controller = FixedRouteController(profile())
        controller.start(FixedRoute("forward-gap", (
            RoutePoint(.5, 1, .5), RoutePoint(.5, 1, 3.5))), frame)
        decision = controller.decide(frame)
        self.assertIn(decision.state, {FixedRouteState.RUNNING, FixedRouteState.NEEDS_INFORMATION})
        self.assertTrue(decision.missing_cells)

    def test_known_wall_and_pit_stop_without_repeated_retries(self) -> None:
        motion = profile()
        for obstacle in ("wall", "pit"):
            with self.subTest(obstacle=obstacle):
                fixture = FlatFixture()
                stamp = ObservationStamp(fixture.session, 1, 1, "test-clock", 50_000_000)
                if obstacle == "wall":
                    fixture.world.observe_blocks(stamp, {
                        (x, y, 1): BlockGeometry.full_cube("minecraft:stone")
                        for x in range(-3, 4) for y in (1, 2)
                    })
                    maximum_z = .70
                else:
                    pit = tuple((x, 0, 1) for x in range(-3, 4))
                    fixture.world.invalidate(stamp, pit)
                    fixture.world.confirm_air(
                        ObservationStamp(fixture.session, 2, 2, "test-clock", 100_000_000), pit)
                    # Partial overhang is permitted; losing the minimum support is not.
                    maximum_z = 1.20
                body = PlanarBodyState(0, 0, 0, 0, 0)
                controller = FixedRouteController(motion)
                controller.start(FixedRoute(obstacle, (
                    RoutePoint(0, 1, 0), RoutePoint(0, 1, 4))), fixture.frame(0, body))
                for sequence in range(1, 61):
                    decision = controller.decide(fixture.frame(sequence, body))
                    if decision.state is FixedRouteState.BLOCKED:
                        break
                    body = apply(body, decision.movement, motion)
                self.assertEqual(decision.state, FixedRouteState.BLOCKED)
                self.assertLessEqual(body.z, maximum_z)
                self.assertLessEqual(math.hypot(body.velocity_x, body.velocity_z), .1)
                self.assertLess(sequence, 40)

    def test_one_hundred_parameterized_fixed_routes_close_the_loop(self) -> None:
        motion = profile()
        fixture = FlatFixture()
        shapes = (
            ((0, 0), (0, 6)),
            ((0, 0), (6, 0)),
            ((0, 0), (4, 0), (4, 5)),
            ((0, 0), (0, 4), (3, 7)),
            ((0, 0), (3, 0), (3, 0), (5, 2), (5, 5)),
        )
        yaws = (-math.pi, -math.pi / 2, 0.0, math.pi / 2, math.pi)
        offsets = (0.0, 0.02, 0.05, 0.10)
        successes = 0
        control_times_ms = []
        for shape in shapes:
            for yaw in yaws:
                for offset in offsets:
                    with self.subTest(shape=shape, yaw=yaw, offset=offset):
                        points = tuple(RoutePoint(x, 1, z) for x, z in shape)
                        first, second = shape[0], shape[1]
                        dx, dz = second[0] - first[0], second[1] - first[1]
                        length = math.hypot(dx, dz)
                        body = PlanarBodyState(first[0] - dz / length * offset,
                                               first[1] + dx / length * offset, 0, 0, yaw)
                        controller = FixedRouteController(motion)
                        controller.start(FixedRoute(f"case-{successes}", points), fixture.frame(0, body))
                        moving_started = False
                        middle_low_ticks = 0
                        for sequence in range(1, 501):
                            decision = controller.decide(fixture.frame(sequence, body))
                            control_times_ms.append(decision.control_time_ns / 1_000_000)
                            speed = math.hypot(body.velocity_x, body.velocity_z)
                            goal_distance = math.hypot(body.x - shape[-1][0], body.z - shape[-1][1])
                            if speed > .2:
                                moving_started = True
                            if moving_started and goal_distance > .8 and speed < .1:
                                middle_low_ticks += 1
                            else:
                                middle_low_ticks = 0
                            self.assertLess(middle_low_ticks, 3)
                            if decision.state is FixedRouteState.SUCCEEDED:
                                break
                            self.assertIn(decision.state, {FixedRouteState.RUNNING, FixedRouteState.BRAKING})
                            body = apply(body, decision.movement, motion)
                        self.assertEqual(decision.state, FixedRouteState.SUCCEEDED)
                        self.assertLessEqual(math.hypot(body.x - shape[-1][0], body.z - shape[-1][1]), .25)
                        self.assertLessEqual(math.hypot(body.velocity_x, body.velocity_z), .1)
                        successes += 1
        self.assertEqual(successes, 100)
        ordered = sorted(control_times_ms)
        self.assertLessEqual(ordered[math.ceil(len(ordered) * .95) - 1], 8.0)
        self.assertLessEqual(ordered[math.ceil(len(ordered) * .99) - 1], 15.0)
        self.assertLess(max(ordered), 30.0)


if __name__ == "__main__":
    unittest.main()
