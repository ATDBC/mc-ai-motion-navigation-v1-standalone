from dataclasses import replace
import math
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.fixed_route import (
    FixedRoute, FixedRouteController, RoutePoint,
)
from mc2p.motion_nav.ground_traversal import (
    GroundTraversalStatus,
    verify_ground_traversal,
)
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)
from tests.motion_nav.test_b10_gap_solver import fixture as gap_fixture
from tests.motion_nav.test_fixed_route_walk import FlatFixture, profile


def traversal_fixture(*, hide=()):
    session = WorldSessionId("ground-traversal-test")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "test", 1)
    known = tuple(
        (x, y, z)
        for x in range(-2, 3)
        for y in range(-2, 6)
        for z in range(-2, 6)
        if (x, y, z) not in hide
    )
    world.confirm_air(stamp, known)
    blocks = {
        (x, 0, z): BlockGeometry.full_cube("minecraft:grass_block")
        for x in range(-2, 3) for z in range(-2, 6)
    }
    blocks[(0, 1, 1)] = BlockGeometry(
        "minecraft:smooth_stone_slab", "boxes",
        (Aabb(0, 0, 0, 1, .5, 1),),
    )
    blocks[(0, 1, 2)] = BlockGeometry.full_cube("minecraft:stone")
    world.observe_blocks(stamp, {
        position: block for position, block in blocks.items()
        if position not in hide
    })
    anchor, _, _, _ = gap_fixture()
    state = replace(
        anchor.physics_state,
        session=session,
        movement_tick_id=0,
        position=(0.5, 1.0, 0.5),
        velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
        yaw_radians=0.0,
    )
    route = FixedRoute("two-natural-steps", (
        RoutePoint(0.5, 1.0, 0.5),
        RoutePoint(0.5, 1.5, 1.5),
        RoutePoint(0.5, 2.0, 2.5),
    ))
    return state, route, PhysicsWorldView(world.view(), JAVA_1_21_RULESET)


def shallow_snow_fixture():
    """A flat lane whose two 1/8-block snow rises start at the first edge."""
    session = WorldSessionId("ground-traversal-shallow-snow-test")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "test", 1)
    blocks = {
        (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
        for x in range(5)
    }
    for x in (1, 3):
        blocks[(x, 1, 0)] = BlockGeometry(
            "minecraft:snow", "boxes",
            (Aabb(0, 0, 0, 1, .125, 1),),
        )
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-2, 7)
        for y in range(-2, 7)
        for z in range(-2, 3)
        if (x, y, z) not in blocks
    ))
    world.observe_blocks(stamp, blocks)
    anchor, _, _, _ = gap_fixture()
    state = replace(
        anchor.physics_state,
        session=session,
        movement_tick_id=0,
        position=(.5, 1.0, .5),
        velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
        yaw_radians=-math.pi / 2.0,
    )
    route = FixedRoute("shallow-snow-from-rest", tuple(
        RoutePoint(x + .5, 1.125 if x in (1, 3) else 1.0, .5)
        for x in range(5)
    ))
    return state, route, PhysicsWorldView(world.view(), JAVA_1_21_RULESET)


class GroundTraversalVerificationTests(unittest.TestCase):
    def test_rest_entry_brakes_before_the_final_shallow_drop(self):
        state, route, world = shallow_snow_fixture()

        result = verify_ground_traversal(
            state, route, world, profile(), maximum_ticks=80,
        )

        self.assertIs(result.status, GroundTraversalStatus.VERIFIED)
        self.assertIsNotNone(result.plan)
        self.assertEqual(result.plan.route, route)
        self.assertLess(result.plan.trajectory[-1].position[0], 4.95)

    def test_verifies_two_natural_half_block_steps_without_static_step_actions(self):
        state, route, world = traversal_fixture()

        result = verify_ground_traversal(
            state, route, world, profile(), maximum_ticks=80,
        )

        self.assertIs(result.status, GroundTraversalStatus.VERIFIED)
        self.assertIsNotNone(result.plan)
        self.assertEqual(result.plan.route, route)
        self.assertGreater(result.plan.estimated_ticks, 0)
        self.assertTrue(any(
            "step_up" in events for events in result.plan.events_by_tick
        ))
        self.assertAlmostEqual(result.plan.trajectory[-1].position[1], 2.0)

    def test_verifies_the_same_surfaces_downhill_with_bounded_airborne_ticks(self):
        state, route, world = traversal_fixture()
        downhill = FixedRoute("two-natural-steps-down", tuple(reversed(route.points)))
        state = replace(
            state,
            position=(0.5, 2.0, 2.5),
            yaw_radians=math.pi,
        )

        result = verify_ground_traversal(
            state, downhill, world, profile(), maximum_ticks=80,
        )

        self.assertIs(result.status, GroundTraversalStatus.VERIFIED)
        self.assertAlmostEqual(result.plan.trajectory[-1].position[1], 1.0)
        self.assertLessEqual(
            max(
                sum(1 for grounded in group if not grounded)
                for group in (
                    tuple(item.on_ground for item in result.plan.trajectory),
                )
            ),
            8,
        )

    def test_unknown_clearance_does_not_become_a_traversal_proof(self):
        state, route, world = traversal_fixture(hide=((0, 3, 2),))

        result = verify_ground_traversal(
            state, route, world, profile(), maximum_ticks=80,
        )

        self.assertIs(result.status, GroundTraversalStatus.NEEDS_WORLD)
        self.assertIn((0, 3, 2), result.missing_cells)

    def test_height_above_step_rule_and_small_tick_budget_are_distinct(self):
        state, route, world = traversal_fixture()
        too_high = FixedRoute("too-high", (
            route.points[0], RoutePoint(0.5, 1.7, 1.5),
        ))

        unsupported = verify_ground_traversal(
            state, too_high, world, profile(), maximum_ticks=80,
        )
        exhausted = verify_ground_traversal(
            state, route, world, profile(), maximum_ticks=1,
        )

        self.assertIs(unsupported.status, GroundTraversalStatus.UNSUPPORTED)
        self.assertIs(exhausted.status, GroundTraversalStatus.BUDGET_EXHAUSTED)

    def test_varying_height_fixed_route_requires_a_matching_proof(self):
        state, route, world = traversal_fixture()
        frame_fixture = FlatFixture()
        controller = FixedRouteController(profile())
        with self.assertRaises(ContractViolation):
            controller.start(
                route,
                frame_fixture.frame(
                    0, PlanarBodyState(0.5, 0.5, 0.0, 0.0, 0.0),
                ),
            )

        proof = verify_ground_traversal(
            state, route, world, profile(), maximum_ticks=80,
        ).plan
        self.assertIsNotNone(proof)
        controller.start(
            route,
            frame_fixture.frame(
                1, PlanarBodyState(0.5, 0.5, 0.0, 0.0, 0.0),
            ),
            traversal_plan=proof,
        )


if __name__ == "__main__":
    unittest.main()
