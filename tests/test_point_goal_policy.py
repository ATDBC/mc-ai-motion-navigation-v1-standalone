from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.navigation_state import NavigationState
from mc2p.skills.normal_navigation_guard import check_normal_segment
from mc2p.skills.point_goal import PointGoal
from mc2p.skills.point_goal_policy import PointGoalPolicy
from tests.follow_v3_fixtures import follow_snapshot, observed_block, player_value
from tests.normal_navigation_fixtures import frame, CLIENT_CLOCK_OFFSET_NS


NOW = 3_000_000_000


def floor_patch(y=63, x_range=range(-3, 5), z_range=range(-3, 8)):
    return tuple(observed_block((x, y, z)) for x in x_range for z in z_range)


def context(*, blocks=None, position=(.5, 64, .5), yaw=0., sequence=0,
            observed_at=NOW, decision_at=NOW, velocity=(0., 0., 0.), scope='scope'):
    observation = frame(sequence=sequence, now_ns=observed_at, position=position,
                        yaw=yaw, blocks=floor_patch() if blocks is None else blocks,
                        velocity=velocity)
    state = NavigationState(scope)
    snapshot = state.observe(observation, decision_at)
    view = project_playground_view(observation, decision_at, observation.controller_clock_id)
    return state, snapshot, view


def historical_context(*, age_ns=2_000_000_000, position=(.5, 64, .5),
                       yaw=0., blocks=None, scope='scope'):
    state = NavigationState(scope)
    old = frame(sequence=0, now_ns=NOW-age_ns, position=position, yaw=yaw,
                blocks=floor_patch() if blocks is None else blocks)
    state.observe(old, NOW-age_ns)
    current = frame(sequence=1, now_ns=NOW, position=position, yaw=yaw, blocks=())
    snapshot = state.observe(current, NOW)
    view = project_playground_view(current, NOW, current.controller_clock_id)
    return state, snapshot, view


