"""R28-3 generation evidence through the Runtime navigation driver."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.motion_nav.async_work import ComputationInvalidationCause
from mc2p.motion_nav.known_map_planner import SurfacePlanningStatus
from tests.sim.runner import Event, InlinePlannerWorker, _goal, run
from tests.sim.scenarios import SCENARIOS, airborne_in_drop


def flat():
    return next(s for s in SCENARIOS if s.name == 'flat_walk')


class HeldPlanner(InlinePlannerWorker):
    """Hold actual worker computation without changing its result semantics."""
    def __init__(self):
        super().__init__()
        self.hold = True
        self.deliveries = []
        self.returned = []

    def poll_latest(self):
        if self.deliveries:
            return self.deliveries.pop(0)
        if self.hold:
            return None
        result = super().poll_latest()
        if result is not None:
            self.returned.append(result)
        return result


class GenerationFormalChainTests(unittest.TestCase):
    def check_safe(self, result, expected='success'):
        self.assertEqual(result.outcome, expected, result.reason)
        self.assertFalse(result.violations)
        self.assertTrue(result.verification_complete, result.coverage_gaps)
        self.assertFalse(result.trace[-1]['source_bound'])

    def cadence(self, cadence):
        worker, original, observations, owners = HeldPlanner(), [], [], []
        def revise(context, revision):
            owner = context.session._planning_coordinator
            before = owner.work_identity, owner.work_window
            if not original:
                original.append(before)
                owners.append(owner)
            context.goal_position = (.5, 64., 8.5 + .03*(revision-1))
            context.goal_state = _goal(context.goal_position)
            self.assertTrue(context.driver.replace_goal('goal', revision,
                context.goal_state, context.clock[0]))
            self.assertEqual((owner.work_identity, owner.work_window), before)
            self.assertEqual(context.session.current_computation_scope, original[0][0].scope)
            observations.append(context.tick)
        def release(context):
            worker.hold = False
        events = [Event(f'revision-{revision}', lambda c, at=cadence*(revision-1): c.tick >= at,
            lambda c, revision=revision: revise(c, revision), goal_revision=revision)
            for revision in range(2, 5)]
        events.append(Event('deliver-original', lambda c: c.tick >= cadence*3+1, release))
        result = run(replace(flat(), events=events), planner_factory=lambda: worker)
        self.check_safe(result)
        self.assertEqual(observations, [cadence, cadence*2, cadence*3])
        self.assertTrue(worker.returned)
        self.assertEqual(worker.returned[0].work_identity.scope, original[0][0].scope)
        first_retirement = [e for e in owners[0].async_diagnostics.events
                            if e.identity == original[0][0] and e.operation == 'finish']
        self.assertEqual(len(first_retirement), 1)
        self.assertLessEqual(first_retirement[0].monotonic_ns,
                            original[0][1].deadline_monotonic_ns + 50_000_000)
        submits = [a for a in worker.activity if a.operation == 'submit']
        self.assertEqual(sum(a.identity == original[0][0] for a in submits), 1)
        self.assertEqual(result.trace[-1]['goal_revision'], 4)

    def test_revision_every_three_ticks_while_worker_in_flight(self):
        self.cadence(3)

    def test_revision_every_five_ticks_while_worker_in_flight(self):
        self.cadence(5)

    def test_revision_every_eight_ticks_while_worker_in_flight(self):
        self.cadence(8)

    def test_old_negative_results_cannot_fail_revised_runtime_goal(self):
        for status in (SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE,
                       SurfacePlanningStatus.NO_KNOWN_ROUTE, SurfacePlanningStatus.TIMEOUT):
            with self.subTest(status=status):
                worker, checked = HeldPlanner(), []
                def deliver(context):
                    worker.hold = False
                    candidate = worker.poll_latest()
                    self.assertIsNotNone(candidate)
                    old = candidate.work_identity
                    candidate = replace(candidate, status=status, path=(), segments=(),
                        ground_traversal_plans=(), planner_states=(), total_cost_seconds=None,
                        total_cost_ticks=None, final_resources=None)
                    self.assertTrue(context.driver.replace_goal('goal', 2, context.goal_state, context.clock[0]))
                    worker.deliveries.append(candidate)
                    checked.append((context.session, old))
                result = run(replace(flat(), events=[Event('old-negative',
                    lambda c: worker._job is not None, deliver)]), planner_factory=lambda: worker)
                self.check_safe(result)
                self.assertEqual(len(checked), 1)
                self.assertEqual(result.trace[-1]['goal_revision'], 2)
                self.assertFalse(any(e.identity == checked[0][1] and e.operation == 'apply'
                    for e in checked[0][0]._planning_coordinator.async_diagnostics.events))

    def test_same_generation_other_work_and_duplicate_do_not_authorize_route(self):
        worker, checked = HeldPlanner(), []
        def deliver(context):
            worker.hold = False
            candidate = worker.poll_latest()
            old = candidate.work_identity
            other = replace(candidate, work_identity=replace(old, subject_id='other-work', revision=old.revision+1))
            worker.deliveries.extend((other, candidate, candidate))
            checked.append((context.session, old))
        result = run(replace(flat(), events=[Event('unordered-delivery',
            lambda c: worker._job is not None, deliver)]), planner_factory=lambda: worker)
        self.check_safe(result)
        self.assertEqual(len(checked), 1)
        events = checked[0][0]._planning_coordinator.async_diagnostics.events
        self.assertEqual(sum(e.identity == checked[0][1] and e.operation == 'apply' for e in events), 1)

    def test_cancellation_retires_in_flight_work_and_preserves_scope_boundary(self):
        worker, checked = HeldPlanner(), []
        def cancel(context):
            old = context.session.current_computation_scope
            owner = context.session._planning_coordinator
            context.driver.release('r28-generation-cancel')
            self.assertGreater(context.session.current_computation_scope.generation, old.generation)
            self.assertIsNone(owner.async_diagnostics.active_identity)
            checked.append(old)
        result = run(replace(flat(), expect='cancelled', events=[Event('cancel-in-flight',
            lambda c: worker._job is not None, cancel)]), planner_factory=lambda: worker)
        self.check_safe(result, 'cancelled')
        self.assertEqual(len(checked), 1)
        self.assertFalse(worker.returned)

    def test_generation_change_in_air_keeps_original_body_owner_until_landing(self):
        from tests.motion_nav.test_navigation_supervised_interruptions import scenario
        from tests.sim.backend import Perturbations
        checked = []
        def invalidate(context):
            old = context.session.current_computation_scope
            self.assertTrue(context.session.has_owned_body_control)
            context.session._goal_requests.invalidate_computation(ComputationInvalidationCause.NEW_STATE_ANCHOR)
            context.driver.release('r28-air-generation-cancel')
            self.assertGreater(context.session.current_computation_scope.generation, old.generation)
            self.assertTrue(context.session.has_owned_body_control)
            self.assertIsNotNone(context.driver.source)
            checked.append(context.tick)
        result = run(replace(scenario('direct_drop_5_budget_2'),
            events=[Event('scope-change-in-air', airborne_in_drop, invalidate)],
            expect='cancelled', perturbations=Perturbations(), max_ticks=180))
        self.check_safe(result, 'cancelled')
        self.assertEqual(len(checked), 1)
        self.assertTrue(result.trace[-1]['on_ground'])

    def test_receding_goal_stays_bounded_without_refreshing_first_work(self):
        worker, checked, revisions = HeldPlanner(), [], []
        def revise(context, revision):
            owner = context.session._planning_coordinator
            before = owner.work_identity, owner.work_window
            if not checked:
                checked.append(before)
            target = (.5, 64., 7. + revision*.1)
            context.goal_position, context.goal_state = target, _goal(target)
            self.assertTrue(context.driver.replace_goal('goal', revision, context.goal_state, context.clock[0]))
            self.assertEqual((owner.work_identity, owner.work_window), before)
            revisions.append(revision)
        events = [Event(f'receding-{r}', lambda c, at=3*(r-1): c.tick >= at,
            lambda c, revision=r: revise(c, revision), goal_revision=r) for r in range(2, 10)]
        events.append(Event('stop-unreachable-follow-up', lambda c: c.tick >= 25,
            lambda c: c.driver.release('bounded-probe-stop')))
        result = run(replace(flat(), events=events, expect='cancelled', max_ticks=80),
                     planner_factory=lambda: worker)
        self.check_safe(result, 'cancelled')
        submits = [a for a in worker.activity if a.operation == 'submit']
        self.assertGreaterEqual(len(submits), 2, 'the unchanged original deadline must retire held work')
        self.assertEqual(sum(a.identity == checked[0][0] for a in submits), 1)
        self.assertEqual(revisions, list(range(2, 10)))

    def test_world_switch_rejects_old_work_on_formal_session_observation(self):
        from mc2p.motion_nav.navigation_session import NavigationSession
        from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
        from tests.motion_nav import test_navigation_session as fixture
        from tests.motion_nav.test_b07_step_route import frame
        from tests.motion_nav.test_planning_coordinator import _world, _request
        from tests.observation_v3_fixtures import valid_snapshot_v3
        world, worker = _world(), fixture._InlinePlanner(hold_first=True)
        session = NavigationSession('r28-world-change', fixture.NavigationSessionTests().profiles(),
            planner_worker=worker, motion_worker=fixture._InlineMotionWorker(), clock_ns=lambda: 1_000_000_000)
        try:
            current = frame(world, 0, (-.5, 1., .5))
            session.start(_request(world), current)
            session.propose(current, None, 2_000_000_000)
            old = session.current_computation_scope
            switched = NavigationObservationAdapter().ingest(replace(valid_snapshot_v3(sequence=1),
                                                                    episode_id='next-world'))
            session.observe(switched, ())
            self.assertEqual(session.report.reason, 'world_session_changed')
            self.assertGreater(session.current_computation_scope.generation, old.generation)
            self.assertEqual(session.current_computation_scope.world_session_id, switched.session.value)
            self.assertFalse(any(e.operation == 'apply' for e in session._planning_coordinator.async_diagnostics.events))
        finally:
            session.close()

    def test_new_anchor_invalidates_old_delivery_in_runtime_chain(self):
        worker, checked = HeldPlanner(), []
        def move_anchor(context):
            old = context.session.current_computation_scope
            worker.hold = False
            candidate = worker.poll_latest()
            current = context.driver.runtime.navigation_observation_adapter.latest_frame
            context.session.observe(current, ())
            context.session._reissue_request_from_current(current,
                'r28-explicit-new-anchor',
                computation_invalidation=ComputationInvalidationCause.NEW_STATE_ANCHOR)
            worker.deliveries.append(candidate)
            checked.append((context.session, old, candidate.work_identity))
        result = run(replace(flat(), events=[Event('body-entry-changed',
            lambda c: worker._job is not None, move_anchor)]), planner_factory=lambda: worker)
        self.check_safe(result)
        session, old, identity = checked[0]
        self.assertGreater(session.current_computation_scope.generation, old.generation)
        self.assertFalse(any(e.operation == 'apply' and e.identity == identity
            for e in session._planning_coordinator.async_diagnostics.events))

    def test_dispatched_placement_survives_revision_and_invalidated_generation(self):
        from mc2p.contracts.behavior import BehaviorProfileV0
        from mc2p.motion_nav.world_interaction import PlacementState
        from tests.sim.async_work_sequences import placement_fixture
        for operation in ('revision', 'generation'):
            with self.subTest(operation=operation):
                clock, backend, runtime, session, driver, _ = placement_fixture()
                transaction = None
                try:
                    for _ in range(100):
                        driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                        if backend.placements:
                            transaction = driver.placement.transaction
                            break
                    self.assertIsNotNone(transaction, 'actual player placement must dispatch')
                    old, window = transaction.work_identity, transaction.work_window
                    if operation == 'revision':
                        driver.replace_goal('placement-goal', 2, _goal((2.5, 64., .5)), clock[0])
                    else:
                        session._goal_requests.invalidate_computation(ComputationInvalidationCause.CANCELLED)
                    for _ in range(60):
                        if transaction.report.terminal:
                            break
                        driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                    self.assertIs(transaction.report.state, PlacementState.COMPLETE)
                    self.assertEqual(transaction.last_admission.identity, old)
                    self.assertEqual(transaction.last_admission.deadline_monotonic_ns, window.deadline_monotonic_ns)
                    self.assertEqual(len(backend.placements), 1)
                    self.assertEqual(backend.items, 2)
                    driver.cancel('r28-placement-probe-end')
                    for _ in range(80):
                        if driver.report.terminal:
                            break
                        driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                    self.assertTrue(driver.report.terminal)
                    driver.release()
                finally:
                    session.close()
                    runtime.close()


if __name__ == '__main__':
    unittest.main()
