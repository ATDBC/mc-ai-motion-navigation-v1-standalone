from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests.sim.product_cases import product_scenario, player_layout
from tests.sim.product_metrics import extract_metrics, compare_metrics
from tests.sim.runner import Scenario, run


class BatchFiveTests(unittest.TestCase):
    def test_terminal_query_cache_rejects_changed_facts(self):
        from mc2p.contracts.common import ContractViolation
        from mc2p.motion_nav.world_model import WorldQueryCache, ObservationStamp, BlockGeometry
        world, _, _, _, _ = self._terminal_inputs()
        view = world.view()
        cache = WorldQueryCache(view)
        cache.cell((1,0,0))
        cache.validate_for(view)
        world.observe_blocks(ObservationStamp(world.session,2,2,'test-clock',100_000_000),
                             {(1,0,0):BlockGeometry.full_cube('minecraft:grass_block')})
        with self.assertRaises(ContractViolation):
            cache.validate_for(view)

    def _terminal_inputs(self):
        from tests.motion_nav.test_navigation_session import _known_world, _ground_anchor
        from tests.motion_nav.test_b07_step_transition import frame
        from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
        from mc2p.motion_nav.world_model import BlockGeometry
        from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
        world = _known_world({(x,0,z):BlockGeometry.full_cube('minecraft:stone')
                              for x in range(-2,4) for z in range(-2,3)})
        initial = frame(world,0,(.5,1.,.5))
        surface = __import__('mc2p.motion_nav.support_surfaces',fromlist=['x'])
        node = surface.SupportSurface(surface.SurfaceNodeId(1,0,1,0),(1.5,1.,.5),
            surface.HorizontalRegion(1.,0.,2.,1.),1.,('minecraft:stone',),((1,0,0),))
        goal = GoalState(__import__('mc2p.motion_nav.world_model',fromlist=['x']).Aabb(1.3,.99,.3,1.7,1.01,.7),
                         GoalSupport.SOLID,frozenset({MovementMode.WALK}),frozenset({'standing'}),.6)
        return world, node, goal, NavigationSessionProfiles.load(Path('config/motion-navigation')).ground, _ground_anchor(initial).physics_state

    def test_terminal_screen_is_bounded_and_retains_the_chosen_endpoint(self):
        import time
        from mc2p.motion_nav.terminal_approach import select_terminal_approach, TerminalApproachStatus
        world,node,goal,profile,state = self._terminal_inputs()
        result = select_terminal_approach(world.view(),node,goal,(.5,1.,.5),profile,state,
                                          deadline_ns=time.perf_counter_ns()+50_000_000)
        self.assertIs(result.status,TerminalApproachStatus.FEASIBLE,result)
        self.assertEqual(result.position,(1.5,1.,.5))
        self.assertLessEqual(result.candidate_count,6)
        self.assertLessEqual(result.rollout_ticks,128)
        self.assertEqual(result.connection.points[-1].x,result.position[0])
        self.assertIn((2,1,0),result.dependencies)
        from dataclasses import replace
        from mc2p.motion_nav.terminal_approach import query_terminal_approach, TerminalApproachReason
        mismatched = replace(result.entry_window, reference_point=(20.,1.,.5))
        refused = query_terminal_approach(world.view(),node,goal,result.connection,
            mismatched,profile,deadline_ns=time.perf_counter_ns()+50_000_000,template=state)
        self.assertIs(refused.reason,TerminalApproachReason.ENTRY_UNSUPPORTED)
        self.assertEqual(refused.rollout_ticks,0)
        expired = select_terminal_approach(world.view(),node,goal,(.5,1.,.5),profile,state,deadline_ns=0)
        self.assertIs(expired.status,TerminalApproachStatus.BUDGET_EXHAUSTED)

    def test_terminal_candidates_have_interior_stop_margin(self):
        from dataclasses import replace
        from mc2p.motion_nav.terminal_approach import terminal_points
        from mc2p.motion_nav.world_model import Aabb
        _,node,goal,_,_ = self._terminal_inputs()
        for x,y,z in terminal_points(node,goal):
            self.assertGreater(min(x-goal.region.min_x,goal.region.max_x-x,z-goal.region.min_z,goal.region.max_z-z),.035)
        self.assertFalse(terminal_points(node,replace(goal,region=Aabb(1.49,.99,.49,1.51,1.01,.51))))

    def test_exact_connection_never_reselects_a_goal_point_or_crosses_wall(self):
        from mc2p.motion_nav.support_surfaces import query_standable_connection
        from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp
        from mc2p.motion_nav.geometry import QueryStatus
        world,node,_,_,_ = self._terminal_inputs()
        point=(1.62,1.,.62)
        self.assertEqual(query_standable_connection(world.view(),node,point,(.5,1.,.5)).position,point)
        world.observe_blocks(ObservationStamp(world.session,2,2,'test-clock',100_000_000),
                             {(0,1,0):BlockGeometry.full_cube('minecraft:stone')})
        self.assertIs(query_standable_connection(world.view(),node,point,(.5,1.,.5)).status,QueryStatus.BLOCKED)

    def test_applied_input_is_associated_with_its_actual_winning_owner(self):
        scene,start,goal = player_layout('player_corridor_middle')
        r=run(Scenario('body-attribution',scene,start,goal))
        self.assertEqual(r.outcome,'success',r.reason)
        for row in r.trace:
            if row['applied_request'] is not None and (row['applied_movement']['forward'] or row['applied_movement']['strafe']):
                self.assertIsNotNone(row['applied_body_activity'],row)
                self.assertTrue(row['applied_body_activity']['owner_id'].startswith('route/'))

    def test_player_manifest_keeps_every_v2_input(self):
        old = json.loads(Path('tests/sim/manifests/navigation-product-r28-v2.json').read_text('utf-8'))
        new = json.loads(Path('tests/sim/manifests/navigation-product-r28-v3.json').read_text('utf-8'))
        self.assertEqual(old['groups'], new['groups'][:8])
        for group in old['groups']:
            for seed in (0, 13, 199):
                a, pa = product_scenario(old, group, seed)
                b, pb = product_scenario(new, group, seed)
                self.assertEqual(pa, pb)
                self.assertEqual((a.start, a.goal), (b.start, b.goal))

    def test_ground_stall_tolerance_does_not_relax_strict_stalls(self):
        from tests.motion_nav.test_navigation_product_metrics import frames
        a = extract_metrics(frames(), start_tick=1, start_position=(0.,64.,0.), outcome='success')
        b = deepcopy(a)
        b['net_stall_ticks']['walking'] += 2
        self.assertEqual(compare_metrics(a,b,tick_tolerance=2)['status'],'equivalent')
        b['net_stall_ticks']['strict_preparation'] += 1
        self.assertEqual(compare_metrics(a,b,tick_tolerance=2)['status'],'different')

    def test_recovering_route_keeps_real_owner_and_typed_phase(self):
        manifest = json.loads(Path('tests/sim/manifests/navigation-product-r28-v2.json').read_text('utf-8'))
        group = next(g for g in manifest['groups'] if g['id']=='drop-late')
        found = []
        for seed in (1, 4):
            scenario, _ = product_scenario(manifest, group, seed)
            result = run(scenario)
            for row in result.trace:
                if row['driver_reason'] in {'recovering_grounded_verified_entry','settling_grounded_verified_entry','releasing_grounded_verified_entry'}:
                    self.assertTrue(row['body_control_activities'], row)
                    self.assertEqual(row['body_control_activities'][0]['phase'], 'entry_recovery')
                    self.assertTrue(row['body_control_activities'][0]['owner_id'].startswith('route/'))
                    found.append(row)
        self.assertTrue(found, 'fixture must reach the real recovery path')

    def test_free_fall_is_not_a_walking_stall(self):
        from tests.motion_nav.test_navigation_product_metrics import frames
        rows = [dict(frames()[0], movement_tick=t, position=(0.,64.-t*.15,0.),
                     body_control_activities=[dict(phase='strict_execution',owner_id='route/a')],
                     on_ground=False) for t in range(2,25)]
        metrics = extract_metrics(rows, start_tick=1,start_position=(0.,64.,0.),outcome='failed')
        self.assertEqual(metrics['net_stall_ticks']['walking'],0)
        self.assertEqual(metrics['net_stall_ticks']['strict_execution'],0)

    def test_wall_target_is_early_typed_rejection_or_success(self):
        scene, start, goal = player_layout('player_wall_head')
        result = run(Scenario('wall-terminal',scene,start,goal,max_ticks=300))
        self.assertFalse(result.violations)
        if result.outcome != 'success':
            self.assertIn('terminal_approach',result.reason)
            self.assertLess(result.ticks,100)


if __name__ == '__main__':
    unittest.main()