class NormalSegmentGuardTests(unittest.TestCase):
    def test_guard_uses_original_evidence_time_and_cannot_ignore_contradiction(self):
        state, snapshot, view = historical_context()
        end = Vec3V0(.5, 64, 1.5)

        strict = check_normal_segment(snapshot, view, NOW, end,
                                      historical=False, belief=state.belief)
        admitted = check_normal_segment(snapshot, view, NOW, end,
                                         historical=True, belief=state.belief)
        self.assertEqual(strict.reason, 'uncertain_history')
        self.assertIsNone(admitted.reason)
        self.assertTrue(all(item.history.last_seen.request_start_ns == NOW-2_001_000_000
                            for item in state.belief.beliefs))

        state.belief.contradict(((0, 63, 1),))
        contradicted = check_normal_segment(snapshot, view, NOW, end,
                                             historical=True, belief=state.belief)
        self.assertEqual(contradicted.reason, 'contradiction')

    def test_guard_classifies_all_failures_before_bounding_summary(self):
        blocks = (observed_block((0, 64, 1)),)
        state, snapshot, view = context(blocks=blocks)
        report = check_normal_segment(snapshot, view, NOW, Vec3V0(.5, 64, 2.5),
                                      historical=False, belief=state.belief)
        self.assertEqual(report.reason, 'known_obstacle')
        self.assertLessEqual(len(report.gaps), 64)
        self.assertGreater(report.inspected_steps, 0)

    def test_contradicted_wall_is_not_authorized_from_old_geometry_alone(self):
        blocks = floor_patch() + (observed_block((0, 64, 1)),)
        state, snapshot, view = context(blocks=blocks)
        state.belief.contradict(((0, 64, 1),))

        report = check_normal_segment(snapshot, view, NOW, Vec3V0(.5, 64, 2.5),
                                      historical=True, belief=state.belief)

        self.assertEqual(report.reason, 'contradiction')

    def test_guard_follows_actual_inertial_envelope_not_only_endpoint(self):
        blocks = floor_patch() + (observed_block((0, 64, 1)),)
        state, snapshot, view = context(blocks=blocks, velocity=(0., 0., .2))
        report = check_normal_segment(snapshot, view, NOW, Vec3V0(.5, 64, .6),
                                      historical=False, belief=state.belief)
        self.assertEqual(report.reason, 'known_obstacle')

    def test_guard_checks_continuous_corner_envelope_between_point_samples(self):
        floors = tuple(observed_block((x, 63, z)) for x in range(-2, 5)
                       for z in range(0, 6))
        blocks = floors + (observed_block((1, 64, 1)),)
        state, snapshot, view = context(blocks=blocks,
            position=(.64, 64, 2.32), yaw=-45.)
        delta = .45/math.sqrt(2)

        report = check_normal_segment(
            snapshot, view, NOW, Vec3V0(.64+delta, 64, 2.32-delta),
            historical=False, belief=state.belief,
        )

        self.assertEqual(report.reason, 'known_obstacle')

    def test_adjacent_wall_downgrades_old_support_without_claiming_collision(self):
        floors = tuple(observed_block((x, 63, z)) for x in range(-2, 4)
                       for z in range(-2, 6))
        wall = observed_block((1, 64, 1))
        state = NavigationState('scope')
        old = frame(sequence=0, now_ns=NOW-2_000_000_000,
                    blocks=floors+(wall,))
        state.observe(old, NOW-2_000_000_000)
        current = frame(sequence=1, now_ns=NOW,
                        blocks=(observed_block((0, 63, 0)),))
        snapshot = state.observe(current, NOW)
        view = project_playground_view(current, NOW, current.controller_clock_id)

        report = check_normal_segment(snapshot, view, NOW, Vec3V0(.5, 64, .95),
                                      historical=True, belief=state.belief)

        self.assertEqual(report.reason, 'uncertain_history')

    def test_entity_or_truncation_downgrades_historical_low_consequence_context(self):
        floors = floor_patch()
        for entities, truncated, expected in (
            ([player_value(relative=(1.4, 0, 0))], False, 'uncertain_history'),
            ([], True, 'control_unavailable'),
        ):
            with self.subTest(truncated=truncated):
                state = NavigationState('scope')
                old = frame(sequence=0, now_ns=NOW-2_000_000_000, blocks=floors)
                state.observe(old, NOW-2_000_000_000)
                current = follow_snapshot(sequence=1, received=NOW,
                    position=(.5, 64, .5), entities=entities, blocks=(), truncated=truncated)
                current = replace(current, client_sample=replace(current.client_sample,
                    started_at_monotonic_ns=CLIENT_CLOCK_OFFSET_NS+NOW-1_000_000,
                    completed_at_monotonic_ns=CLIENT_CLOCK_OFFSET_NS+NOW))
                snapshot = state.observe(current, NOW)
                view = project_playground_view(current, NOW, current.controller_clock_id)
                report = check_normal_segment(snapshot, view, NOW, Vec3V0(.5, 64, .95),
                                              historical=True, belief=state.belief)
                self.assertEqual(report.reason, expected)

    def test_truncated_entities_fail_closed_even_with_fresh_strict_support(self):
        observation = follow_snapshot(
            sequence=0, received=NOW, position=(.5, 64, .5), entities=[],
            blocks=floor_patch(), truncated=True,
        )
        observation = replace(observation, client_sample=replace(
            observation.client_sample,
            started_at_monotonic_ns=CLIENT_CLOCK_OFFSET_NS+NOW-1_000_000,
            completed_at_monotonic_ns=CLIENT_CLOCK_OFFSET_NS+NOW,
        ))
        state = NavigationState('scope')
        snapshot = state.observe(observation, NOW)
        view = project_playground_view(
            observation, NOW, observation.controller_clock_id
        )

        report = check_normal_segment(
            snapshot, view, NOW, Vec3V0(.5, 64, .95),
            historical=False, belief=state.belief,
        )

        self.assertEqual(report.reason, 'control_unavailable')

    def test_guard_rejects_unbounded_segment_instead_of_unbounded_work(self):
        state, snapshot, view = context()
        with self.assertRaises(ContractViolation):
            check_normal_segment(snapshot, view, NOW, Vec3V0(.5, 64, 8.5001),
                                 historical=False, belief=state.belief)


