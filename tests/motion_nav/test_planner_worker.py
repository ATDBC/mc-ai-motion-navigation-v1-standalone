from __future__ import annotations

from mc2p.motion_nav.async_work import AsyncComputationScope

import time
import unittest
from dataclasses import replace
from queue import Empty, Full, Queue

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import FixedRouteController, FixedRouteState
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.known_map_planner import (
    KnownMapSnapshotBuilder, PlanningRequest, PlanningStatus, SnapshotBuildStatus,
)
from mc2p.motion_nav.planner_worker import PlannerWorker, PlanningSubmissionStatus, _publish
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.known_map_planner import KnownMapBounds, astar_plan, build_walk_graph
from tests.motion_nav.test_fixed_route_walk import FlatFixture, apply, profile
from tests.motion_nav.test_known_map_planning import grid_graph


def cross_task_sequence_probe(*, deliver_current: bool,
                              old_sequence: int = 99, current_sequence: int = 1,
                              current_first: bool = False) -> dict:
    """Deterministic result-transport injection into the real worker methods.

    The controlled queues avoid process timing; typed submission and the
    production poll_latest selection/cleanup both run unchanged.  This is an
    uncollected A0 defect probe, not a test requiring defective behavior.
    """
    from mc2p.motion_nav.async_work import AsyncWorkIdentity, AsyncWorkKind
    from mc2p.motion_nav.known_map_planner import plan_known_surface_snapshot
    from tests.motion_nav.test_planning_coordinator import _request, _world
    from tests.motion_nav.test_b07_step_route import step_profile
    from tests.motion_nav.test_b07_surface_planning import ordinary_profile
    from tests.motion_nav.test_jump_up import jump_profile

    class LiveProcess:
        def is_alive(self):
            return True

    world = _world()
    snapshot = KnownMapSnapshotBuilder(
        world.view(), KnownMapBounds(-2, 2, 1, 1, -1, 1, True),
    ).advance(world.view(), 100_000).snapshot
    request = _request(world)
    old = replace(request, sequence=old_sequence, request_id="a0-old-task-request",
                  work_identity=AsyncWorkIdentity(
                      AsyncComputationScope(world.session.value, "old-task", 1), "old-owner",
                      AsyncWorkKind.PLANNING, "a0-old-task-request", 1))
    current = replace(request, sequence=current_sequence, request_id="a0-current-task-request",
                      work_identity=AsyncWorkIdentity(
                          AsyncComputationScope(world.session.value, "current-task", 1), "current-owner",
                          AsyncWorkKind.PLANNING, "a0-current-task-request", 1))
    worker = PlannerWorker.__new__(PlannerWorker)
    worker._closed = False
    worker._submitted, worker._diagnostic_deliveries = [], []
    worker._process = LiveProcess()
    worker._requests = Queue(maxsize=1)
    # A transport can make two successive deliveries readable in one poll.
    # The queue permits exactly those deliveries; it is not a production
    # queue-capacity change or a simulation of background publication.
    worker._results = Queue(maxsize=2)
    ground, step, jump = ordinary_profile(), step_profile(), jump_profile()
    worker.submit_surface_snapshot(snapshot, ground, step, current, jump)
    old_result = plan_known_surface_snapshot(snapshot, ground, step, old, jump)
    current_result = plan_known_surface_snapshot(snapshot, ground, step, current, jump)
    if deliver_current and current_first:
        worker._results.put_nowait(current_result)
    worker._results.put_nowait(old_result)
    if deliver_current and not current_first:
        worker._results.put_nowait(current_result)
    delivered = worker.poll_available()
    chosen = next((item for item in delivered if item.work_identity == current.work_identity), delivered[-1])
    return {
        "old_task_sequence": old.sequence,
        "current_task_sequence": current.sequence,
        "current_result_delivered": deliver_current,
        "chosen_request_id": chosen.request_id,
        "chosen_task_id": chosen.work_identity.task_id,
        "current_submission_cleared": not worker._submitted,
        "current_result_lost": deliver_current and current_result not in delivered,
        "old_result_lost": old_result not in delivered,
    }


