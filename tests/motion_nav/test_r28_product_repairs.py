"""Product behaviour through Runtime, plus the shared goal geometry boundary."""
from dataclasses import replace
import unittest

from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId
from mc2p.motion_nav.support_surfaces import HorizontalRegion, SupportSurface, SurfaceNodeId
from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run, lane
from tests.sim.scenarios import half_steps, columns
from tests.sim.continuous_height_matrix import matrix_scenario


class GoalRegionTests(unittest.TestCase):
    def test_noncentral_goal_and_small_center_margin_both_complete(self):
        scene = Scene({(x, 63, z): "minecraft:stone" for x in range(-3, 4) for z in range(13)},
                      ((-5, 5), (60, 68), (-2, 15)))
        for goal in ((.911, 64., 9.316), (.636, 64., 10.366), (-.323, 64., 10.37)):
            with self.subTest(goal=goal):
                result = run(Scenario("noncentral-goal", scene, (.5, 64., .5), goal, max_ticks=200))
                self.assertEqual(result.outcome, "success", result.reason)
                self.assertFalse(result.violations)

    def test_noncentral_goal_preserves_height_proof_and_finishes_tail(self):
        for scene, start, goal in (
            (lane(half_steps(), width=3), (.5, 64., .5), (.68, 64., 7.82)),
            (lane(columns([68, 68, 67, 66, 65, 64, 64]), width=3), (.5, 68., .5), (.68, 64., 6.82)),
        ):
            with self.subTest(goal=goal):
                result = run(Scenario("height-goal-tail", scene, start, goal))
                self.assertEqual(result.outcome, "success", result.reason)
                self.assertFalse(result.violations)

    def test_target_query_exposes_missing_body_cells_and_dependencies(self):
        from mc2p.motion_nav.support_surfaces import standable_point_in_region
        session = WorldSessionId("goal-region-missing")
        world = WorldKnowledge(session)
        stamp = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
        missing = (1, 2, 0)
        world.confirm_air(stamp, tuple((x, y, z) for x in range(-1, 3)
            for y in range(-1, 5) for z in range(-1, 2) if (x, y, z) != missing))
        world.observe_blocks(stamp, {(0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
                                   (1, 0, 0): BlockGeometry.full_cube("minecraft:stone")})
        surface = SupportSurface(SurfaceNodeId(0, 0, 1, 0), (.5, 1., .5),
                                 HorizontalRegion(0., 0., 1., 1.), 1., ("minecraft:stone",), ((0, 0, 0),))
        result = standable_point_in_region(world.view(), surface, Aabb(.91, .99, .4, .99, 1.01, .6))
        self.assertIs(result.status, QueryStatus.NEEDS_INFORMATION)
        self.assertIn(missing, result.missing_cells)
        world.confirm_air(ObservationStamp(session, 2, 2, "test-clock", 100_000_000), (missing,))
        result = standable_point_in_region(world.view(), surface, Aabb(.91, .99, .4, .99, 1.01, .6))
        self.assertIs(result.status, QueryStatus.FEASIBLE)
        self.assertIn(missing, result.dependencies)
        self.assertAlmostEqual(result.position[0], .95)


    def test_goal_tail_dependency_invalidates_the_active_route(self):
        from mc2p.motion_nav.known_map_planner import KnownMapBounds, SurfacePlanningRequest, astar_surface_plan, build_surface_graph
        from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
        from mc2p.motion_nav.route_admission import RouteAdmitter, ActiveRouteTracker, CorridorStatus, AdmissionStatus
        from tests.motion_nav.test_b07_step_transition import frame, profile as step_profile
        from tests.motion_nav.test_b07_surface_planning import ordinary_profile
        from tests.motion_nav.test_navigation_session import _known_world
        world = _known_world({(0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
                              (1, 0, 0): BlockGeometry.full_cube("minecraft:stone")})
        graph = build_surface_graph(world.view(), KnownMapBounds(0, 1, 1, 1, 0, 0, True), ordinary_profile(), step_profile())
        nodes = sorted(graph.nodes, key=lambda node: node.position[0])
        goal = GoalState(Aabb(1.72, .99, .4, 1.92, 1.01, .6), GoalSupport.SOLID,
                         frozenset({MovementMode.WALK}), frozenset({"standing"}), .6)
        request = SurfacePlanningRequest(1, "goal-tail-deps", "goal", 1, world.session.value,
                                         nodes[0].node_id, nodes[-1].node_id, goal_state=goal)
        candidate = astar_surface_plan(graph, request)
        admitted = RouteAdmitter().admit_surface(candidate, frame(world, 0, nodes[0].position),
            expected_request_id=request.request_id, goal_id="goal", goal_revision=1, changed_cells=())
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        changed = (2, 1, 0)
        self.assertIn(changed, admitted.route.action_route.dependencies)
        tracker = ActiveRouteTracker(admitted.route, candidate)
        tracker.apply_changes((changed,))
        self.assertIs(tracker.update(0.).status, CorridorStatus.BLOCKED_BY_CHANGE)
