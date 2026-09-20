from __future__ import annotations

import unittest

from mc2p.motion_nav.action_route import JumpUpSegment, StepSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, SurfacePlanningRequest, astar_surface_plan,
    build_surface_graph,
)
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from tests.motion_nav.test_b07_step_transition import frame, profile as step_profile
from tests.motion_nav.test_b07_surface_planning import mixed_height_world, ordinary_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_fixed_route_walk import profile as grass_profile
from tests.motion_nav.test_b07_support_surfaces import surface_world
from mc2p.motion_nav.world_model import BlockGeometry


class B07StepRouteTests(unittest.TestCase):
    def candidate(self):
        world = mixed_height_world()
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )
        start = next(node.node_id for node in graph.nodes
                     if node.position[0] == .5)
        goal = next(node.node_id for node in graph.nodes
                    if node.position[0] == 1.5)
        request = SurfacePlanningRequest(
            1, "surface-route", "surface-goal", 1,
            world.session.value, start, goal,
        )
        return world, astar_surface_plan(graph, request), request

    def test_admission_preserves_step_as_a_typed_action(self):
        world, candidate, request = self.candidate()
        initial = frame(world, 0, candidate.path[0].position)

        result = RouteAdmitter().admit_surface(
            candidate, initial, expected_request_id=request.request_id,
            goal_id=request.goal_id, goal_revision=request.goal_revision,
            changed_cells=(),
        )

        self.assertIs(result.status, AdmissionStatus.ACCEPTED)
        self.assertIsNotNone(result.route)
        self.assertEqual(len(result.route.action_route.actions), 1)
        self.assertIs(type(result.route.action_route.actions[0]), StepSegment)

    def test_changed_step_dependency_is_rejected_before_execution(self):
        world, candidate, request = self.candidate()
        initial = frame(world, 0, candidate.path[0].position)

        result = RouteAdmitter().admit_surface(
            candidate, initial, expected_request_id=request.request_id,
            goal_id=request.goal_id, goal_revision=request.goal_revision,
            changed_cells=(candidate.dependencies[0],),
        )

        self.assertIs(result.status, AdmissionStatus.REJECTED)
        self.assertEqual(result.reason, "route_dependencies_changed")

    def test_executor_runs_step_controller_and_observes_endpoint(self):
        world, candidate, request = self.candidate()
        initial = frame(world, 0, candidate.path[0].position)
        admitted = RouteAdmitter().admit_surface(
            candidate, initial, expected_request_id=request.request_id,
            goal_id=request.goal_id, goal_revision=request.goal_revision,
            changed_cells=(),
        ).route
        self.assertIsNotNone(admitted)
        executor = ActionRouteExecutor(
            ordinary_profile(), jump_profile(), step_profile(),
        )
        executor.start(admitted.action_route, initial)

        moving = executor.decide(initial)
        landed = executor.decide(frame(world, 1, candidate.path[-1].position))

        self.assertIs(moving.state, ActionRouteState.RUNNING)
        self.assertFalse(moving.movement.jump)
        self.assertIs(landed.state, ActionRouteState.COMPLETE)

    def test_surface_route_preserves_existing_jump_up_action(self):
        world = surface_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (1, 1, 0): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 1, 2, 0, 0, True),
            grass_profile(), step_profile(), jump_profile(),
        )
        start = next(node.node_id for node in graph.nodes if node.position[1] == 1)
        goal = next(node.node_id for node in graph.nodes if node.position[1] == 2)
        request = SurfacePlanningRequest(
            3, "surface-jump-route", "surface-jump-goal", 1,
            world.session.value, start, goal,
        )
        candidate = astar_surface_plan(graph, request)
        initial = frame(world, 0, candidate.path[0].position)

        admitted = RouteAdmitter().admit_surface(
            candidate, initial, expected_request_id=request.request_id,
            goal_id=request.goal_id, goal_revision=request.goal_revision,
            changed_cells=(),
        )

        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        self.assertIs(type(admitted.route.action_route.actions[0]), JumpUpSegment)


if __name__ == "__main__":
    unittest.main()
