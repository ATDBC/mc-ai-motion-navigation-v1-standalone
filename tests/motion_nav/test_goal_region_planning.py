"""F2-SG typed goal-set and single-search regression gates."""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.known_map_planner import (
    ExactSurfacePlanningGoal,
    GoalRegionPlanningRequest,
    GoalTerminalWitness,
    KnownMapBounds,
    KnownMapSnapshotBuilder,
    SurfacePlanningRequest,
    SurfacePlanningStatus,
    SurfaceRouteCandidate,
    _plain_search,
    _resource_aware_search,
    plan_known_surface_snapshot,
)
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockGeometry,
    ObservationStamp,
    WorldKnowledge,
    WorldSessionId,
)
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_b07_step_transition import profile as step_profile


def _two_level_goal_world() -> WorldKnowledge:
    """Known world where the geometrically preferred upper goal is unreachable."""
    session = WorldSessionId("f2sg-two-level-goal")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "f2sg-test", 50_000_000)
    cells = tuple(
        (x, y, z)
        for x in range(-1, 5)
        for y in range(60, 69)
        for z in range(-1, 2)
    )
    world.confirm_air(stamp, cells)
    blocks = {
        (x, 63, 0): BlockGeometry.full_cube("minecraft:stone")
        for x in range(4)
    }
    # This creates an upper surface inside the same GoalState.  With no
    # JumpUp profile the upper surface cannot be reached from the lower run.
    blocks[(0, 64, 0)] = BlockGeometry.full_cube("minecraft:stone")
    world.observe_blocks(stamp, blocks)
    return world


