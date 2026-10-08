"""F2-ST contracts for a proved, costed terminal ground edge."""
from __future__ import annotations

from dataclasses import replace
import inspect
import math
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.ground_terminal_approach import (
    GroundTerminalApproach,
    GroundTerminalApproachStatus,
    GroundTraversalExitRequirement,
    solve_ground_terminal_approach,
)
from mc2p.motion_nav.known_map_planner import (
    PlannerStateKey,
    SurfaceTerminalApproachEdge,
    _plain_search,
    _resource_aware_search,
)
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.segment_entry import SegmentEntryWindow
from tests.motion_nav.test_fixed_route_walk import FlatFixture, profile
from tests.motion_nav.test_ground_traversal import traversal_fixture


def _completion(x0=.55, x1=.95, z0=.55, z1=.95):
    return GroundCompletionRegion(
        bounds=__import__("mc2p.motion_nav.world_model", fromlist=["Aabb"]).Aabb(
            x0, .95, z0, x1, 1.05, z1,
        ),
        reference_point=((x0 + x1) / 2, 1.0, (z0 + z1) / 2),
        support_height=1.0,
        surface_identity=(0, 0, 1, 0),
        dependencies=((0, 0, 0),),
    )


class F2STContractRedTests(unittest.TestCase):
    def test_exit_requirement_owns_terminal_speed_instead_of_a_constant(self):
        requirement = GroundTraversalExitRequirement(
            _completion(), frozenset({"standing"}),
            frozenset({MovementMode.WALK}), 0.0, .6,
        )
        self.assertEqual(requirement.maximum_speed_blocks_per_second, .6)
        with self.assertRaises(ContractViolation):
            replace(requirement, minimum_speed_blocks_per_second=.7)

    def test_already_satisfied_is_a_zero_input_zero_tick_proof(self):
        state, _, world = traversal_fixture()
        completion = _completion(.3, .7, .3, .7)
        requirement = GroundTraversalExitRequirement(
            completion, frozenset({"standing"}),
            frozenset({MovementMode.WALK}), 0.0, .2,
        )
        result = solve_ground_terminal_approach(
            entry_state=state,
            entry_window=None,
            completion_region=completion,
            exit_requirement=requirement,
            world=world,
            profile=profile(),
            maximum_ticks=80,
        )
        self.assertIs(result.status, GroundTerminalApproachStatus.ALREADY_SATISFIED)
        self.assertIsNotNone(result.approach)
        self.assertEqual(result.approach.cost_ticks, 0)
        self.assertEqual(result.approach.plan.inputs, ())
        self.assertTrue(all(item == MovementV1() for item in result.approach.plan.commands))

    def test_unsupported_entry_is_typed_and_does_not_become_zero_speed(self):
        state, _, world = traversal_fixture()
        completion = _completion(1.2, 1.8, .2, .8)
        requirement = GroundTraversalExitRequirement(
            completion, frozenset({"standing"}),
            frozenset({MovementMode.WALK}), 0.0, .2,
        )
        result = solve_ground_terminal_approach(
            entry_state=replace(state, sprinting=True),
            entry_window=None,
            completion_region=completion,
            exit_requirement=requirement,
            world=world,
            profile=profile(),
            maximum_ticks=80,
        )
        self.assertIs(result.status, GroundTerminalApproachStatus.ENTRY_UNPROVEN)
        self.assertIsNone(result.approach)

    def test_search_state_and_edge_keep_terminal_approach_identity(self):
        self.assertIn("terminal_approach_id", PlannerStateKey.__dataclass_fields__)
        self.assertIn("approach", SurfaceTerminalApproachEdge.__dataclass_fields__)

    def test_generic_search_loops_remain_goal_agnostic(self):
        for search in (_plain_search, _resource_aware_search):
            source = inspect.getsource(search)
            self.assertNotIn("GroundTerminalApproach", source)
            self.assertNotIn("SurfaceTerminalApproachEdge", source)


if __name__ == "__main__":
    unittest.main()