class PointGoalPolicyTests(unittest.TestCase):
    def test_cannot_silently_select_a_future_algorithm(self):
        with self.assertRaises(ContractViolation):
            PointGoalPolicy('Z')

    def test_a_is_real_old_controller_with_active_revision_five_and_no_fake_entity(self):
        policy = PointGoalPolicy('A')
        state, snapshot, view = context()
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)

        decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(policy.controller.perception.variant, 'active_perception_v1')
        self.assertEqual(policy.controller.perception.config.revision, 5)
        self.assertEqual(view.base.entities, ())
        self.assertIn(decision.state, {'following', 'searching', 'waiting_observation'})

    def test_open_ground_b_and_c_authorize_only_normal_forward(self):
        for group in ('B', 'C'):
            with self.subTest(group=group):
                policy = PointGoalPolicy(group)
                state, snapshot, view = context()
                goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
                decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)
                self.assertEqual(decision.movement, MovementV1(forward=1))
                self.assertEqual(decision.look, LookV1())
                self.assertEqual(decision.state, 'following')
                summary = policy.planning_summary
                self.assertLessEqual(len(summary['candidates']), 16)
                self.assertLessEqual(summary['expansions'], 256)
                self.assertLess(summary['cells_checked'], len(floor_patch()))
                self.assertEqual(summary['planning_budget_ns'], 10_000_000)

    def test_early_return_exposes_no_current_plan_instead_of_previous_summary(self):
        policy = PointGoalPolicy('C')
        state, snapshot, view = context()
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
        policy.decide(snapshot, view, NOW, goal, belief=state.belief)
        self.assertTrue(policy.planning_diagnostic['planned'])

        decision = policy.decide(snapshot, view, NOW+1, goal, belief=state.belief)

        self.assertEqual(decision.reason, 'awaiting_new_observation')
        self.assertEqual(policy.planning_diagnostic, {
            'planned': False, 'expansions': 0, 'cells_checked': 0,
            'candidates': (), 'cell_rejections': (), 'segment_rejections': (),
            'elapsed_ns': 0, 'planning_budget_ns': 10_000_000,
            'budget_exhausted': False, 'summary_truncated': False,
        })

    def test_planning_diagnostic_marks_every_bounded_projection_cut(self):
        policy = PointGoalPolicy('C')
        base = {
            'expansions': 1, 'cells_checked': 1, 'candidates': (),
            'cell_rejections': (), 'segment_rejections': (),
            'elapsed_ns': 1, 'planning_budget_ns': 10_000_000,
            'budget_exhausted': False, 'summary_truncated': False,
        }
        cases = {
            'candidate_count': {'candidates': tuple(
                ((item, 0), 1.0, 0.0, 0.0) for item in range(17))},
            'cell_count': {'cell_rejections': tuple(
                ((item, 0), 'blocked') for item in range(65))},
            'cell_reason': {'cell_rejections': (((0, 0), 'x' * 81),)},
            'segment_count': {'segment_rejections': tuple(
                ((str(item),), 'blocked') for item in range(65))},
            'segment_key': {'segment_rejections': ((('x' * 161,), 'blocked'),)},
            'segment_reason': {'segment_rejections': ((('key',), 'x' * 81),)},
        }
        for label, replacement in cases.items():
            with self.subTest(label=label):
                policy._planning_summary = {**base, **replacement}
                diagnostic = policy.planning_diagnostic
                self.assertTrue(diagnostic['summary_truncated'])
                self.assertLessEqual(len(diagnostic['candidates']), 16)
                self.assertLessEqual(len(diagnostic['cell_rejections']), 64)
                self.assertLessEqual(len(diagnostic['segment_rejections']), 64)
                self.assertTrue(all(
                    len(row['reason']) <= 80
                    for row in diagnostic['cell_rejections']))
                self.assertTrue(all(
                    len(row['key']) <= 160 and len(row['reason']) <= 80
                    for row in diagnostic['segment_rejections']))

    def test_b_rejects_two_second_floor_while_c_conditionally_uses_it(self):
        for group, expected in (('B', MovementV1()), ('C', MovementV1(forward=1))):
            with self.subTest(group=group):
                policy = PointGoalPolicy(group)
                state, snapshot, view = historical_context()
                goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
                decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)
                self.assertEqual(decision.movement, expected)

    def test_actual_entrance_pose_rejects_clipping_next_center_and_selects_safe_center(self):
        floor = tuple(observed_block((x, -61, z)) for x in range(0, 5)
                      for z in range(6, 14) if (x, z) != (2, 9))
        wall = tuple(observed_block((2, y, 9)) for y in (-60, -59, -58))
        state, snapshot, view = context(blocks=floor+wall,
            position=(1.924, -60, 8.297), yaw=0.)
        goal = PointGoal('goal', 'scope', Vec3V0(1.5, -60, 12.5), NOW+5_000_000_000)
        policy = PointGoalPolicy('B')

        decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(decision.movement, MovementV1())
        self.assertNotEqual(decision.look, LookV1())
        self.assertIn('alternate', decision.reason)
        selected = policy.selected_waypoint
        self.assertIsNotNone(selected)
        self.assertAlmostEqual(selected.x, 1.5)
        self.assertAlmostEqual(selected.z, 8.5)

    def test_failed_best_connection_does_not_block_different_feasible_connection(self):
        blocks = floor_patch(x_range=range(-4, 5), z_range=range(-2, 7))
        blocks += (observed_block((0, 64, 1)),)
        state, snapshot, view = context(blocks=blocks, position=(.5, 64, .5), yaw=0.)
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
        policy = PointGoalPolicy('B')

        decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(decision.movement, MovementV1())
        self.assertNotEqual(decision.state, 'blocked')
        self.assertNotAlmostEqual(policy.selected_waypoint.x, .5)

    def test_missing_frontier_requests_finite_observation_instead_of_permanent_block(self):
        blocks = (observed_block((0, 63, 0)),)
        state, snapshot, view = context(blocks=blocks)
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+20_000_000_000)
        policy = PointGoalPolicy('B')

        decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(decision.movement, MovementV1())
        self.assertIn(decision.state, {'searching', 'waiting_observation'})
        self.assertEqual(decision.reason, 'observe_missing_support')
        self.assertEqual(policy.recovery.recoveries, 1)

    def test_planning_budget_exhaustion_fails_closed_without_partial_candidate(self):
        state, snapshot, view = context()
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
        policy = PointGoalPolicy('B')
        fake = [0]

        def over_budget():
            fake[0] += 11_000_000
            return fake[0]

        with patch('mc2p.skills.point_goal_policy.time.perf_counter_ns',
                   side_effect=over_budget):
            decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.reason, 'planning_budget_exhausted')
        self.assertTrue(policy.planning_summary['budget_exhausted'])

    def test_route_budget_exhaustion_discards_scored_candidates(self):
        state, snapshot, view = context()
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
        policy = PointGoalPolicy('B')
        route = dict(
            expansions=1, cells_checked=1, candidates=(), cell_rejections=(),
            summary_truncated=False, budget_exhausted=True,
        )

        with patch.object(
            policy, '_route',
            return_value=((Vec3V0(.5, 64, 1.5),), route),
        ):
            decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.reason, 'planning_budget_exhausted')
        self.assertTrue(policy.planning_summary['budget_exhausted'])

    def test_guard_that_consumes_budget_cannot_authorize_movement(self):
        state, snapshot, view = context()
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
        policy = PointGoalPolicy('B')
        route = dict(
            expansions=1, cells_checked=1, candidates=(), cell_rejections=(),
            summary_truncated=False, budget_exhausted=False,
        )
        real_guard = check_normal_segment
        guard_returned = [False]

        def guarded(*args, **kwargs):
            report = real_guard(*args, **kwargs)
            guard_returned[0] = True
            return report

        def planning_clock():
            return 11_000_000 if guard_returned[0] else 0

        with patch.object(
            policy, '_route',
            return_value=((Vec3V0(.5, 64, 1.5),), route),
        ), patch(
            'mc2p.skills.point_goal_policy.check_normal_segment',
            side_effect=guarded,
        ), patch(
            'mc2p.skills.point_goal_policy.time.perf_counter_ns',
            side_effect=planning_clock,
        ):
            decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.reason, 'planning_budget_exhausted')
        self.assertTrue(policy.planning_summary['budget_exhausted'])

    def test_all_rejected_candidates_use_final_budget_exhaustion_reason(self):
        blocks = floor_patch() + (observed_block((0, 64, 1)),)
        state, snapshot, view = context(blocks=blocks)
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
        policy = PointGoalPolicy('B')
        route = dict(
            expansions=1, cells_checked=1, candidates=(), cell_rejections=(),
            summary_truncated=False, budget_exhausted=False,
        )
        real_guard = check_normal_segment
        guard_returned = [False]
        post_guard_checks = [0]

        def rejected_guard(*args, **kwargs):
            report = real_guard(*args, **kwargs)
            guard_returned[0] = True
            return report

        def planning_clock():
            if not guard_returned[0]:
                return 0
            post_guard_checks[0] += 1
            return 11_000_000 if post_guard_checks[0] >= 2 else 0

        with patch.object(
            policy, '_route',
            return_value=((Vec3V0(.5, 64, 1.5),), route),
        ), patch(
            'mc2p.skills.point_goal_policy.check_normal_segment',
            side_effect=rejected_guard,
        ), patch(
            'mc2p.skills.point_goal_policy.time.perf_counter_ns',
            side_effect=planning_clock,
        ):
            decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.reason, 'planning_budget_exhausted')
        self.assertTrue(policy.planning_summary['budget_exhausted'])

    def test_discarded_observation_recovery_preserves_ledger_and_pending_look(self):
        state, snapshot, view = context(blocks=(observed_block((0, 63, 0)),))
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
        policy = PointGoalPolicy('B')
        state.bind_policy(policy)
        policy._gate.request(view, -30., 0., NOW)
        pending_before = policy._gate.pending
        recovery_before = policy.recovery
        route = dict(
            expansions=1, cells_checked=1, candidates=(),
            cell_rejections=(((0, 1), 'missing_support'),),
            summary_truncated=False, budget_exhausted=False,
        )
        clock_values = iter((0, 0, 11_000_000, 11_000_000))

        with patch.object(policy, '_route', return_value=((), route)), patch(
            'mc2p.skills.point_goal_policy.time.perf_counter_ns',
            side_effect=lambda: next(clock_values),
        ):
            decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.look, LookV1())
        self.assertEqual(decision.reason, 'planning_budget_exhausted')
        self.assertEqual(policy.recovery, recovery_before)
        self.assertIs(policy._gate.pending, pending_before)

    def test_discarded_heading_turn_preserves_prior_pending_look(self):
        state, snapshot, view = context(yaw=10.)
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
        policy = PointGoalPolicy('B')
        policy._gate.request(view, -30., 0., NOW)
        pending_before = policy._gate.pending
        route = dict(
            expansions=1, cells_checked=1, candidates=(), cell_rejections=(),
            summary_truncated=False, budget_exhausted=False,
        )
        real_guard = check_normal_segment
        guard_returned = [False]
        post_guard_checks = [0]

        def guarded(*args, **kwargs):
            report = real_guard(*args, **kwargs)
            guard_returned[0] = True
            return report

        def planning_clock():
            if not guard_returned[0]:
                return 0
            post_guard_checks[0] += 1
            return 11_000_000 if post_guard_checks[0] >= 2 else 0

        with patch.object(
            policy, '_route',
            return_value=((Vec3V0(.5, 64, 1.5),), route),
        ), patch(
            'mc2p.skills.point_goal_policy.check_normal_segment',
            side_effect=guarded,
        ), patch(
            'mc2p.skills.point_goal_policy.time.perf_counter_ns',
            side_effect=planning_clock,
        ):
            decision = policy.decide(snapshot, view, NOW, goal, belief=state.belief)

        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.look, LookV1())
        self.assertEqual(decision.reason, 'planning_budget_exhausted')
        self.assertIs(policy._gate.pending, pending_before)

    def test_policy_clear_preserves_navigation_state_cache_and_recovery(self):
        state, _, _ = context()
        policy = PointGoalPolicy('B')
        policy.bind_state(state)
        cache = state.policy_cache('B')
        cache['retained_plan'] = ('segment', 1)
        ledger = state.recovery_ledger('B')
        self.assertTrue(ledger.allow('segment', 'revision', NOW))
        ledger.fail('segment', 'revision', NOW)

        policy.clear()

        self.assertEqual(cache['retained_plan'], ('segment', 1))
        self.assertEqual(ledger.snapshot().recoveries, 1)

    def test_equivalent_timestamp_churn_does_not_create_new_recovery_identity(self):
        state = NavigationState('scope')
        policy = PointGoalPolicy('B')
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+20_000_000_000)
        for index in range(2):
            now = NOW+index*250_000_000
            obs = frame(sequence=index, now_ns=now, blocks=(observed_block((0, 63, 0)),))
            snapshot = state.observe(obs, now)
            view = project_playground_view(obs, now, obs.controller_clock_id)
            policy.decide(snapshot, view, now, goal, belief=state.belief)

        recovery = policy.recovery
        self.assertEqual(len(recovery.attempts), 1)
        self.assertEqual(recovery.recoveries, 2)

    def test_nonzero_heading_error_stays_neutral_until_new_observation(self):
        policy = PointGoalPolicy('B')
        state, snapshot, view = context(yaw=10.)
        goal = PointGoal('goal', 'scope', Vec3V0(.5, 64, 4.5), NOW+5_000_000_000)
        first = policy.decide(snapshot, view, NOW, goal, belief=state.belief)
        self.assertEqual(first.movement, MovementV1())
        self.assertNotEqual(first.look, LookV1())

        repeated = policy.decide(snapshot, view, NOW+1, goal, belief=state.belief)
        self.assertEqual(repeated.movement, MovementV1())
        self.assertEqual(repeated.reason, 'awaiting_new_observation')

        turned_obs = frame(sequence=1, now_ns=NOW+50_000_000, yaw=0.,
                           blocks=floor_patch())
        turned_snapshot = state.observe(turned_obs, NOW+50_000_000)
        turned_view = project_playground_view(
            turned_obs, NOW+50_000_000, turned_obs.controller_clock_id
        )
        policy.feedback(True, turned_view, NOW+50_000_000)
        moved = policy.decide(turned_snapshot, turned_view, NOW+50_000_000,
                              goal, belief=state.belief)
        self.assertEqual(moved.movement, MovementV1(forward=1))

    def test_forward_authorization_guards_actual_yaw_not_desired_waypoint_angle(self):
        # Desired connector is 7 degrees left and clear, but actual yaw's inertia
        # reaches the obstacle.  The tolerance must not substitute desired yaw.
        blocks = floor_patch() + (observed_block((0, 64, 1), kind='boxes',
            boxes=((.72, 0, 0, .9, 1.8, 1),)),)
        state, snapshot, view = context(blocks=blocks, yaw=0.)
        radians = math.radians(-7)
        goal = PointGoal('goal', 'scope', Vec3V0(.5-math.sin(radians)*4, 64,
                                                 .5+math.cos(radians)*4),
                         NOW+5_000_000_000)
        decision = PointGoalPolicy('B').decide(snapshot, view, NOW, goal,
                                                belief=state.belief)
        self.assertEqual(decision.movement, MovementV1())


if __name__ == '__main__':
    unittest.main()
