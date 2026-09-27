from dataclasses import replace
import unittest

from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds,
    KnownMapSnapshotBuilder,
    SnapshotBuildStatus,
    SurfacePlanningRequest,
    SurfacePlanningStatus,
    SurfaceWalkEdge,
    build_surface_graph,
    plan_known_surface_snapshot,
    seconds_to_planning_ticks,
    astar_surface_plan,
)
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.ground_traversal import (
    GroundTraversalProofCache,
    verify_ground_traversal,
)
from mc2p.motion_nav.route_admission import RouteAdmitter
from mc2p.motion_nav.action_route import WalkSegment
from mc2p.motion_nav.ground_motion import PlanarBodyState
from tests.motion_nav.test_b07_surface_planning import (
    mixed_height_world,
    ordinary_profile,
)
from tests.motion_nav.test_b07_step_transition import profile as step_profile
from tests.motion_nav.test_b07_support_surfaces import surface_world
from tests.motion_nav.test_ground_traversal import traversal_fixture
from tests.motion_nav.test_b10_gap_solver import fixture as gap_fixture
from tests.motion_nav.test_fixed_route_walk import FlatFixture
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp


class ContinuousHeightPlanningTests(unittest.TestCase):
    def test_seconds_are_rounded_up_to_positive_planning_ticks(self):
        self.assertEqual(seconds_to_planning_ticks(0.0001), 1)
        self.assertEqual(seconds_to_planning_ticks(0.05), 1)
        self.assertEqual(seconds_to_planning_ticks(0.05001), 2)

    def test_adjacent_half_block_height_change_has_walk_candidate_and_step_fallback(self):
        world = mixed_height_world()

        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )
        forward = tuple(
            edge for edge in graph.edges
            if edge.start.column_x == 0 and edge.end.column_x == 1
        )

        walks = tuple(edge for edge in forward if type(edge) is SurfaceWalkEdge)
        self.assertEqual(len(walks), 1)
        self.assertTrue(walks[0].requires_ground_traversal_proof)
        self.assertTrue(any(type(edge).__name__ == "StepEdge" for edge in forward))

    def test_surface_route_reports_integer_tick_cost_as_search_truth(self):
        world = mixed_height_world()
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )
        request = SurfacePlanningRequest(
            1, "continuous-height", "continuous-height-goal", 1,
            world.session.value,
            SurfaceNodeId(0, 0, 0, 0),
            SurfaceNodeId(1, 0, 1, 0),
        )

        candidate = astar_surface_plan(graph, request)

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertIs(type(candidate.total_cost_ticks), int)
        self.assertEqual(
            candidate.total_cost_seconds,
            candidate.total_cost_ticks * ordinary_profile().tick_seconds,
        )

    def test_snapshot_planner_attaches_formal_proof_before_using_low_height_walk(self):
        world = surface_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry(
                "minecraft:smooth_stone_slab", "boxes",
                (Aabb(0, 0, 0, 1, .5, 1),),
            ),
            (2, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        world.confirm_air(
            ObservationStamp(
                world.session, 2, 2, "test-clock", 50_000_000,
            ),
            tuple((2, y, z) for y in range(-2, 6) for z in range(-1, 2)
                  if (2, y, z) != (2, 0, 0)),
        )
        bounds = KnownMapBounds(0, 2, 0, 1, 0, 0, True)
        built = KnownMapSnapshotBuilder(world.view(), bounds).advance(
            world.view(), 10_000,
        )
        self.assertIs(built.status, SnapshotBuildStatus.COMPLETE)
        anchor, _, _, _ = gap_fixture()
        entry = replace(
            anchor.physics_state,
            session=world.session,
            movement_tick_id=0,
            position=(0.5, 1.0, 0.5),
            velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
            yaw_radians=-1.5707963267948966,
        )
        request = SurfacePlanningRequest(
            2, "proved-height", "proved-height-goal", 1,
            world.session.value,
            SurfaceNodeId(0, 0, 1, 0),
            SurfaceNodeId(2, 0, 1, 0),
            entry_physics_state=entry,
        )

        candidate = plan_known_surface_snapshot(
            built.snapshot, ordinary_profile(),
            replace(step_profile(), cost_seconds=.8), request,
        )

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertTrue(
            candidate.ground_traversal_plans,
            msg=(candidate.total_cost_ticks,
                 tuple(type(edge).__name__ for edge in candidate.segments)),
        )
        self.assertTrue(any(type(edge) is SurfaceWalkEdge
                            for edge in candidate.segments))
        self.assertEqual(
            candidate.total_cost_ticks,
            candidate.ground_traversal_plans[0].estimated_ticks,
        )
        frame = FlatFixture().frame(
            0, PlanarBodyState(0.5, 0.5, 0.0, 0.0, 0.0),
        )
        action_route = RouteAdmitter._surface_action_route(
            candidate, frame, 0.0, (), "proved-height-route",
        )
        self.assertIsNotNone(action_route)
        self.assertIs(type(action_route.actions[0]), WalkSegment)
        self.assertIsNotNone(action_route.actions[0].traversal_plan)

    def test_ground_proof_cache_is_bounded_and_revision_keyed_by_caller(self):
        state, route, world = traversal_fixture()
        result = verify_ground_traversal(
            state, route, world, ordinary_profile(), maximum_ticks=80,
        )
        cache = GroundTraversalProofCache(2)
        cache.put(("revision", 1), result)
        cache.put(("revision", 2), result)
        self.assertIs(cache.get(("revision", 1)), result)
        cache.put(("revision", 3), result)

        self.assertIsNone(cache.get(("revision", 2)))
        self.assertIs(cache.get(("revision", 1)), result)
        self.assertIs(cache.get(("revision", 3)), result)


if __name__ == "__main__":
    unittest.main()
