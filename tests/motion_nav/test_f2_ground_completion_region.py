"""Original-goal terminal regressions, before any region implementation."""
import unittest
from dataclasses import replace

from tests.sim.product_cases import player_layout
from tests.sim.runner import Scenario, run
from tests.sim.backend import CalculatorBackend, Scene
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp
from mc2p.motion_nav.support_surfaces import query_support_surfaces, standable_region_in_goal
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion, GroundRouteExecutionContract
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from scripts.f2_ground_route_evidence import ROOT, _frame
from mc2p.contracts.common import ContractViolation


PROFILES = NavigationSessionProfiles.load(ROOT / 'config/motion-navigation')


def region_fixture(family='player_wall_head'):
    scene, start, goal = player_layout(family)
    backend = CalculatorBackend([0], scene, start, 0.)
    surface = query_support_surfaces(backend.world._world,
        int(goal[0]), int(goal[2]), 64., 64.).surfaces[0]
    from tests.sim.runner import _goal
    return backend, surface, _goal(goal)


class GroundCompletionRegionTests(unittest.TestCase):
    def test_goal_boundary_tail_differences_are_typed_and_never_enlarge_goal(self):
        scene = Scene({(x, 63, z): 'minecraft:stone'
            for x in range(-2, 3) for z in range(-2, 3)}, ((-3, 4), (60, 68), (-3, 4)))
        backend = CalculatorBackend([0], scene, (.5, 64., .5), 0.)
        surface = query_support_surfaces(backend.world._world, 0, 0, 64., 64.).surfaces[0]
        for axis in ('x', 'y', 'z'):
            for side in ('lower', 'upper'):
                for delta in (-5.e-10, 0., 5.e-10):
                    coordinates = [.3, 63.9, .3, .7, 64.1, .7]
                    index = ('x', 'y', 'z').index(axis)
                    boundary = (64. if axis == 'y' else
                        getattr(surface.region, ('max_' if side == 'lower' else 'min_')+axis))
                    if side == 'lower':
                        coordinates[index], coordinates[index+3] = boundary+delta, boundary+.1
                    else:
                        coordinates[index], coordinates[index+3] = boundary-.1, boundary-delta
                    goal = Aabb(*coordinates)
                    with self.subTest(axis=axis, side=side, delta=delta):
                        selected = standable_region_in_goal(backend.world._world, surface, goal)
                        expected = (QueryStatus.FEASIBLE if delta < 0 or (axis == 'y' and delta == 0)
                                    else QueryStatus.BLOCKED)
                        self.assertIs(selected.status, expected)
                        if selected.completion_region is not None:
                            region = selected.completion_region
                            self.assertTrue(all(lo <= value <= hi for lo, value, hi in
                                zip(goal.as_tuple()[:3], region.reference_point, goal.as_tuple()[3:])))
                            self.assertTrue(all(lo <= value for lo, value in
                                zip(goal.as_tuple()[:3], region.bounds.as_tuple()[:3])))
                            self.assertTrue(all(value <= hi for value, hi in
                                zip(region.bounds.as_tuple()[3:], goal.as_tuple()[3:])))

    def test_completion_region_does_not_allow_mid_route_head_wall(self):
        from tests.sim.runner import lane
        from mc2p.motion_nav.online_motion import project_movement_command
        from mc2p.motion_nav.physics_1_21 import step
        from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, CalculationStatus
        scene = lane([[63]] * 10, width=3)
        scene = replace(scene, solids={**scene.solids,
            **{(x, y, 4): 'minecraft:stone' for x in range(3) for y in (64, 65)}})
        backend = CalculatorBackend([0], scene, (.5, 64., 3.69), 0.)
        bounds = Aabb(.3, 63.99, 8.4, .7, 64.01, 8.8)
        region = GroundCompletionRegion(bounds, (.5, 64., 8.6), 64., (0, 8, 64, 0), ())
        route = FixedRoute('region-head-wall', (RoutePoint(.5, 64., .5), RoutePoint(*region.reference_point)),
            GroundRouteExecutionContract.for_completion(region, (), PROFILES.ground.profile_id))
        controller = FixedRouteController(PROFILES.ground)
        state = backend.state
        controller.start(route, _frame(state, backend.world._world, 0))
        for tick in range(1, 81):
            decision = controller.decide(_frame(state, backend.world._world, tick), physics_state=state)
            self.assertLessEqual(decision.full_candidates, 3)
            self.assertNotEqual(decision.state, FixedRouteState.SUCCEEDED)
            if decision.state in {FixedRouteState.NEEDS_REPLAN, FixedRouteState.BLOCKED}:
                break
            projection = project_movement_command(state, decision.movement)
            result = step(state, projection.tick_input, backend.world, JAVA_1_21_RULESET)
            self.assertIs(result.status, CalculationStatus.OK)
            state = result.next_state
        self.assertIn(decision.state, {FixedRouteState.NEEDS_REPLAN, FixedRouteState.BLOCKED})
        self.assertLess(state.position[2], 4.)

    def test_all_formal_direct_entries_bind_same_region_contract(self):
        from tests.motion_nav.test_d062_direct_walk_validation import D062DirectWalkValidationTests
        from tests.motion_nav.test_d064_ground_direct_handoff import D064GroundDirectAdmissionTests
        from tests.motion_nav.test_d060_terminal_node_exact_proof import _world
        for result in (D062DirectWalkValidationTests._admit_local(_world(62,16)),
                       D064GroundDirectAdmissionTests()._admit(_world(64,16))):
            route=result.route.action_route.actions[-1].fixed_route
            self.assertIsNotNone(route.execution_contract)
            self.assertIsNotNone(route.execution_contract.completion_region)
            self.assertEqual(route.execution_contract.completion_region.reference_point,
                             (route.points[-1].x,route.points[-1].y,route.points[-1].z))

    def test_region_proof_replays_equivalent_facts_and_stops_changed_rectangle(self):
        from tests.motion_nav.test_d062_direct_walk_validation import D062DirectWalkValidationTests
        from tests.motion_nav.test_d060_terminal_node_exact_proof import _world
        from tests.motion_nav.test_b07_surface_planning import ordinary_profile
        from mc2p.motion_nav.route_admission import ActiveRouteTracker
        from mc2p.motion_nav.route_validation import ActiveRouteValidationDisposition, RouteValidationBudget
        world=_world(62,16)
        result=D062DirectWalkValidationTests._admit_local(world)
        tracker=ActiveRouteTracker(result.route)
        for sequence, cell in ((2,(0,-60,9)),(3,(0,-59,10))):
            stamp=ObservationStamp(world.session,sequence,sequence,'region-proof',sequence*50_000_000)
            world.confirm_air(stamp,(cell,))
            decision=tracker.validate(world.view(),(cell,),ground_profile=ordinary_profile(),
                                      budget=RouteValidationBudget(4))
            self.assertIs(decision.disposition,ActiveRouteValidationDisposition.CONTINUE)
            self.assertEqual(decision.queries_used,2)
        changed=(0,-59,10)
        world.observe_blocks(ObservationStamp(world.session,4,4,'region-proof',200_000_000),
            {changed:BlockGeometry('minecraft:stone','boxes',(Aabb(0.,0.,.2,1.,1.,1.),))})
        from mc2p.motion_nav.route_validation import replay_walk_validation_recipe, WalkValidationQueryKind
        point=next(r for r in result.route.validation_plan.recipes
                   if r.query_kind is WalkValidationQueryKind.STANDABLE_CONNECTION)
        self.assertIs(replay_walk_validation_recipe(point,world.view())[0],QueryStatus.FEASIBLE)
        decision=tracker.validate(world.view(),(changed,),ground_profile=ordinary_profile())
        self.assertIs(decision.disposition,ActiveRouteValidationDisposition.STOP)
        self.assertEqual(decision.queries_used,2)


    def test_non_roundtrip_geometry_and_position_mismatch_never_replays(self):
        backend, surface, goal = region_fixture('player_corridor_end')
        state = replace(backend.state, position=(.5, 64., 9.4))
        frame = _frame(state, backend.world._world, 0)
        selected = standable_region_in_goal(frame.world, surface, goal.region)
        region = selected.completion_region
        route = FixedRoute('mismatch', (RoutePoint(.5, 64., .5), RoutePoint(*region.reference_point)),
            GroundRouteExecutionContract(region.dependencies, PROFILES.ground.profile_id,
                                         completion_region=region))
        for wrong in (replace(state, body_width=state.body_width+1e-7),
                      replace(state, position=(.5+1e-10, 64., 9.4))):
            controller = FixedRouteController(PROFILES.ground)
            controller.start(route, frame)
            decision = controller.decide(_frame(state, frame.world, 1), physics_state=wrong)
            self.assertEqual(decision.full_candidates, 0)
            self.assertNotEqual(decision.state, FixedRouteState.SUCCEEDED)

    def test_region_is_exact_bounded_immutable_and_keeps_wall_dependencies(self):
        backend, surface, goal = region_fixture()
        selected = standable_region_in_goal(backend.world._world, surface, goal.region)
        self.assertIs(selected.status, QueryStatus.FEASIBLE)
        region = selected.completion_region
        self.assertAlmostEqual(region.bounds.max_x, 1.7)
        self.assertIn((2, 64, 6), region.dependencies)
        self.assertTrue(all(lo <= hi for lo, hi in zip(goal.region.as_tuple()[:3], region.bounds.as_tuple()[:3])))
        self.assertTrue(all(lo <= hi for lo, hi in zip(region.bounds.as_tuple()[3:], goal.region.as_tuple()[3:])))
        with self.assertRaises((AttributeError, TypeError)):
            region.reference_point = (.5, 64., .5)
        with self.assertRaises(ContractViolation):
            replace(region, reference_point=(2., 64., 6.5))

    def test_no_intersection_unknown_and_low_ceiling_are_typed_rejections(self):
        backend, surface, goal = region_fixture()
        self.assertIs(standable_region_in_goal(backend.world._world, surface,
            Aabb(4., 64., 4., 5., 64.1, 5.)).status, QueryStatus.BLOCKED)
        stamp = ObservationStamp(backend.state.session, 2, 2, 'region-test', 100_000_000)
        backend.truth.invalidate(stamp, ((1, 64, 6),))
        self.assertIs(standable_region_in_goal(backend.truth.view(), surface, goal.region).status,
                      QueryStatus.NEEDS_INFORMATION)
        backend, surface, goal = region_fixture()
        backend.truth.observe_blocks(stamp, {(1, 65, 6): BlockGeometry.full_cube('minecraft:stone')})
        self.assertIs(standable_region_in_goal(backend.truth.view(), surface, goal.region).status,
                      QueryStatus.BLOCKED)

    def test_hazard_fluid_and_unsupported_contact_cannot_form_region(self):
        for block in (BlockGeometry.full_cube('minecraft:magma_block'),
                      BlockGeometry.empty('minecraft:water', fluid=True),
                      BlockGeometry.unsupported('minecraft:oak_fence')):
            backend, surface, goal = region_fixture()
            stamp = ObservationStamp(backend.state.session, 2, 2, 'region-test', 100_000_000)
            backend.truth.observe_blocks(stamp, {(2, 64, 6): block})
            selected = standable_region_in_goal(backend.truth.view(), surface, goal.region,
                allowed_materials=PROFILES.ground.support_materials)
            self.assertIs(selected.status, QueryStatus.UNSUPPORTED)

    def test_support_threshold_cutting_rectangle_is_not_approximated(self):
        scene = Scene({(0, 63, 0): 'minecraft:stone'}, ((-2, 3), (60, 68), (-2, 3)))
        backend = CalculatorBackend([0], scene, (.5, 64., .5), 0.)
        surface = query_support_surfaces(backend.world._world, 0, 0, 64., 64.).surfaces[0]
        selected = standable_region_in_goal(backend.world._world, surface,
            # This goal lies inside one support-grid cell. The 0.5 contour
            # cuts that cell, so no positive exact rectangle may be invented.
            Aabb(.8, 63.99, .8, 1., 64.01, 1.))
        self.assertIs(selected.status, QueryStatus.UNSUPPORTED)

    def test_observed_region_completion_can_precede_reference(self):
        backend, surface, goal = region_fixture('player_corridor_end')
        region = standable_region_in_goal(backend.world._world, surface, goal.region).completion_region
        state = replace(backend.state, position=(.5, 64., 9.55), velocity_blocks_per_tick=(0., 0., 0.))
        route = FixedRoute('region-before-guide', (RoutePoint(.5, 64., .5), RoutePoint(*region.reference_point)),
            GroundRouteExecutionContract(region.dependencies, PROFILES.ground.profile_id,
                                         completion_region=region))
        controller = FixedRouteController(PROFILES.ground)
        controller.start(route, _frame(state, backend.world._world, 0))
        self.assertNotEqual(state.position, region.reference_point)
        decision = controller.decide(_frame(state, backend.world._world, 1), physics_state=state)
        self.assertIs(decision.state, FixedRouteState.SUCCEEDED)
        backend.truth.observe_blocks(ObservationStamp(state.session, 2, 2, 'support-removed', 100_000_000),
            {(0, 63, 9): BlockGeometry.empty('minecraft:air')})
        controller = FixedRouteController(PROFILES.ground)
        controller.start(route, _frame(state, backend.truth.view(), 0))
        decision = controller.decide(_frame(state, backend.truth.view(), 1), physics_state=state)
        self.assertNotEqual(decision.state, FixedRouteState.SUCCEEDED)

    def test_same_observation_body_box_roundtrip_matches(self):
        from scripts.f2_ground_route_evidence import _frame
        from mc2p.motion_nav.safe_ground_control import ground_route_state_matches
        from tests.sim.backend import CalculatorBackend
        scene, start, _ = player_layout('player_corridor_end')
        backend = CalculatorBackend([0], scene, start, 0.)
        frame = _frame(backend.state, backend.world._world, 0)
        from mc2p.motion_nav.world_model import Aabb
        frame = replace(frame, body=replace(frame.body,
            body_box=Aabb(.2, 64., .2, .8, 65.8, .8)))
        reconstructed = replace(backend.state,
            body_width=frame.body.body_box.max_x-frame.body.body_box.min_x,
            body_height=frame.body.body_box.max_y-frame.body.body_box.min_y)
        self.assertNotEqual(reconstructed.body_box, frame.body.body_box)
        self.assertTrue(ground_route_state_matches(frame, reconstructed))

    def test_original_player_goals_complete_without_point_tolerance(self):
        for family in ('player_wall_head', 'player_wall_parallel', 'player_corner',
                       'player_corridor_end', 'player_ledge_1', 'player_ledge_2'):
            with self.subTest(family=family):
                scene, start, goal = player_layout(family)
                result = run(Scenario('f2-original-' + family, scene, start, goal))
                self.assertEqual(result.outcome, 'success', result.reason)
                self.assertFalse(result.violations)
                self.assertTrue(result.verification_complete)
                self.assertTrue(all(abs(a-b) <= limit for a, b, limit in
                                    zip(result.final_position, goal, (.20, .08, .20))))

    def test_four_directions_and_actual_first_input_delay(self):
        from tests.sim.continuous_height_matrix import _rotate_cell, _rotate_point, _YAW_BY_DIRECTION
        from tests.sim.backend import Perturbations
        for family in ('player_wall_head', 'player_wall_parallel', 'player_corner',
                       'player_corridor_end', 'player_ledge_1', 'player_ledge_2'):
            for direction in ('south', 'east', 'north', 'west'):
                scene, start, goal = player_layout(family)
                corners = [_rotate_cell((x, 60, z), direction)
                    for x in scene.volume[0] for z in scene.volume[2]]
                rotated = Scene({_rotate_cell(p, direction): b for p, b in scene.solids.items()},
                    ((min(p[0] for p in corners), max(p[0] for p in corners)), scene.volume[1],
                     (min(p[2] for p in corners), max(p[2] for p in corners))))
                scenario = Scenario(family+'-'+direction, rotated, _rotate_point(start, direction),
                                    _rotate_point(goal, direction), _YAW_BY_DIRECTION[direction])
                with self.subTest(family=family, direction=direction, delivery='normal'):
                    normal = run(scenario)
                    self.assertEqual(normal.outcome, 'success', normal.reason)
                    self.assertFalse(normal.violations)
                first = next(row['movement_tick'] for row in normal.trace
                             if row['applied_movement']['forward'] or row['applied_movement']['strafe'])
                with self.subTest(family=family, direction=direction, delivery='first_late'):
                    delayed = run(replace(scenario, perturbations=Perturbations(late_ticks=frozenset({first}))))
                    self.assertIn(('late_input', first), delayed.applied_perturbations)
                    self.assertEqual(delayed.outcome, 'success', delayed.reason)
                    self.assertFalse(delayed.violations)
                    self.assertTrue(delayed.verification_complete)


if __name__ == '__main__':
    unittest.main()
