from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.runtime.trace import trace_projection
from mc2p.skills.navigation_state import NavigationState
from mc2p.skills.point_goal import PointGoal
from mc2p.skills.point_goal_driver import PointGoalDriver
from mc2p.skills.point_goal_policy import PointGoalPolicy
from tests.test_follow_driver import FollowBackend
from tests.test_player_runtime import _RecordingTrace
from tests.follow_v3_fixtures import follow_snapshot, observed_block
from tests.test_navigation_joint_policy import TestCapabilities
from scripts.normal_navigation_evidence import _main_planning_event


class PointGoalDriverTests(unittest.TestCase):
    def setUp(self):
        self._configure(Vec3V0(.5, 64, 4.5))

    def _configure(self, goal_position, *, group='C'):
        self.clock = [100_000_000]
        self.backend = FollowBackend(self.clock)
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        reset = ResetRequestV0('reset', 'episode-1', 'test', 1, 1_000_000_000)
        self.assertTrue(self.runtime.reset(reset).succeeded)
        self.state = NavigationState('scope')
        self.policy = PointGoalPolicy(group, control_capabilities=TestCapabilities() if group in {'D', 'E','goal_directed_exploration'} else None)
        self.driver = PointGoalDriver(self.runtime, self.state, self.policy,
                                      clock_ns=lambda: self.clock[0])
        self.goal = PointGoal('goal', 'scope', goal_position,
                              self.clock[0]+10_000_000_000)
        self.profile = BehaviorProfileV0()
        self.driver.start(self.goal, self.clock[0])

    def tearDown(self):
        self.runtime.close()

    def tick(self, owner=None):
        return self.driver.tick(self.profile, owner or self.clock[0]+3_000_000_000)

    def test_tick_uses_ordered_combined_one_tick_navigation_control(self):
        result = self.tick()
        self.assertEqual(result.decision.action.movement, MovementV1(forward=1))
        rows = [payload['envelope'] for kind, payload in self.trace.records
                if kind == 'ordered_intent']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].intent.movement_requires_look)
        self.assertEqual(rows[0].intent.valid_for_ticks, 1)
        self.assertLessEqual(rows[0].intent.expires_at_monotonic_ns-
                             rows[0].intent.submitted_at_monotonic_ns,
                             250_000_000)
        self.assertEqual(self.driver.sequence, 1)
        self.assertTrue(self.driver.last_execution_confirmed)
        self.assertIn('planning_summary', self.state.policy_cache('C'))
        event = next(payload for kind, payload in self.trace.records
                     if kind == 'playground_task')
        self.assertEqual(event['planning_diagnostic'],
                         self.policy.planning_diagnostic)
        self.assertEqual(event['selected_waypoint'],
                         self.policy.selected_waypoint)
        self.assertLessEqual(len(event['planning_diagnostic']['candidates']), 16)
        self.assertLessEqual(
            len(event['planning_diagnostic']['segment_rejections']), 64)

    def test_dispatch_revalidation_reuses_the_same_observation_view(self):
        with patch.object(self.driver, "_view", wraps=self.driver._view) as view:
            result = self.tick()

        self.assertIsNotNone(result)
        self.assertEqual(view.call_count, 1)

    def test_fast_poll_neither_catches_up_nor_replays_old_frame(self):
        self.tick()
        actions = len(self.backend.actions)
        self.assertIsNone(self.tick())
        self.assertEqual(len(self.backend.actions), actions)

    def test_replace_goal_keeps_source_and_policy_but_rebinds_monitor(self):
        self.tick()
        source = self.driver.source
        attempt = self.driver.attempt_id
        next_goal = PointGoal(
            "goal-revision-2", "scope", Vec3V0(1.5, 64, 4.5),
            self.clock[0] + 9_000_000_000,
        )
        with patch.object(self.policy, "clear", wraps=self.policy.clear) as clear:
            self.driver.replace_goal(next_goal, self.clock[0])
            clear.assert_not_called()
        self.assertIs(self.driver.source, source)
        self.assertEqual(self.driver.attempt_id, attempt)
        self.assertIs(self.driver._monitor.goal, next_goal)
        self.assertEqual(self.driver._task.task_id, "point-goal/goal-revision-2")
        self.clock[0] += 50_000_000
        with patch.object(self.policy, "decide", wraps=self.policy.decide) as decide:
            self.tick()
        self.assertIs(decide.call_args.args[3], next_goal)

    def test_replace_goal_rejects_scope_deadline_terminal_and_time_regression(self):
        with self.assertRaises(ContractViolation):
            self.driver.replace_goal(
                replace(self.goal, scope_id="other"), self.clock[0],
            )
        with self.assertRaises(ContractViolation):
            self.driver.replace_goal(
                replace(self.goal, goal_id="expired", deadline_ns=self.clock[0]),
                self.clock[0],
            )
        newer = replace(self.goal, goal_id="newer")
        self.driver.replace_goal(newer, self.clock[0] + 1)
        with self.assertRaises(ContractViolation):
            self.driver.replace_goal(replace(newer, goal_id="older"), self.clock[0])
        self.driver.stop(self.profile, "done")
        with self.assertRaises(ContractViolation):
            self.driver.replace_goal(replace(newer, goal_id="after-stop"), self.clock[0] + 2)

    def test_exhausted_navigation_reports_then_releases_its_source(self):
        self.runtime.close()
        self._configure(Vec3V0(.5,64,4.5),group='goal_directed_exploration')
        self.tick()
        execution=self.policy.joint_planner.execution
        execution.problem(self.clock[0],'connection_unavailable','entry')
        execution.problem_timeout_ns=100_000_000
        self.clock[0]+=150_000_000
        self.tick()
        self.assertEqual(self.driver.reason,'navigation_recovery_problem_deadline')
        self.clock[0]+=60_000_000
        result=self.tick()
        self.assertEqual(self.driver.state,'stopped')
        self.assertEqual(result.decision.action.movement,MovementV1())

    def test_arrival_dwell_keeps_real_driver_planning_events_valid(self):
        for group in ('C', 'D', 'E'):
            with self.subTest(group=group):
                self.runtime.close()
                self._configure(Vec3V0(.5, 64, 2.5), group=group)

                def arrived_observation():
                    # Transport fixture supplies legal arrival and successive
                    # client samples; the real monitor must prove 300 ms dwell.
                    result = follow_snapshot(sequence=self.backend.sequence,
                        received=self.clock[0], position=(.5, 64, 2.5), entities=[],
                        blocks=tuple(observed_block((x, 63, z))
                                     for x in range(-1, 2) for z in range(-1, 7)))
                    return replace(result, client_sample=replace(result.client_sample,
                        started_at_monotonic_ns=10_000_000_000+self.clock[0]-1_000_000,
                        completed_at_monotonic_ns=10_000_000_000+self.clock[0]))

                self.backend.observation = arrived_observation
                with patch('time.perf_counter_ns', return_value=0):
                    first = self.tick()
                    self.assertNotEqual(first.decision.action.movement, MovementV1())
                    self.assertIsNotNone(self.policy.selected_waypoint)
                    self.assertNotEqual(self.driver.state, 'success')
                    for index in range(5):
                        self.clock[0] += 100_000_000
                        result = self.tick()
                        self.assertEqual(result.decision.action.movement, MovementV1())
                        self.assertEqual(result.decision.action.look, LookV1())
                        self.assertTrue(self.driver.last_execution_confirmed)
                        if index < 3:
                            self.assertEqual(self.driver.state, 'holding_distance')
                        if self.driver.state == 'success':
                            break
                self.assertEqual(self.driver.state, 'success')
                rows = [dict(record_type=kind, payload=trace_projection(payload))
                        for kind, payload in self.trace.records]
                events = [row['payload'] for row in rows if row['record_type'] == 'playground_task']
                self.assertGreaterEqual(len(events), 5)
                run = dict(group=group, case_plan=dict(actor_task=dict(goal_id=self.goal.goal_id)),
                           joint_configuration=dict(prelook_enabled=True, force_route_id=None))
                for event_index, row in enumerate(rows):
                    if row['record_type'] != 'playground_task':
                        continue
                    intent_index = max(i for i in range(event_index) if rows[i]['record_type'] == 'ordered_intent')
                    envelope = rows[intent_index]['payload']['envelope']
                    _main_planning_event(rows, intent_index, event_index+1, run,
                        self.driver.source, row['payload']['intent_sequence'],
                        row['payload']['observation_sequence_id'], envelope['intent'], None)
                self.assertTrue(all(event['selected_waypoint'] is None for event in events[1:]))

    def test_owner_deadline_must_leave_a_full_step_and_cannot_regress(self):
        result = self.driver.tick(self.profile, self.clock[0]+249_999_999)
        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertEqual(self.driver.state, 'stopped')
        self.assertEqual(self.driver.reason, 'owner_lease_insufficient')

    def test_higher_priority_look_preempts_bound_movement(self):
        self.runtime.submit_intent(ActionIntentV1(
            'human', 'human', 'episode-1', 0, ActionPriorityV0.PLAYER,
            self.clock[0], self.clock[0]+2_000_000_000, look=LookV1(90, 0)))
        result = self.tick()
        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertEqual(result.decision.action.look, LookV1(90, 0))
        self.assertEqual(self.driver.state, 'preempted')
        self.assertFalse(self.driver.last_execution_confirmed)

    def test_preempted_neutral_frames_cannot_accumulate_arrival_dwell(self):
        self.runtime.close()
        self._configure(Vec3V0(.5, 64, .5))
        observation = self.backend.observation

        def timed_observation():
            result = observation()
            return replace(result, client_sample=replace(
                result.client_sample,
                started_at_monotonic_ns=10_000_000_000+self.clock[0]-1_000_000,
                completed_at_monotonic_ns=10_000_000_000+self.clock[0],
            ))

        self.backend.observation = timed_observation
        self.runtime.submit_intent(ActionIntentV1(
            'human', 'human', 'episode-1', 0, ActionPriorityV0.PLAYER,
            self.clock[0], self.clock[0]+2_000_000_000, look=LookV1(90, 0)))

        first = self.tick()
        self.assertEqual(first.decision.action.movement, MovementV1())
        self.assertEqual(self.driver.state, 'preempted')
        self.runtime.cancel_source('human')
        self.clock[0] += 350_000_000

        second = self.tick()

        self.assertTrue(self.driver.last_execution_confirmed)
        self.assertNotEqual(self.driver.state, 'success')
        self.assertEqual(self.driver.state, 'holding_distance')

    def test_cancelled_receipt_is_preserved_as_nonexecution_without_resend(self):
        source = self.driver.source
        self.backend.receipt_changes = {'status': 'cancelled'}
        result = self.tick()
        self.assertEqual(result.backend_result.receipt.status, 'cancelled')
        self.assertFalse(self.driver.last_execution_confirmed)
        self.assertEqual(self.driver.state, 'cancelled')
        self.assertEqual(len(self.backend.actions), 1)
        with self.assertRaises(ContractViolation):
            self.runtime.unregister_ordered_source(source)

    def test_runtime_cancel_is_preserved_and_never_treated_as_arrival(self):
        self.runtime.cancel('player_cancelled')
        result = self.tick()
        self.assertEqual(result.report.status.value, 'cancelled')
        self.assertEqual(self.driver.state, 'cancelled')
        self.assertFalse(self.driver.last_execution_confirmed)
        with self.assertRaises(ContractViolation):
            self.tick()

    def test_uncertain_receipt_seals_runtime_and_never_resends(self):
        self.backend.receipt_changes = {'status': 'pending_confirmation'}
        with self.assertRaises(RuntimeError):
            self.tick()
        self.assertEqual(self.runtime.state.value, 'failed')
        count = len(self.backend.actions)
        with self.assertRaises(ContractViolation):
            self.tick()
        self.assertEqual(len(self.backend.actions), count)

    def test_identical_post_observation_cannot_confirm_execution(self):
        before = self.runtime.observation
        runtime_step = self.runtime.step

        def duplicate_observation(*args, **kwargs):
            result = runtime_step(*args, **kwargs)
            return replace(result, observation=before)

        with patch.object(
            self.runtime, 'step', side_effect=duplicate_observation
        ), self.assertRaises(RuntimeError):
            self.tick()

        self.assertFalse(self.driver.last_execution_confirmed)
        self.assertEqual(self.runtime.state.value, 'failed')

    def test_event_log_failure_seals_before_dispatch(self):
        original = self.trace.write

        def fail(kind, payload):
            if kind == 'playground_task':
                raise OSError('trace fault')
            original(kind, payload)

        self.trace.write = fail
        with self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.runtime.state.value, 'failed')
        self.assertEqual(self.backend.actions, [])

    def test_stop_unregisters_source_but_preserves_online_memory(self):
        self.tick()
        positions_before = tuple(item.block.position for item in self.state.snapshot.terrain)
        source = self.driver.source
        result = self.driver.stop(self.profile, 'player_stop')

        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertEqual(self.driver.state, 'stopped')
        self.assertEqual(self.state.memory_generation, 1)
        self.assertEqual(tuple(item.block.position for item in self.state.snapshot.terrain),
                         positions_before)
        self.assertGreater(len(self.state.belief.beliefs), 0)
        self.assertIn('planning_summary', self.state.policy_cache('C'))
        with self.assertRaises(ContractViolation):
            self.runtime.unregister_ordered_source(source)

    def test_expiry_during_submit_uses_cleanup_without_consuming_sequence(self):
        submit = self.runtime.submit_ordered_intent

        def delayed(envelope):
            self.clock[0] = envelope.intent.expires_at_monotonic_ns
            return submit(envelope)

        with patch.object(self.runtime, 'submit_ordered_intent', side_effect=delayed):
            result = self.tick()
        self.assertEqual(result.decision.action.movement, MovementV1())
        self.assertEqual(self.driver.sequence, 0)
        self.assertEqual(self.driver.reason, 'action_lease_expired_before_dispatch')
        self.assertEqual(self.runtime.state.value, 'ready')


if __name__ == '__main__':
    unittest.main()
