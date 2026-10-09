"""D093: stopping and strict-input loss must remain distinct from arrival."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteConfig, FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion, GroundRouteExecutionContract
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.segment_entry import SegmentEntryWindow
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.body_control import StopCause
from tests.motion_nav.test_fixed_route_walk import FlatFixture, profile
from scripts.action_entry_late_hardening import run_case


class ActionEntryLateHardeningTests(unittest.TestCase):
    def test_red_collector_accepts_legacy_terminal_report_without_failure_cause(self):
        old_report = SimpleNamespace(terminal=True)
        ticks = []

        def legacy_run(scenario, *, control_step):
            context = SimpleNamespace(
                driver=SimpleNamespace(tick=lambda *args: ticks.append(args)),
                clock=[0], session=SimpleNamespace(report=old_report),
            )
            control_step(context)
            return SimpleNamespace(outcome='success', reason='goal_state_satisfied',
                ticks=1, final_position=scenario.start, damage=0., violations=(), trace=())

        with patch('scripts.action_entry_late_hardening.run', side_effect=legacy_run):
            result = run_case('column_outer_turn', None)
        self.assertFalse(hasattr(old_report, 'failure_cause'))
        self.assertEqual(len(ticks), 1)
        self.assertIsNone(result['task_failure_cause'])
        self.assertEqual(result['outcome'], 'success')

    def fixture(self, **entry_changes):
        fixture = FlatFixture()
        body = PlanarBodyState(.5, 2.55, 0., .6, 0.)
        window = SegmentEntryWindow((.5, 1., 2.5), (0., 1.), -.16, .16, .16,
            .99, 1.01, 0., .1, 3.141592653589793, frozenset({'standing'}),
            frozenset({MovementMode.WALK}), 0., .035, 'entry-test')
        window = replace(window, **entry_changes)
        completion = GroundCompletionRegion(Aabb(.34, .99, 2.34, .66, 1.01, 2.66),
            (.5, 1., 2.5), 1., (0, 0, 2, 0), ())
        route = FixedRoute('entry-brake', (RoutePoint(.5, 1., .5), RoutePoint(.5, 1., 2.5)),
            execution_contract=GroundRouteExecutionContract.for_completion(completion, (), profile().profile_id))
        controller = FixedRouteController(profile(), FixedRouteConfig(handoff_entry_window=window))
        controller.start(route, fixture.frame(0, body))
        return controller, fixture.frame(1, body)

    def test_stopping_into_entry_brakes_instead_of_tracking_endpoint(self):
        controller, frame = self.fixture()
        decision = controller.decide(frame)
        self.assertIs(decision.state, FixedRouteState.BRAKING)
        self.assertEqual(decision.reason, 'goal_braking')
        self.assertNotEqual(decision.state, FixedRouteState.SUCCEEDED)

    def test_wrong_yaw_or_minimum_entry_speed_does_not_acquire_stopping_permission(self):
        for changes in ({'required_yaw_radians': 1.}, {'minimum_speed_blocks_per_second': .05}):
            with self.subTest(changes=changes):
                controller, frame = self.fixture(**changes)
                decision = controller.decide(frame)
                self.assertNotEqual(decision.reason, 'goal_braking')

    def test_random_late_outer_corner_finishes(self):
        result = run_case('column_outer_turn', 1)
        self.assertEqual(result['outcome'], 'success', result['reason'])
        self.assertFalse(result['violations'])

    def test_final_jump_safe_landing_short_of_goal_reports_input_loss(self):
        from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
        from mc2p.motion_nav.motion_candidate import VerifiedMotionExecutorState
        observed = []
        advance = ActionRouteExecutor._advance

        def record(executor, *args, **kwargs):
            if kwargs.get('completed_after_input_loss'):
                observed.append(executor._controller.state)
            decision = advance(executor, *args, **kwargs)
            if kwargs.get('completed_after_input_loss'):
                observed.append(decision.state)
            return decision

        with patch.object(ActionRouteExecutor, '_advance', record):
            result = run_case('column_landing_turn', 4)
        self.assertEqual(observed, [VerifiedMotionExecutorState.INPUT_LOST, ActionRouteState.FAILED])
        self.assertEqual(result['reason'], 'input_lost_after_safe_landing_before_goal')
        self.assertEqual(result['outcome'], 'failed')
        self.assertFalse(result['violations'])
        self.assertEqual(result['damage'], 0)

    def test_input_loss_classification_does_not_restart_safe_landing(self):
        import json, gzip
        from pathlib import Path
        from mc2p.contracts.behavior import BehaviorProfileV0
        from tests.sim.runner import run
        from scripts.action_entry_late_hardening import scenario_for
        reports=[]

        def step(context):
            context.driver.tick(BehaviorProfileV0(),context.clock[0]+500_000_000)
            if context.session.report.terminal:
                reports.append(context.session.report)
            return ()

        result=run(scenario_for('column_landing_turn',4),control_step=step)
        self.assertTrue(reports)
        self.assertIs(getattr(reports[-1], 'failure_cause', None), StopCause.INPUT_LOST)
        baseline=Path(__file__).resolve().parents[2]/'evidence/motion_navigation/action-entry-late-hardening-v1/h1-only-landing-baseline/runs.jsonl.gz'
        with gzip.open(baseline,'rt') as stream:
            control=next(json.loads(line) for line in stream
                         if json.loads(line)['family']=='column_landing_turn' and json.loads(line)['seed']==4)
        def motion(trace):
            return json.loads(json.dumps([(f['movement_tick'],f['position'],f['velocity'],f['on_ground'],
                     f['applied_movement'],f['action_kind'],f['input_window']) for f in trace]))
        self.assertEqual(motion(result.trace),motion(control['trace']))
        self.assertEqual(result.final_position,tuple(control['final_position']))

    def test_normal_final_jump_can_still_succeed(self):
        result = run_case('column_landing_turn', None)
        self.assertEqual(result['outcome'], 'success', result['reason'])

    def test_normal_goal_mismatch_does_not_become_input_loss(self):
        from mc2p.motion_nav.goal_observation import ObservedGoal, ObservedGoalStatus
        with patch('mc2p.motion_nav.action_route_executor.evaluate_observed_goal',
                   return_value=ObservedGoal(ObservedGoalStatus.NOT_SATISFIED)):
            result = run_case('column_landing_turn', None)
        self.assertEqual(result['reason'], 'goal_state_not_satisfied')

    def test_entry_permission_does_not_replace_walk_completion(self):
        controller, frame = self.fixture(maximum_lateral_offset_blocks=.4)
        frame = replace(frame, body=replace(frame.body, position=(.75, 1., 2.55)))
        decision = controller.decide(frame)
        self.assertNotEqual(decision.reason, 'goal_braking')

    def test_fabric_entry_late_gate_reaches_typed_walk_braking_and_rejects_air(self):
        from scripts.f2_ground_route_runtime import _entry_late_walk_gate
        from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
        gates=[]
        prepare=RuntimeNavigationDriver.prepare_proposals

        def inspect(driver,*args,**kwargs):
            proposals=prepare(driver,*args,**kwargs)
            frame=driver.runtime.navigation_observation_adapter.latest_frame
            gate=_entry_late_walk_gate(driver.session,frame,proposals)
            if gate is not None:
                gates.append(gate)
                self.assertGreater(gate['speed_blocks_per_second'],gate['maximum_entry_speed_blocks_per_second'])
                self.assertIsNone(_entry_late_walk_gate(driver.session,
                    replace(frame,body=replace(frame.body,is_on_ground=False)),proposals))
                self.assertIsNone(_entry_late_walk_gate(driver.session,frame,()))
            return proposals

        with patch.object(RuntimeNavigationDriver,'prepare_proposals',inspect):
            result=run_case('column_outer_turn',None)
        self.assertTrue(gates)
        self.assertEqual(result['outcome'],'success')

    def test_fabric_entry_late_matrix_is_four_directions_and_changes_only_condition(self):
        from scripts.f2_ground_route_runtime import frozen_plan
        rows=frozen_plan(handoff_entry_late=True)
        baseline={r['frozen_v9_id']:r for r in frozen_plan(handoff=True)}
        self.assertEqual({r['direction'] for r in rows},{0,1,2,3})
        self.assertEqual(len(rows),4)
        for row in rows:
            self.assertEqual(row['condition'],'late_entry')
            for field in ('scene_sha256','goal_bounds','start_position'):
                self.assertEqual(row[field],baseline[row['frozen_v9_id']][field])


if __name__ == '__main__':
    unittest.main()
