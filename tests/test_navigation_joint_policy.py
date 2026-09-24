from dataclasses import replace
from pathlib import Path
import math
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.point_goal import PointGoal
from mc2p.skills.point_goal_policy import PointGoalPolicy
from mc2p.skills.navigation_joint_policy import JointPlanner, _route_id
from mc2p.skills.normal_control_capabilities import ControlCapabilities
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.runtime.trace import trace_projection
from mc2p.skills.normal_navigation_guard import check_control_proposal
from scripts.joint_planning_evidence import validate_planning_diagnostic
from mc2p.contracts.common import require_identifier
from tests.test_point_goal_policy import context, floor_patch, NOW
from tests.normal_navigation_fixtures import frame
from tests.follow_v3_fixtures import observed_block


def TestCapabilities(denied=()):
    return ControlCapabilities(tuple((f,s,a) for f in (-1,0,1) for s in (-1,0,1)
        for a in ('fixed','yaw','pitch','yaw_pitch') if (f,s,a) not in denied),(),Path('.'))


class JointPolicyTests(unittest.TestCase):
    def setup_policy(self, group='E', **kwargs):
        state, snapshot, view = context(**kwargs)
        policy = PointGoalPolicy(group, control_capabilities=TestCapabilities())
        state.bind_policy(policy)
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+30_000_000_000)
        return policy, state, snapshot, view, goal

    def test_de_explicitly_require_capabilities(self):
        for group in ('D', 'E'):
            with self.subTest(group=group), self.assertRaises(ContractViolation):
                PointGoalPolicy(group)

    def test_retained_noncenter_route_and_current_cell_center_have_distinct_ids(self):
        for group in ('D', 'E'):
            policy, state, snapshot, view, goal = self.setup_policy(group)
            planner = policy.joint_planner
            with patch('time.perf_counter_ns', return_value=0):
                policy.decide(snapshot, view, NOW, goal, belief=state.belief)
                base = next(c for c in planner.candidates if c.route_id == 'cell/0/1')
                point = Vec3V0(.499903214988853, 64, 1.4860873922302344)
                proposal = planner._proposal(snapshot, view, point, 0., 0., state.memory_generation)
                self.assertIsNone(check_control_proposal(snapshot, view, NOW, proposal,
                    historical=True, belief=state.belief, memory_generation=state.memory_generation).reason)
                planner.selected = replace(base, endpoint=point, proposal=proposal)
                planner.decide(snapshot, view, goal, NOW, belief=state.belief,
                               memory_generation=state.memory_generation)
            with self.subTest(group=group):
                candidates = planner.candidates
                self.assertTrue(any(c.endpoint == point for c in candidates))
                self.assertTrue(any(c.endpoint == Vec3V0(.5, 64, 1.5) and c.route_id == 'cell/0/1'
                                    for c in candidates))
                ids = [c.candidate_id for c in candidates]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertLessEqual(len({c.route_id for c in candidates}), 4)
                validate_planning_diagnostic(trace_projection(planner.diagnostic), dict(group=group,
                    joint_configuration=dict(prelook_enabled=True, force_route_id=None)))

    def test_identical_route_endpoints_expand_gazes_once(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        planner = policy.joint_planner
        with patch('time.perf_counter_ns', return_value=0):
            routes, summary = planner._routes._route(snapshot, view, NOW, goal, state.belief, 0)
            # Both entries are an actually generated, guard-checked connector.
            with patch.object(planner._routes, '_route', return_value=((routes[0], routes[0]), summary)):
                planner.decide(snapshot, view, goal, NOW, belief=state.belief,
                               memory_generation=state.memory_generation)
        ids = [c.candidate_id for c in planner.candidates]
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(planner.diagnostic['route_count'], 1)

    def test_route_ids_preserve_centers_and_exact_stable_noncenter_coordinates(self):
        self.assertEqual(_route_id(Vec3V0(1.5, -60, 2.5)), 'cell/1/2')
        self.assertEqual(_route_id(Vec3V0(.5, -60, 1.5)), 'cell/0/1')
        points = (Vec3V0(11.499903214988853, -60, 7.4860873922302344),
                  Vec3V0(math.nextafter(11.5, 0), -60, 7.5),
                  Vec3V0(11.5, -60, math.nextafter(7.5, 0)),
                  Vec3V0(11.5, math.nextafter(-60., 0), 7.5),
                  Vec3V0(float.fromhex('0x1.fffffffffffffp1023'), 0, -5e-324))
        ids = []
        for point in points:
            identity = _route_id(point)
            self.assertEqual(identity, _route_id(replace(point)))
            self.assertTrue(identity.startswith('point/'))
            require_identifier(identity+'/reposition', 'candidate')
            self.assertLessEqual(len(identity), 128)
            self.assertLessEqual(len(identity+'/reposition'), 160)
            self.assertEqual(tuple(map(float.fromhex, identity.split('/')[1:])), (point.x, point.y, point.z))
            ids.append(identity)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(_route_id(Vec3V0(0., 64, 0.)), _route_id(Vec3V0(-0., 64, -0.)))

    def test_cost_cell_prefix_ids_still_select_the_generated_centers(self):
        for identity, position, endpoint in (('cell/0/1', (.5,64,.5), Vec3V0(.5,64,1.5)),
                                              ('cell/1/2', (1.5,64,1.5), Vec3V0(1.5,64,2.5))):
            state, snapshot, view = context(position=position)
            policy = PointGoalPolicy('E', control_capabilities=TestCapabilities(), force_route_id=identity)
            state.bind_policy(policy)
            goal = PointGoal('goal', 'scope', Vec3V0(position[0],64,4.5), NOW+30_000_000_000)
            with self.subTest(identity=identity), patch('time.perf_counter_ns', return_value=0):
                policy.decide(snapshot,view,NOW,goal,belief=state.belief)
                self.assertEqual(policy.joint_planner.selected.route_id,identity)
                self.assertEqual(policy.joint_planner.selected.endpoint,endpoint)

    def test_same_legal_candidates_and_no_irrelevant_side_gaze(self):
        rows = []
        for group in ('D', 'E'):
            policy, state, snapshot, view, goal = self.setup_policy(group)
            decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)
            self.assertNotEqual(decision.movement, MovementV1())
            diagnostic = policy.planning_diagnostic
            self.assertLessEqual(len(diagnostic['joint_candidates']), 16)
            self.assertLessEqual(diagnostic['route_count'], 4)
            self.assertTrue(all(c['gaze_kind'] != 'irrelevant_side' for c in diagnostic['joint_candidates']))
            rows.append(tuple(c['candidate_id'] for c in diagnostic['joint_candidates']))
            self.assertEqual(diagnostic['proposal'].movement, decision.movement)
        self.assertEqual(rows[0], rows[1])

    def test_published_diagnostic_is_detached_from_planner_state(self):
        policy, state, snapshot, view, goal = self.setup_policy('D')
        policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        published = policy.planning_diagnostic
        self.assertIs(state.policy_cache('D')['planning_summary'], published)
        published['joint_candidates'][0]['candidate_id'] = 'mutated-by-consumer'

        fresh = policy.planning_diagnostic
        self.assertNotEqual(fresh['joint_candidates'][0]['candidate_id'],
                            'mutated-by-consumer')

    def test_unfinished_route_is_reused_but_rechecked_on_new_observation(self):
        policy, state, snapshot, view, goal = self.setup_policy('D')
        first = policy.decide(snapshot, view, NOW, goal, belief=state.belief)
        self.assertNotEqual(first.movement, MovementV1())
        planner = policy.joint_planner
        retained = planner.selected.endpoint
        later = frame(sequence=1, now_ns=NOW+50_000_000,
                      position=(.5, 64, .7), blocks=floor_patch())
        later_snapshot = state.observe(later, NOW+50_000_000)
        later_view = project_playground_view(
            later, NOW+50_000_000, later.controller_clock_id,
        )

        with patch.object(planner._routes, '_route', wraps=planner._routes._route) as search:
            second = policy.decide(
                later_snapshot, later_view, NOW+50_000_000, goal,
                belief=state.belief,
            )

        search.assert_not_called()
        self.assertNotEqual(second.movement, MovementV1())
        self.assertEqual(planner.selected.endpoint, retained)
        self.assertEqual(policy.planning_diagnostic['expansions'], 0)

    def test_reused_route_does_not_bypass_new_obstacle_guard(self):
        policy, state, snapshot, view, goal = self.setup_policy('D')
        policy.decide(snapshot, view, NOW, goal, belief=state.belief)
        planner = policy.joint_planner
        wall = observed_block((0, 64, 1))
        later = frame(sequence=1, now_ns=NOW+50_000_000,
                      position=(.5, 64, .7), blocks=floor_patch() + (wall,))
        later_snapshot = state.observe(later, NOW+50_000_000)
        later_view = project_playground_view(
            later, NOW+50_000_000, later.controller_clock_id,
        )

        with patch.object(planner._routes, '_route', wraps=planner._routes._route) as search:
            decision = policy.decide(
                later_snapshot, later_view, NOW+50_000_000, goal,
                belief=state.belief,
            )

        search.assert_not_called()
        self.assertEqual(decision.movement, MovementV1())
        self.assertIsNone(planner.selected)

    def test_unavailable_controls_are_not_ranked_as_allowed(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        policy = PointGoalPolicy('E', control_capabilities=TestCapabilities(
            tuple((f, s, a) for f in (-1, 0, 1) for s in (-1, 0, 1)
                  if f or s for a in ('fixed', 'yaw', 'pitch', 'yaw_pitch'))))
        state.bind_policy(policy)
        decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)
        self.assertEqual(decision.movement, MovementV1())
        self.assertTrue(all(c['proposal'].movement == MovementV1()
                            for c in policy.planning_diagnostic['joint_candidates']))

    def test_pre_submit_recheck_rejects_new_contradiction_and_expired_stamp(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)
        state.belief.contradict(((0, 63, 0),))
        checked = policy.revalidate(decision, snapshot, view, NOW+1, belief=state.belief)
        self.assertEqual(checked.movement, MovementV1())
        self.assertIn('recheck', checked.reason)

    def test_predictions_do_not_fill_unknown_floor_or_fake_acquisition(self):
        blocks = (observed_block((0, 63, 0)),)
        policy, state, snapshot, view, goal = self.setup_policy(blocks=blocks)
        original = tuple(snapshot.terrain)
        decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)
        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(tuple(state.snapshot.terrain), original)
        self.assertIsNone(policy.planning_diagnostic['acquired_ns'])
        self.assertTrue(policy.planning_diagnostic['needs'])

    def test_requested_route_must_be_a_generated_legal_route(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        policy = PointGoalPolicy('E', control_capabilities=TestCapabilities(),
                                 force_route_id='not-a-generated-route')
        state.bind_policy(policy)
        decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)
        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.reason, 'requested_route_unavailable')

    def test_joint_selection_retains_valid_route_within_hysteresis(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        policy.decide(snapshot, view, NOW, goal, belief=state.belief)
        planner = policy.joint_planner
        candidates = planner.candidates
        old = planner.selected
        alternative = next(c for c in candidates if c.candidate_id != old.candidate_id)
        challenger = replace(alternative, intervals=old.intervals,
            progress_debt_seconds=max(0, old.progress_debt_seconds-.01),
            gaze_debt=old.gaze_debt, uncertainty_penalty=old.uncertainty_penalty)
        self.assertEqual(planner.select((old, challenger), NOW).candidate_id, old.candidate_id)
        self.assertEqual(planner.select((challenger,), NOW).candidate_id, challenger.candidate_id)

    def test_freshness_expiry_releases_even_stationary_look(self):
        policy, state, snapshot, view, goal = self.setup_policy(blocks=(observed_block((0,63,0)),))
        decision = policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        checked = policy.revalidate(decision,snapshot,view,NOW+600_000_000,belief=state.belief)
        self.assertEqual(checked.look.yaw_delta_degrees, 0.)
        self.assertEqual(checked.look.pitch_delta_degrees, 0.)
        self.assertIn('recheck',checked.reason)

    def test_feedback_acquisition_requires_actual_new_useful_block(self):
        policy, state, snapshot, view, goal = self.setup_policy(blocks=(observed_block((0,63,0)),))
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        planner = policy.joint_planner
        sensed = next(c for c in planner.candidates if c.need_id is not None)
        planner.selected = sensed
        planner._pending_need = next(n for n in planner.needs if n.need_id == sensed.need_id)
        before = planner.diagnostic
        no_new = frame(sequence=1,now_ns=NOW+50_000_000,blocks=())
        no_new_snapshot = state.observe(no_new,NOW+50_000_000)
        no_new_view = project_playground_view(no_new,NOW+50_000_000,no_new.controller_clock_id)
        feedback = planner.feedback(True,no_new_snapshot,no_new_view,NOW+50_000_000)
        self.assertIsNone(feedback['acquired_ns'])
        need = planner._pending_need
        acquired = frame(sequence=2,now_ns=NOW+100_000_000,blocks=(observed_block(need.block),))
        acquired_snapshot = state.observe(acquired,NOW+100_000_000)
        acquired_view = project_playground_view(acquired,NOW+100_000_000,acquired.controller_clock_id)
        feedback = planner.feedback(True,acquired_snapshot,acquired_view,NOW+100_000_000)
        self.assertEqual(feedback['acquired_ns'], acquired.received_at_monotonic_ns)
        self.assertEqual(feedback['need_id'],need.need_id)
        self.assertEqual(planner.diagnostic,before)

    def test_known_wall_does_not_repeat_occluded_gaze_and_repositions_legally(self):
        blocks = floor_patch()+tuple(observed_block((0,y,1)) for y in (64,65,66))
        policy, state, snapshot, view, goal = self.setup_policy(blocks=blocks)
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        planner = policy.joint_planner
        hidden = (0,63,2)
        self.assertTrue(planner._occluded(snapshot,view.base.own.position,hidden))
        self.assertFalse(planner._occluded(snapshot,Vec3V0(3.5,64,.5),hidden))
        self.assertTrue(planner.candidates)
        self.assertTrue(all(c.endpoint != Vec3V0(.5,64,1.5) for c in planner.candidates))

    def test_budget_discards_partially_built_candidates(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        ticks = [0]
        def timer():
            ticks[0] += 11_000_000
            return ticks[0]
        with patch('mc2p.skills.navigation_joint_policy.time.perf_counter_ns', side_effect=timer):
            decision = policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        self.assertEqual(decision.movement,MovementV1())
        self.assertEqual(decision.reason,'planning_budget_exhausted')

    def test_failed_combination_is_not_immediately_reissued(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        failed_route = policy.joint_planner.selected.route_id
        observation = frame(sequence=1,now_ns=NOW+50_000_000,blocks=floor_patch())
        current = state.observe(observation,NOW+50_000_000)
        current_view = project_playground_view(observation,NOW+50_000_000,observation.controller_clock_id)
        policy.bind_feedback_snapshot(current)
        policy.feedback(False,current_view,NOW+50_000_000)
        policy.decide(current,current_view,NOW+50_000_000,goal,belief=state.belief)
        self.assertTrue(all(c.route_id != failed_route for c in policy.joint_planner.candidates))

    def test_unverified_dual_axis_is_split_into_verified_single_axis(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        planner = JointPlanner('E',control_capabilities=TestCapabilities(tuple(
            (f,s,'yaw_pitch') for f in (-1,0,1) for s in (-1,0,1))))
        proposal = planner._proposal(snapshot,view,Vec3V0(.5,64,1.5),30.,45.,1)
        self.assertIsNotNone(proposal)
        self.assertNotEqual(proposal.look.yaw_delta_degrees or proposal.look.pitch_delta_degrees,0.)
        self.assertEqual(proposal.look.yaw_delta_degrees*proposal.look.pitch_delta_degrees,0.)

    def test_forced_initial_prefix_releases_only_after_actual_endpoint_arrival(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        policy = PointGoalPolicy('E',control_capabilities=TestCapabilities(),force_route_id='cell/0/1')
        state.bind_policy(policy)
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        planner = policy.joint_planner
        self.assertEqual(planner.selected.route_id,'cell/0/1')
        arrival = frame(sequence=1,now_ns=NOW+50_000_000,position=(.5,64,1.5),blocks=floor_patch())
        current = state.observe(arrival,NOW+50_000_000)
        current_view = project_playground_view(arrival,NOW+50_000_000,arrival.controller_clock_id)
        planner.feedback(True,current,current_view,NOW+50_000_000)
        policy.decide(current,current_view,NOW+50_000_000,goal,belief=state.belief)
        self.assertTrue(policy.planning_diagnostic['force_route_completed'])
        self.assertNotEqual(planner.selected.route_id,'cell/0/1')

    def test_direct_planner_rejects_stale_self_state(self):
        policy, state, snapshot, view, goal = self.setup_policy()
        decision = policy.joint_planner.decide(snapshot,view,goal,NOW+600_000_000,
            belief=state.belief,memory_generation=state.memory_generation)
        self.assertEqual(decision.movement,MovementV1())
        self.assertEqual(decision.look.yaw_delta_degrees or decision.look.pitch_delta_degrees,0.)

    def test_independent_and_joint_selection_use_same_candidates_but_different_costs(self):
        policy,state,snapshot,view,goal = self.setup_policy()
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        base = policy.joint_planner.candidates[0]
        direct = replace(base,candidate_id='direct',route_id='direct',route_score=1.,
                         intervals=((0,5_000_000_000),),progress_debt_seconds=1.)
        detour = replace(base,candidate_id='detour',route_id='detour',route_score=1.1,
                         intervals=((0,1_000_000_000),),progress_debt_seconds=1.1)
        independent = JointPlanner('D',control_capabilities=TestCapabilities())
        joint = JointPlanner('E',control_capabilities=TestCapabilities())
        self.assertEqual(independent.select((direct,detour),NOW).candidate_id,'direct')
        self.assertEqual(joint.select((direct,detour),NOW).candidate_id,'detour')

    def test_repeated_frame_does_not_publish_or_reauthorize_prior_plan(self):
        policy,state,snapshot,view,goal = self.setup_policy()
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        decision = policy.decide(snapshot,view,NOW+1,goal,belief=state.belief)
        self.assertFalse(policy.planning_diagnostic['planned'])
        checked = policy.revalidate(decision,snapshot,view,NOW+1,belief=state.belief)
        self.assertEqual(checked.reason,'awaiting_new_observation')

    def test_occluded_need_times_sampling_after_reaching_known_observation_position(self):
        blocks = tuple(b for b in floor_patch() if b.position != (0,63,2))
        blocks += tuple(observed_block((0,y,1)) for y in (64,65,66))
        policy,state,snapshot,view,goal = self.setup_policy(blocks=blocks,position=(1.5,64,.5))
        goal = replace(goal,position=Vec3V0(-1.5,64,4.5))
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        moved_observation = [c for c in policy.planning_diagnostic['joint_candidates']
                             if c['gaze_kind'] == 'reposition']
        self.assertTrue(moved_observation)
        for c in moved_observation:
            self.assertGreaterEqual(c['intervals'][-1][0],c['intervals'][0][1])
        self.assertFalse(any(c['gaze_kind']=='task' and c['need_id'] is not None
                             for c in policy.planning_diagnostic['joint_candidates']))

    def test_d_hysteresis_uses_route_score_not_joint_time_score(self):
        policy,state,snapshot,view,goal = self.setup_policy()
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        base = policy.joint_planner.candidates[0]
        old = replace(base,candidate_id='old',route_id='old',route_score=2.,intervals=((0,1),))
        better = replace(base,candidate_id='better',route_id='better',route_score=1.,
                         intervals=((0,9_000_000_000),))
        planner = JointPlanner('D',control_capabilities=TestCapabilities())
        planner.selected = old
        self.assertEqual(planner.select((old,better),NOW).candidate_id,'better')

    def test_earlier_same_priority_need_overrides_gaze_hysteresis(self):
        policy,state,snapshot,view,goal = self.setup_policy()
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        base = policy.joint_planner.candidates[0]
        old = replace(base,candidate_id='old',need_id='later',need_priority=0,
                      required_by_ns=NOW+200)
        urgent = replace(base,candidate_id='urgent',need_id='sooner',need_priority=0,
                         required_by_ns=NOW+100,progress_debt_seconds=base.progress_debt_seconds-.001)
        for group in ('D','E'):
            planner = JointPlanner(group,control_capabilities=TestCapabilities())
            planner.selected = old
            self.assertEqual(planner.select((old,urgent),NOW).candidate_id,'urgent')

    def test_disabled_prelook_does_not_gain_task_sensing_reward_while_moving(self):
        blocks = tuple(b for b in floor_patch() if b.position != (0,63,2))
        policy,state,snapshot,view,goal = self.setup_policy(blocks=blocks)
        policy = PointGoalPolicy('E',control_capabilities=TestCapabilities(),prelook_enabled=False)
        state.bind_policy(policy)
        policy.decide(snapshot,view,NOW,goal,belief=state.belief)
        self.assertTrue(policy.joint_planner.candidates)
        self.assertTrue(all(c.need_id is None for c in policy.joint_planner.candidates))


class JointDriverTests(unittest.TestCase):
    def test_wire_expiry_never_outlives_candidate_evidence_expiry(self):
        from tests.test_point_goal_driver import PointGoalDriverTests
        from mc2p.skills.point_goal_driver import PointGoalDriver
        case = PointGoalDriverTests()
        case.setUp()
        try:
            case.driver.stop(case.profile,'switch_test_consumer')
            policy = PointGoalPolicy('E',control_capabilities=TestCapabilities())
            driver = PointGoalDriver(case.runtime,case.state,policy,lambda:case.clock[0])
            driver.start(case.goal,case.clock[0])
            case.clock[0] += 400_000_000
            driver.tick(case.profile,case.clock[0]+3_000_000_000)
            envelope = [p['envelope'] for k,p in case.trace.records if k=='ordered_intent'][-1]
            diagnostic = [p['planning_diagnostic'] for k,p in case.trace.records if k=='playground_task'][-1]
            selected = next(c for c in diagnostic['joint_candidates']
                            if c['candidate_id']==diagnostic['selected_candidate_id'])
            self.assertLessEqual(envelope.intent.expires_at_monotonic_ns,selected['valid_until_ns'])
        finally:
            case.tearDown()

    def test_real_driver_rechecks_after_source_cancel_and_records_matching_proposal(self):
        from tests.test_point_goal_driver import PointGoalDriverTests
        from mc2p.skills.point_goal_driver import PointGoalDriver
        for group in ('D','E'):
            case = PointGoalDriverTests()
            case.setUp()
            try:
                case.driver.stop(case.profile,'switch_test_consumer')
                policy = PointGoalPolicy(group,control_capabilities=TestCapabilities())
                driver = PointGoalDriver(case.runtime,case.state,policy,lambda:case.clock[0])
                driver.start(case.goal,case.clock[0])
                cancel = case.runtime.cancel_source
                def contradict_after_cancel(source):
                    cancel(source)
                    case.state.belief.contradict(((0,63,0),))
                with patch.object(case.runtime,'cancel_source',side_effect=contradict_after_cancel):
                    result = driver.tick(case.profile,case.clock[0]+3_000_000_000)
                self.assertEqual(result.decision.action.movement,MovementV1())
                event = [payload for kind,payload in case.trace.records if kind=='playground_task'][-1]
                self.assertIn('joint_recheck',event['reason'])
                self.assertEqual(event['planning_diagnostic']['submit_rejection'],'contradiction')
                self.assertEqual(event['planning_diagnostic']['proposal'].scope_id,'scope')
            finally:
                case.tearDown()


if __name__ == '__main__':
    unittest.main()
