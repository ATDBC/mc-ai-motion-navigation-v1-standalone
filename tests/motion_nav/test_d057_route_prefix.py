"""D057 removes only a proved redundant ground-route entry point."""
from dataclasses import replace
import math
import unittest
from unittest.mock import patch

import mc2p.motion_nav.route_admission as route_admission
from mc2p.motion_nav.action_route import JumpGapSegment, StepSegment, WalkSegment
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, SurfacePlanningRequest, astar_surface_plan,
    build_surface_graph,
)
from mc2p.motion_nav.movement_transition import MovementMode, ResourceState
from mc2p.motion_nav.route_admission import (
    ActiveRouteTracker, AdmissionStatus, RouteAdmitter,
)
from mc2p.motion_nav.support_surfaces import StandablePointResult
from mc2p.motion_nav.world_model import BlockGeometry
from tests.motion_nav import test_b07_step_route
from tests.motion_nav.test_b07_step_transition import frame, profile as step_profile
from tests.motion_nav.test_b07_surface_planning import (
    flat_surface_world, ordinary_profile,
)
from tests.motion_nav.test_b09_air_transitions import (
    air_profile, frame as air_frame, ground_profile, known_world,
)


class D057RoutePrefixTests(unittest.TestCase):
    def flat_candidate(self, direction):
        world = flat_surface_world(5)
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 4, 1, 1, 0, 4, True),
            ordinary_profile(), step_profile(),
        )
        nodes = {
            (node.node_id.column_x, node.node_id.column_z): node
            for node in graph.nodes
        }
        start = nodes[(2, 2)]
        goal = nodes[(2 + direction[0] * 2, 2 + direction[1] * 2)]
        request = SurfacePlanningRequest(
            1, f"d057-{'-'.join(map(str, direction))}", "goal", 1,
            world.session.value, start.node_id, goal.node_id,
        )
        candidate = astar_surface_plan(graph, request)
        self.assertEqual(candidate.path[1].node_id,
                         nodes[(2 + direction[0], 2 + direction[1])].node_id)
        return world, request, candidate

    def admit(self, world, request, candidate, position):
        return RouteAdmitter().admit_surface(
            candidate, frame(world, 2, position),
            expected_request_id=request.request_id,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            changed_cells=(),
        )

    def test_forward_progress_on_both_projection_sides_skips_old_center(self):
        dependency = (99, 99, 99)
        real_query = route_admission.query_standable_connection
        for direction in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            for offset in (.449, .451):
                with self.subTest(direction=direction, offset=offset):
                    world, request, candidate = self.flat_candidate(direction)
                    start = candidate.path[0].position
                    position = (
                        start[0] + direction[0] * offset,
                        start[1],
                        start[2] + direction[1] * offset,
                    )

                    def tagged_query(*args, **kwargs):
                        result = real_query(*args, **kwargs)
                        return replace(result, dependencies=tuple(sorted({
                            *result.dependencies, dependency,
                        })))

                    with patch.object(
                        route_admission, "query_standable_connection",
                        side_effect=tagged_query,
                    ) as direct:
                        admitted = self.admit(
                            world, request, candidate, position,
                        )

                    self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
                    self.assertEqual(direct.call_count, 1)
                    route = admitted.route
                    self.assertIsNotNone(route)
                    action = route.action_route.actions[0]
                    self.assertIs(type(action), WalkSegment)
                    points = tuple(
                        (point.x, point.y, point.z)
                        for point in action.fixed_route.points
                    )
                    self.assertEqual(points[0], position)
                    self.assertEqual(points[1], candidate.path[1].position)
                    self.assertNotIn(candidate.path[0].position, points[1:])
                    self.assertEqual(
                        route.corridor.node_ids[0], candidate.path[1].node_id,
                    )
                    self.assertAlmostEqual(
                        route.connection_length_blocks,
                        math.dist(position, candidate.path[1].position),
                    )
                    action_length = sum(
                        math.dist(first, second)
                        for first, second in zip(points, points[1:])
                    )
                    self.assertAlmostEqual(
                        route.fixed_route_length_blocks, action_length,
                    )
                    self.assertAlmostEqual(
                        route.corridor.length_blocks, action_length,
                    )
                    for dependencies in (
                        route.connection_dependencies,
                        route.corridor.dependencies,
                        route.action_route.dependencies,
                    ):
                        self.assertIn(dependency, dependencies)
                    tracker = ActiveRouteTracker(route, candidate)
                    tracked = tracker.update(0).route
                    self.assertEqual(
                        tracked.corridor.node_ids[0], candidate.path[1].node_id,
                    )
                    graph_edge_length = math.dist(
                        candidate.path[1].position,
                        candidate.path[2].position,
                    )
                    advanced = tracker.update(
                        route.connection_length_blocks + graph_edge_length,
                    ).route
                    self.assertEqual(
                        advanced.corridor.node_ids[0], candidate.path[2].node_id,
                    )

    def test_real_reverse_keeps_the_start_center(self):
        direction = (0, -1)
        world, request, candidate = self.flat_candidate(direction)
        start = candidate.path[0].position
        position = (start[0], start[1], start[2] + .451)
        with patch.object(
            route_admission, "query_standable_connection",
            wraps=route_admission.query_standable_connection,
        ) as direct:
            admitted = self.admit(world, request, candidate, position)

        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        self.assertEqual(direct.call_count, 0)
        action = admitted.route.action_route.actions[0]
        self.assertEqual(
            tuple((point.x, point.y, point.z)
                  for point in action.fixed_route.points[:3]),
            (position, candidate.path[0].position, candidate.path[1].position),
        )

    def test_unproved_direct_connection_keeps_the_start_center(self):
        direction = (0, 1)
        for status in (
            QueryStatus.BLOCKED,
            QueryStatus.NEEDS_INFORMATION,
            QueryStatus.UNSUPPORTED,
        ):
            with self.subTest(status=status):
                world, request, candidate = self.flat_candidate(direction)
                start = candidate.path[0].position
                position = (start[0], start[1], start[2] + .451)
                missing = ((98, 98, 98),) if (
                    status is QueryStatus.NEEDS_INFORMATION
                ) else ()
                result = StandablePointResult(
                    status, dependencies=((99, 99, 99),),
                    missing_cells=missing,
                )
                with patch.object(
                    route_admission, "query_standable_connection",
                    return_value=result,
                ) as direct:
                    admitted = self.admit(
                        world, request, candidate, position,
                    )

                self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
                self.assertEqual(direct.call_count, 1)
                action = admitted.route.action_route.actions[0]
                self.assertEqual(
                    tuple((point.x, point.y, point.z)
                          for point in action.fixed_route.points[:3]),
                    (position, candidate.path[0].position,
                     candidate.path[1].position),
                )

    def test_height_action_is_not_rewritten_as_a_ground_connection(self):
        world, candidate, request = (
            test_b07_step_route.B07StepRouteTests().candidate()
        )
        position = candidate.path[0].position
        with patch.object(
            route_admission, "query_standable_connection",
            wraps=route_admission.query_standable_connection,
        ) as direct:
            admitted = self.admit(world, request, candidate, position)

        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        self.assertEqual(direct.call_count, 0)
        self.assertIs(
            type(admitted.route.action_route.actions[0]), StepSegment,
        )

    def test_ground_segment_after_air_is_not_treated_as_route_entry(self):
        world = known_world({
            (0, 0, 1): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 3): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 4): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 0, 1, 1, 1, 4, True),
            ground_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        nodes = {node.node_id.column_z: node for node in graph.nodes}
        request = SurfacePlanningRequest(
            1, "d057-air-then-ground", "goal", 1, world.session.value,
            nodes[1].node_id, nodes[4].node_id,
            initial_resources=ResourceState((("food_points", 20.0),)),
        )
        candidate = astar_surface_plan(graph, request)
        initial = air_frame(
            world, 2, candidate.path[0].position, (0.0, 0.0, 0.0),
            on_ground=True,
        )
        with patch.object(
            route_admission, "query_standable_connection",
            wraps=route_admission.query_standable_connection,
        ) as direct:
            admitted = RouteAdmitter().admit_surface(
                candidate, initial,
                expected_request_id=request.request_id,
                goal_id=request.goal_id,
                goal_revision=request.goal_revision,
                changed_cells=(),
            )

        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        self.assertEqual(direct.call_count, 0)
        actions = admitted.route.action_route.actions
        self.assertIs(type(actions[0]), JumpGapSegment)
        ground = next(action for action in actions if type(action) is WalkSegment)
        self.assertEqual(
            tuple((point.x, point.y, point.z)
                  for point in ground.fixed_route.points),
            (candidate.path[-2].position, candidate.path[-1].position),
        )


if __name__ == "__main__":
    unittest.main()
