from __future__ import annotations

from dataclasses import replace
from queue import Queue
import unittest
from unittest.mock import patch

from mc2p.motion_nav.known_map_planner import plan_known_surface_snapshot
from mc2p.motion_nav.planner_worker import PlannerWorker, PlanningSubmissionStatus
from mc2p.motion_nav.planning_coordinator import PlanningUpdateKind
from tests.motion_nav.test_b07_step_route import frame
from tests.motion_nav import test_planning_coordinator as planning_fixtures
from tests.motion_nav.test_planning_coordinator import _permit, _request, _world


class HeldPlanner:
    def __init__(self):
        self.requests = []
        self.results = []
        self.deliveries = []

    def submit_surface_snapshot(self, snapshot, ground, step, request, jump, **kwargs):
        self.requests.append(request)
        self.results.append(plan_known_surface_snapshot(
            snapshot, ground, step, request, jump, **kwargs))
        return True

    def poll_available(self):
        available, self.deliveries = tuple(self.deliveries), []
        return available

    def poll_latest(self):
        available = self.poll_available()
        return available[-1] if available else None

    def is_alive(self):
        return True

    def close(self):
        pass


class DualPlanningTests(unittest.TestCase):
    def start(self, *, budget=10_000):
        world, planner = _world(), HeldPlanner()
        owner = planning_fixtures.PlanningCoordinatorTests().coordinator(
            planner, snapshot_cells_per_step=budget)
        request = _request(world)
        owner.begin(request, frame(world, 0, (-.5, 1., .5)),
            permit=_permit(), state_anchor=None,
            remaining_damage_budget=request.damage_budget)
        return world, planner, owner, request

    def advance(self, owner, world, tick):
        return owner.advance(frame(world, tick, (-.5, 1., .5)),
            current_scope=owner._request_ledger.current_computation_scope,
            state_anchor=None, edge_probe=None,
            remaining_damage_budget=owner.request.damage_budget)

    def revise(self, owner, revision, **changes):
        request = replace(owner.request, request_id=f'dual-request-{revision}',
            sequence=revision, goal_revision=revision, work_identity=None, **changes)
        owner.revise_request(request)
        return request

    def test_latest_submits_before_held_old_result_without_changing_old_window(self):
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        identity, window = owner.work_identity, owner.work_window
        revised = self.revise(owner, 2)
        self.advance(owner, world, 1)
        self.assertEqual([r.request_id for r in planner.requests],
            [request.request_id, revised.request_id])
        self.assertEqual(planner.requests[0].work_identity, identity)
        starts = {e.identity: e.window for e in owner.async_diagnostics.events
                  if e.operation == 'begin'}
        self.assertEqual(starts[identity], window)
        self.assertEqual(len(starts), 2)
        self.assertEqual(planner.requests[1].work_identity.scope, identity.scope)
        self.assertFalse(any(e.operation == 'finish'
            and e.identity == identity for e in owner.async_diagnostics.events))

    def test_two_results_in_either_order_apply_at_most_one_current_route(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                world, planner, owner, request = self.start()
                self.advance(owner, world, 0)
                self.revise(owner, 2)
                self.advance(owner, world, 1)
                self.assertEqual(len(planner.requests), 2)
                planner.deliveries = list(reversed(planner.results)) if reverse else list(planner.results)
                update = self.advance(owner, world, 2)
                self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
                self.assertEqual(update.goal_revision, 2)
                self.assertEqual(sum(e.operation == 'apply'
                    for e in owner.async_diagnostics.events), 1)

    def test_full_work_capacity_coalesces_to_latest_after_real_receipt(self):
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        self.revise(owner, 2)
        self.advance(owner, world, 1)
        self.revise(owner, 3)
        self.revise(owner, 4)
        self.advance(owner, world, 2)
        self.assertEqual(len(planner.requests), 2)
        from mc2p.motion_nav.known_map_planner import SurfacePlanningStatus
        planner.deliveries = [replace(planner.results[0],
            status=SurfacePlanningStatus.INTERNAL_ERROR, path=(), segments=())]
        self.advance(owner, world, 3)
        self.assertEqual([r.goal_revision for r in planner.requests], [1, 2, 4])

    def test_busy_keeps_prepared_work_and_original_window(self):
        world, planner, owner, request = self.start()
        identity, window = owner.work_identity, owner.work_window
        original = planner.submit_surface_snapshot
        planner.submit_surface_snapshot = lambda *a, **k: PlanningSubmissionStatus.BUSY
        update = self.advance(owner, world, 0)
        self.assertEqual(update.reason, 'planner_capacity_wait')
        self.assertTrue(owner.diagnostics(frame(world, 0, (-.5, 1., .5))).work_identity_valid)
        self.assertIsNotNone(owner.snapshot)
        self.assertIsNone(owner.pipeline.builder)
        planner.submit_surface_snapshot = original
        self.advance(owner, world, 1)
        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(owner.work_identity, identity)
        self.assertEqual(owner.work_window, window)
        self.assertEqual(owner.local_attempt_failures, 0)
        self.assertEqual(owner._retry_ledger.total_recovery_starts, 0)

    def test_two_builders_share_the_original_frame_budget(self):
        from mc2p.motion_nav.known_map_planner import KnownMapSnapshotBuilder
        world, planner, owner, request = self.start(budget=5)
        self.revise(owner, 2)
        calls = []
        advance = KnownMapSnapshotBuilder.advance
        def bounded(builder, view, maximum_cells):
            calls.append(maximum_cells)
            return advance(builder, view, maximum_cells)
        with patch.object(KnownMapSnapshotBuilder, 'advance', bounded):
            self.advance(owner, world, 0)
        self.assertEqual(len(calls), 2)
        self.assertLessEqual(sum(calls), 5)
        self.assertTrue(all(cells > 0 for cells in calls))

    def test_one_expired_work_does_not_end_or_refresh_the_other(self):
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        original = owner.work_identity
        self.revise(owner, 2)
        self.advance(owner, world, 5)
        windows = dict(owner.async_diagnostics.planning_work)
        current = planner.requests[1].work_identity
        self.advance(owner, world, 10)
        self.assertEqual(dict(owner.async_diagnostics.planning_work), {current: windows[current]})
        self.assertIn(original, owner.async_diagnostics.pending_planning_receipts)
        self.assertEqual(owner.local_attempt_failures, 0)
        self.assertEqual(owner._retry_ledger.total_recovery_starts, 0)

    def test_both_expired_keep_receipts_and_do_not_create_third_work(self):
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        self.revise(owner, 2)
        self.advance(owner, world, 1)
        self.advance(owner, world, 10)
        self.advance(owner, world, 11)
        for tick in (12, 13, 14):
            self.advance(owner, world, tick)
        self.assertEqual(len(planner.requests), 2)
        self.assertEqual(len(owner.async_diagnostics.pending_planning_receipts), 2)
        self.assertFalse(owner.async_diagnostics.planning_work)
        self.assertEqual(owner.local_attempt_failures, 1)
        self.assertEqual(owner._retry_ledger.total_recovery_starts, 0)

    def test_cancellation_keeps_transport_slots_until_both_real_receipts(self):
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        self.revise(owner, 2)
        self.advance(owner, world, 1)
        owner.cancel_work()
        self.assertFalse(owner.async_diagnostics.planning_work)
        self.assertEqual(len(owner.async_diagnostics.pending_planning_receipts), 2)
        planner.deliveries = list(planner.results)
        owner._poll_results()
        self.assertFalse(owner.async_diagnostics.pending_planning_receipts)
        self.assertFalse(any(e.operation == 'apply' for e in owner.async_diagnostics.events))

    def test_two_worker_failures_produce_one_task_failure(self):
        from mc2p.motion_nav.known_map_planner import SurfacePlanningStatus
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        self.revise(owner, 2)
        self.advance(owner, world, 1)
        planner.deliveries = [replace(result, status=SurfacePlanningStatus.INTERNAL_ERROR,
            path=(), segments=(), reasons=('planner_worker_died',)) for result in planner.results]
        update = self.advance(owner, world, 2)
        self.assertIs(update.kind, PlanningUpdateKind.FAILED)
        self.assertEqual(update.goal_revision, 2)
        self.assertTrue(owner.failure_matches_retired_work(update.failure))
        self.assertIs(self.advance(owner, world, 3), update)
        self.assertFalse(owner.async_diagnostics.planning_work)
        self.assertFalse(owner.async_diagnostics.pending_planning_receipts)
        self.assertEqual(owner._retry_ledger.total_recovery_starts, 0)

    def test_latest_failure_waits_for_older_legal_positive_work(self):
        from mc2p.motion_nav.known_map_planner import SurfacePlanningStatus
        from tests.motion_nav.test_navigation_session import _goal
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        old = planner.requests[0].work_identity
        old_window = dict(owner.async_diagnostics.planning_work)[old]
        self.revise(owner, 2, goal_state=replace(_goal((1.5, 1., .5)),
            maximum_terminal_speed_blocks_per_second=10.))
        self.advance(owner, world, 1)
        planner.deliveries = [replace(planner.results[1],
            status=SurfacePlanningStatus.INTERNAL_ERROR, path=(), segments=(),
            reasons=('planner_worker_died',))]
        update = self.advance(owner, world, 2)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertEqual(dict(owner.async_diagnostics.planning_work), {old: old_window})
        self.assertEqual(len(planner.requests), 2)
        planner.deliveries = [planner.results[0]]
        update = self.advance(owner, world, 3)
        self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertEqual(update.route.work_identity, old)
        self.assertEqual(update.goal_revision, 2)
        self.assertEqual(sum(e.operation == 'apply'
            for e in owner.async_diagnostics.events), 1)
        self.assertEqual(owner._retry_ledger.total_recovery_starts, 0)

    def test_expired_original_observation_does_not_pin_running_update(self):
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        old = planner.requests[0].work_identity
        self.revise(owner, 2)
        self.advance(owner, world, 5)
        latest = planner.requests[1].work_identity
        window = dict(owner.async_diagnostics.planning_work)[latest]
        update = owner.expire_at_observation(frame(world, 10, (-.5, 1., .5)),
            remaining_damage_budget=request.damage_budget)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertEqual(dict(owner.async_diagnostics.planning_work), {latest: window})
        planner.deliveries = [planner.results[1]]
        update = self.advance(owner, world, 11)
        self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertEqual(update.route.work_identity, latest)
        self.assertEqual(sum(e.operation == 'apply' and e.identity == old
            for e in owner.async_diagnostics.events), 0)

    def test_new_anchor_rejects_both_old_scope_deliveries(self):
        from mc2p.motion_nav.async_work import ComputationInvalidationCause
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        self.revise(owner, 2)
        self.advance(owner, world, 1)
        old = {request.work_identity for request in planner.requests}
        self.assertEqual(len(old), 2)
        scope = owner._request_ledger.invalidate_computation(
            ComputationInvalidationCause.NEW_STATE_ANCHOR)
        planner.deliveries = list(planner.results)
        update = self.advance(owner, world, 2)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertFalse(any(e.operation == 'apply' and e.identity in old
            for e in owner.async_diagnostics.events))
        self.assertTrue(all(identity.scope == scope
            for identity, _ in owner.async_diagnostics.planning_work))
        self.assertEqual({record.identity for record in owner.admission_records
            if record.disposition.value == 'discarded_late'}, old)

    def test_world_change_retires_both_work_and_rejects_their_receipts(self):
        from mc2p.motion_nav.world_model import WorldKnowledge, WorldSessionId
        world, planner, owner, request = self.start()
        self.advance(owner, world, 0)
        self.revise(owner, 2)
        self.advance(owner, world, 1)
        old = {request.work_identity for request in planner.requests}
        switched = WorldKnowledge(WorldSessionId('dual-planning-next-world'))
        update = self.advance(owner, switched, 2)
        self.assertIs(update.kind, PlanningUpdateKind.FAILED)
        self.assertEqual(update.reason, 'world_session_changed')
        self.assertFalse(owner.async_diagnostics.planning_work)
        self.assertEqual(set(owner.async_diagnostics.pending_planning_receipts), old)
        planner.deliveries = list(planner.results)
        owner._poll_results()
        self.assertFalse(owner.async_diagnostics.pending_planning_receipts)
        self.assertFalse(any(e.operation == 'apply' for e in owner.async_diagnostics.events))


class DualWorkerTests(unittest.TestCase):
    def test_transport_full_is_busy_without_removing_the_unread_job(self):
        from tests.motion_nav.test_known_map_planning import grid_graph
        from mc2p.motion_nav.known_map_planner import PlanningRequest
        worker = PlannerWorker.__new__(PlannerWorker)
        worker._closed = False
        worker._submitted, worker._diagnostic_deliveries = [], []
        worker._requests = Queue(1)
        sentinel = object()
        worker._requests.put_nowait(sentinel)
        graph, start, goal = grid_graph(2)
        request = PlanningRequest(1, 'full-transport', 'goal', 1, 'random', start, goal)
        self.assertIs(worker.submit(graph, request), PlanningSubmissionStatus.BUSY)
        self.assertIs(worker._requests.get_nowait(), sentinel)
        self.assertFalse(worker._submitted)
        self.assertIs(worker.submit(graph, request), PlanningSubmissionStatus.ACCEPTED)

    def test_real_dead_worker_reports_each_accepted_work_once(self):
        from tests.motion_nav.test_known_map_planning import grid_graph
        from mc2p.motion_nav.known_map_planner import PlanningRequest
        graph, start, goal = grid_graph(2)
        worker = PlannerWorker(debug_delay_seconds=1.)
        try:
            for number in (1, 2):
                self.assertIs(worker.submit(graph, PlanningRequest(number,
                    f'dead-{number}', 'goal', 1, 'random', start, goal)), PlanningSubmissionStatus.ACCEPTED)
            worker.terminate()
            worker.join(2)
            failures = worker.poll_available()
            self.assertEqual([r.request_id for r in failures], ['dead-1', 'dead-2'])
            self.assertTrue(all(r.reasons == ('planner_worker_died',) for r in failures))
            self.assertEqual(worker.poll_available(), ())
        finally:
            worker.close()


class DualPlanningFailureFormalTests(unittest.TestCase):
    def test_deferred_latest_failure_enters_handoff_after_old_positive_is_inapplicable(self):
        from mc2p.motion_nav.known_map_planner import SurfacePlanningStatus
        from mc2p.motion_nav.navigation_handoff import NavigationHandoffCoordinator
        from mc2p.motion_nav.planner_worker import _execute_job
        from tests.sim.async_monitor import ObservedAsyncActivity
        from tests.sim.runner import Event, InlinePlannerWorker, _goal, run
        from tests.sim.scenarios import SCENARIOS

        class HeldWorker(InlinePlannerWorker):
            def __init__(self):
                super().__init__()
                self.deliveries = []

            def poll_available(self):
                values, self.deliveries = tuple(self.deliveries), []
                return values

            def deliver(self, job, *, fail=False):
                self.activity.append(ObservedAsyncActivity(job.request.work_identity, 'poll'))
                result = _execute_job(job)
                self.deliveries.append(replace(result,
                    status=SurfacePlanningStatus.INTERNAL_ERROR, path=(), segments=(),
                    reasons=('planner_worker_died',)) if fail else result)

        worker, attempts, accepted = HeldWorker(), [], []
        def revise(context):
            target = (1.5, 64., 8.5)
            context.goal_position, context.goal_state = target, _goal(target)
            self.assertTrue(context.driver.replace_goal('goal', 2,
                context.goal_state, context.clock[0]))

        def fail_latest(context):
            self.assertEqual(len(worker._queued_jobs), 1)
            job = worker._queued_jobs.pop(0)
            attempts.append(job.request.work_identity)
            worker.deliver(job, fail=True)

        def deliver_old(context):
            self.assertIsNotNone(worker._job)
            job, worker._job = worker._job, None
            worker.deliver(job)

        stage = NavigationHandoffCoordinator.stage_planning_failure
        def checked(handoff, failure, request, *, planning):
            accepted.append((failure.attempt_id,
                stage(handoff, failure, request, planning=planning)))
            return accepted[-1][1]

        events = [Event('revise', lambda c: c.tick >= 3, revise, goal_revision=2),
            Event('latest-error', lambda c: c.tick >= 5, fail_latest),
            Event('old-positive', lambda c: c.tick >= 6, deliver_old)]
        scenario = replace(next(s for s in SCENARIOS if s.name == 'flat_walk'),
            events=events, expect='failed', max_ticks=50)
        with patch.object(NavigationHandoffCoordinator, 'stage_planning_failure', checked):
            result = run(scenario, planner_factory=lambda: worker)
        self.assertEqual((result.outcome, result.reason), ('failed', 'planning_internal_error'))
        self.assertEqual(accepted, [(attempts[0].key, True)])
        self.assertTrue(result.verification_complete, result.coverage_gaps)
        # The injected INTERNAL_ERROR is deliberately diagnosed by I9.
        # Its failure still has to reach handoff, with no other violation.
        self.assertEqual([(code, reason) for _, code, reason in result.violations],
            [('I9', 'legal goal produced planning internal error')])
        self.assertFalse(result.trace[-1]['source_bound'])
        self.assertEqual(result.recovery_failures, 0)


if __name__ == '__main__':
    unittest.main()