class PlannerWorkerTests(unittest.TestCase):
    def test_full_identity_wins_for_both_sequence_orders_and_delivery_orders(self):
        for old_sequence, current_sequence in ((99, 1), (1, 99)):
            for current_first in (False, True):
                with self.subTest(old_sequence=old_sequence, current_first=current_first):
                    observed = cross_task_sequence_probe(deliver_current=True,
                        old_sequence=old_sequence, current_sequence=current_sequence,
                        current_first=current_first)
                    self.assertEqual(observed['chosen_task_id'], 'current-task')
                    self.assertTrue(observed['current_submission_cleared'])

    def test_old_duplicate_cannot_clear_next_work_and_death_reports_current(self):
        from mc2p.motion_nav.async_work import AsyncWorkIdentity, AsyncWorkKind
        class Process:
            alive = True
            def is_alive(self):
                return self.alive
        graph, start, goal = grid_graph(2)
        request = PlanningRequest(99, 'old', 'goal', 1, 'random', start, goal,
            work_identity=AsyncWorkIdentity(AsyncComputationScope('random', 'old-task', 1),
                                           'old-owner', AsyncWorkKind.PLANNING, 'old', 1))
        worker = PlannerWorker.__new__(PlannerWorker)
        worker._closed = False
        worker._submitted, worker._diagnostic_deliveries = [], []
        worker._process, worker._requests, worker._results = Process(), Queue(2), Queue(3)
        worker.submit(graph, request)
        old = astar_plan(graph, request)
        worker._results.put(old)
        self.assertEqual(worker.poll_latest(), old)
        current = replace(request, sequence=1, request_id='current',
            work_identity=AsyncWorkIdentity(AsyncComputationScope('random', 'current-task', 2),
                                           'new-owner', AsyncWorkKind.PLANNING, 'current', 1))
        worker.submit(graph, current)
        worker._results.put(old)
        worker._results.put(old)
        self.assertEqual(worker.poll_available(), (old, old))
        self.assertEqual(worker._submitted[0].request, current)
        worker._process.alive = False
        failure = worker.poll_latest()
        self.assertEqual(failure.work_identity, current.work_identity)
        self.assertEqual(failure.request_id, 'current')
        self.assertEqual(failure.reasons, ('planner_worker_died',))
        self.assertIsNone(worker.poll_latest())

    def test_diagnostic_result_requires_exact_producer_without_default_scope(self):
        graph, start, goal = grid_graph(2)
        old = PlanningRequest(99, 'diagnostic-old', 'goal', 1, 'random', start, goal)
        current = replace(old, sequence=1, request_id='diagnostic-current')
        from mc2p.motion_nav.planner_worker import _PlanningJob
        job = _PlanningJob(graph, None, None, None, None, None, (), current)
        self.assertFalse(PlannerWorker._matches_submission(astar_plan(graph, old), job))
        self.assertTrue(PlannerWorker._matches_submission(astar_plan(graph, current), job))

    def test_old_scope_cannot_clear_current_submission(self):
        observed = cross_task_sequence_probe(deliver_current=False)
        self.assertFalse(observed['current_submission_cleared'])

    def test_current_scope_result_wins_over_old_high_sequence(self):
        observed = cross_task_sequence_probe(deliver_current=True)
        self.assertEqual(observed['chosen_task_id'], 'current-task')
        self.assertFalse(observed['current_result_lost'])

    def test_result_publication_retries_after_a_transient_full_slot_race(self):
        class TransientSlot:
            def __init__(self):
                self.values = ["old"]
                self.put_attempts = 0
                self.get_attempts = 0

            def put(self, value, timeout):
                self.put_attempts += 1
                if self.put_attempts <= 2:
                    raise Full
                self.values.append(value)

            def get(self, timeout):
                self.get_attempts += 1
                if self.get_attempts == 1:
                    raise Empty
                return self.values.pop(0)

        slot = TransientSlot()
        _publish(slot, "latest")
        self.assertEqual(slot.values, ["old", "latest"])
        self.assertGreaterEqual(slot.put_attempts, 3)
        self.assertEqual(slot.get_attempts, 0)

    def test_snapshot_graph_construction_and_search_both_run_in_worker(self):
        fixture=FlatFixture();motion=profile()
        bounds=KnownMapBounds(-8,8,1,1,-2,15,True)
        builder=KnownMapSnapshotBuilder(fixture.world.view(),bounds)
        progress=builder.advance(fixture.world.view(),100_000)
        self.assertIs(progress.status,SnapshotBuildStatus.COMPLETE)
        request=PlanningRequest(1,'snapshot-worker','goal',1,fixture.session.value,
                                (0,1,0),(0,1,12))
        worker=PlannerWorker(debug_delay_seconds=.1)
        try:
            worker.submit_snapshot(progress.snapshot,motion,request)
            result=None;deadline=time.perf_counter()+3
            while result is None and time.perf_counter()<deadline:
                result=worker.poll_latest();time.sleep(.01)
            self.assertIsNotNone(result)
            self.assertIs(result.status,PlanningStatus.COMPLETE)
            self.assertEqual(result.request_id,'snapshot-worker')
        finally:
            worker.close()

    def test_background_delay_never_blocks_submit_or_poll(self):
        graph,start,goal=grid_graph(2)
        worker=PlannerWorker(debug_delay_seconds=.5)
        try:
            worker.submit(graph,PlanningRequest(1,'slow','goal',1,'random',start,goal))
            polls=[]
            for _ in range(8):
                polls.append(worker.poll_latest())
                time.sleep(.03)
            self.assertTrue(all(result is None for result in polls))
            deadline=time.perf_counter()+2
            result=None
            while result is None and time.perf_counter()<deadline:
                result=worker.poll_latest();time.sleep(.01)
            self.assertIsNotNone(result)
            self.assertEqual(result.request_id,'slow')
        finally:
            worker.close()

    def test_two_accepted_requests_both_arrive_and_third_is_busy(self):
        graph,start,goal=grid_graph(3)
        worker=PlannerWorker(debug_delay_seconds=.15)
        try:
            self.assertIs(worker.submit(graph,PlanningRequest(1,'old','goal',1,'random',start,goal)), PlanningSubmissionStatus.ACCEPTED)
            time.sleep(.03)
            self.assertIs(worker.submit(graph,PlanningRequest(2,'middle','goal',1,'random',start,goal)), PlanningSubmissionStatus.ACCEPTED)
            self.assertIs(worker.submit(graph,PlanningRequest(3,'latest','goal',1,'random',start,goal)), PlanningSubmissionStatus.BUSY)
            deadline=time.perf_counter()+2;seen=[]
            while time.perf_counter()<deadline:
                seen.extend(worker.poll_available())
                if len(seen) == 2:break
                time.sleep(.02)
            self.assertEqual([item.request_id for item in seen], ['old', 'middle'])
        finally:
            worker.close()

    def test_rapid_burst_cannot_replace_two_accepted_submissions(self):
        graph,start,goal=grid_graph(8)
        worker=PlannerWorker(debug_delay_seconds=.05)
        try:
            statuses = [worker.submit(graph,PlanningRequest(
                sequence,f'burst-{sequence}','goal',1,'random',start,goal)) for sequence in range(100)]
            self.assertEqual(statuses[:2], [PlanningSubmissionStatus.ACCEPTED] * 2)
            self.assertEqual(statuses[2:], [PlanningSubmissionStatus.BUSY] * 98)
            deadline=time.perf_counter()+3;seen=[]
            while time.perf_counter()<deadline:
                seen.extend(worker.poll_available())
                if len(seen) == 2:break
                time.sleep(.01)
            self.assertEqual([item.request_id for item in seen], ['burst-0', 'burst-1'])
        finally:
            worker.close()

    def test_dead_worker_is_reported_without_synthesizing_a_route(self):
        graph,start,goal=grid_graph(4)
        worker=PlannerWorker()
        try:
            worker.submit(graph,PlanningRequest(1,'request','goal',1,'random',start,goal))
            worker.terminate()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            failed = worker.poll_latest()
            self.assertIs(failed.status, PlanningStatus.INTERNAL_ERROR)
            self.assertEqual(failed.request_id, "request")
            self.assertEqual(failed.reasons, ("planner_worker_died",))
        finally:
            worker.close()

    def test_fixed_route_control_continues_during_five_hundred_ms_search(self):
        fixture=FlatFixture();motion=profile()
        graph=build_walk_graph(fixture.world.view(),KnownMapBounds(-2,2,1,1,-2,15,True),motion)
        request=PlanningRequest(1,'initial','goal',1,fixture.session.value,(0,1,0),(0,1,12))
        candidate=astar_plan(graph,request)
        body=PlanarBodyState(.5,.5,0,0,0)
        admitted=RouteAdmitter(maximum_corridor_blocks=10).admit(
            candidate,fixture.frame(0,body),expected_request_id='initial',
            goal_id='goal',goal_revision=1,changed_cells=())
        self.assertIs(admitted.status,AdmissionStatus.ACCEPTED)
        controller=FixedRouteController(motion);controller.start(admitted.route.fixed_route,fixture.frame(0,body))
        worker=PlannerWorker(debug_delay_seconds=.5)
        try:
            worker.submit(graph,PlanningRequest(2,'delayed','goal',1,fixture.session.value,
                                                (0,1,0),(0,1,10)))
            movements=[]
            for sequence in range(1,13):
                decision=controller.decide(fixture.frame(sequence,body))
                worker.poll_latest()
                self.assertIs(decision.state,FixedRouteState.RUNNING)
                movements.append(decision.movement)
                body=apply(body,decision.movement,motion)
                time.sleep(.05)
            self.assertTrue(all(movement!=MovementV1() for movement in movements))
            self.assertGreater(body.z,.5)
        finally:
            worker.close()


if __name__=='__main__':
    unittest.main()