def _goal_state() -> GoalState:
    return GoalState(
        Aabb(-.05, 63.9, .15, 1.95, 65.1, .85),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _snapshot(world: WorldKnowledge):
    bounds = KnownMapBounds(0, 3, 64, 65, 0, 0, True)
    result = KnownMapSnapshotBuilder(world.view(), bounds).advance(world.view(), 10_000)
    return result.snapshot


class F2SGContractTests(unittest.TestCase):
    def test_region_and_exact_targets_are_distinct_typed_requests(self):
        world = _two_level_goal_world()
        exact = SurfacePlanningRequest(
            1, "f2sg-exact", "goal", 1, world.session.value,
            SurfaceNodeId(3, 0, 64, 0), SurfaceNodeId(1, 0, 64, 0),
            planning_target=ExactSurfacePlanningGoal(SurfaceNodeId(1, 0, 64, 0)),
        )
        region = SurfacePlanningRequest(
            2, "f2sg-region", "goal", 1, world.session.value,
            SurfaceNodeId(3, 0, 64, 0), SurfaceNodeId(0, 0, 65, 0),
            goal_state=_goal_state(),
            planning_target=GoalRegionPlanningRequest(_goal_state()),
        )

        self.assertIs(type(exact.planning_target), ExactSurfacePlanningGoal)
        self.assertIs(type(region.planning_target), GoalRegionPlanningRequest)
        other_goal = GoalState(
            Aabb(-.05, 63.9, .15, .95, 65.1, .85),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        with self.assertRaises(ContractViolation):
            SurfacePlanningRequest(
                3, "f2sg-mismatch", "goal", 1, world.session.value,
                SurfaceNodeId(3, 0, 64, 0), SurfaceNodeId(0, 0, 65, 0),
                goal_state=other_goal,
                planning_target=GoalRegionPlanningRequest(_goal_state()),
            )

    def test_region_complete_candidate_requires_bound_terminal_witness(self):
        world = _two_level_goal_world()
        goal = _goal_state()
        request = SurfacePlanningRequest(
            1, "f2sg-contract", "goal", 1, world.session.value,
            SurfaceNodeId(3, 0, 64, 0), SurfaceNodeId(0, 0, 65, 0),
            goal_state=goal,
            planning_target=GoalRegionPlanningRequest(goal),
        )
        with self.assertRaises(ContractViolation):
            SurfaceRouteCandidate(
                request.sequence, request.request_id, request.goal_id,
                request.goal_revision, request.world_session, 1,
                request.start, request.goal, SurfacePlanningStatus.COMPLETE,
                (), (), None, (), 0, goal_state=goal,
                planning_target=request.planning_target,
            )

        completion = GroundCompletionRegion(
            Aabb(1.1, 63.95, .2, 1.9, 64.05, .8),
            (1.5, 64.0, .5), 64.0, (1, 0, 64, 0), ((1, 63, 0),),
        )
        witness = GoalTerminalWitness(
            SurfaceNodeId(1, 0, 64, 0), completion, ((1, 63, 0),)
        )
        self.assertEqual(witness.terminal_surface, SurfaceNodeId(1, 0, 64, 0))


class F2SGSnapshotSearchTests(unittest.TestCase):
    def test_goal_enumeration_and_search_share_one_planning_deadline(self):
        world = _two_level_goal_world()
        goal = _goal_state()
        request = SurfacePlanningRequest(
            1, "f2sg-deadline", "goal", 1, world.session.value,
            SurfaceNodeId(3, 0, 64, 0), SurfaceNodeId(0, 0, 65, 0),
            goal_state=goal,
            planning_target=GoalRegionPlanningRequest(goal),
            maximum_planning_seconds=.5,
        )

        clock = iter((0,))
        with patch(
            "mc2p.motion_nav.known_map_planner.time.perf_counter_ns",
            side_effect=lambda: next(clock, 600_000_000),
        ):
            result = plan_known_surface_snapshot(
                _snapshot(world), ordinary_profile(), step_profile(), request,
            )

        self.assertIs(result.status, SurfacePlanningStatus.TIMEOUT)
        self.assertEqual(result.path, ())

    def test_one_snapshot_search_chooses_reachable_surface_in_goal_region(self):
        world = _two_level_goal_world()
        goal = _goal_state()
        request = SurfacePlanningRequest(
            1, "f2sg-two-terminal", "goal", 1, world.session.value,
            SurfaceNodeId(3, 0, 64, 0),
            # The hint is deliberately the unreachable upper surface.
            SurfaceNodeId(0, 0, 65, 0),
            goal_state=goal,
            planning_target=GoalRegionPlanningRequest(goal),
        )

        result = plan_known_surface_snapshot(
            _snapshot(world), ordinary_profile(), step_profile(), request,
        )

        self.assertIs(result.status, SurfacePlanningStatus.COMPLETE)
        self.assertEqual(result.planning_goal, SurfaceNodeId(1, 0, 64, 0))
        self.assertIsNotNone(result.terminal_witness)
        self.assertEqual(
            result.terminal_witness.terminal_surface,
            SurfaceNodeId(1, 0, 64, 0),
        )
        self.assertEqual(result.path[-1].node_id, result.planning_goal)

    def test_f2sg_does_not_rewrite_generic_search_loops(self):
        plain = inspect.getsource(_plain_search)
        resource = inspect.getsource(_resource_aware_search)
        self.assertNotIn("GoalRegionPlanningRequest", plain)
        self.assertNotIn("GoalRegionPlanningRequest", resource)
        self.assertNotIn("virtual", plain.lower())
        self.assertNotIn("virtual", resource.lower())


class F2SGFormalChainTests(unittest.TestCase):
    def test_region_goal_already_satisfied_uses_local_completion_not_zero_action_route(self):
        from scripts.f2s_support_region_evidence import run_v9_case
        from tests.sim.f2s_cases import materialized_manifest

        case = next(
            row for row in materialized_manifest()["support_region_cases"]
            if row["id"] == "f2s/cross_piece/follow/south/normal"
        )
        result = run_v9_case(case)

        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["reason"], "goal_state_satisfied")

    def test_goal_set_skips_a_completion_sliver_the_ground_controller_cannot_stop_in(self):
        from scripts.f2s_support_region_evidence import run_v9_case
        from tests.sim.f2s_cases import materialized_manifest

        case = next(
            row for row in materialized_manifest()["support_region_cases"]
            if row["id"] == "f2s/bridge_head/follow/south/normal"
        )
        result = run_v9_case(case)

        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["reason"], "goal_state_satisfied")

    def test_frozen_f2s_c04_reaches_the_other_goal_surface(self):
        from scripts.f2r_piecewise_evidence import formal
        from tests.sim.f2r_cases import materialized_manifest

        case = next(
            row for row in materialized_manifest()["clutter_scan"]
            if row["id"] == "f2r/clutter/0.2/3/9/product"
        )
        result = formal(case)

        self.assertTrue(result["success"])
        self.assertEqual(result["reason"], "goal_state_satisfied")
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["damage"], 0.0)
        self.assertTrue(result["source_released"])

    def test_same_start_surface_region_still_emits_the_terminal_walk(self):
        """A zero-edge graph path still has to walk into its bound region."""
        from scripts.f2r_piecewise_evidence import formal
        from tests.sim.f2r_cases import materialized_manifest

        case = next(
            row for row in materialized_manifest()["clutter_scan"]
            if row["id"] == "f2r/clutter/0.1/0/14/follow"
        )
        result = formal(case)

        self.assertTrue(result["success"])
        self.assertEqual(result["reason"], "goal_state_satisfied")
        self.assertEqual(result["violations"], [])
        self.assertTrue(result["source_released"])


if __name__ == "__main__":
    unittest.main()
