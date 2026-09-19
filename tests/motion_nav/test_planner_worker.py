from __future__ import annotations

import time
import unittest
from queue import Empty, Full

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import FixedRouteController, FixedRouteState
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.known_map_planner import (
    KnownMapSnapshotBuilder, PlanningRequest, PlanningStatus, SnapshotBuildStatus,
)
from mc2p.motion_nav.planner_worker import PlannerWorker, _publish_latest
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.known_map_planner import KnownMapBounds, astar_plan, build_walk_graph
from tests.motion_nav.test_fixed_route_walk import FlatFixture, apply, profile
from tests.motion_nav.test_known_map_planning import grid_graph


class PlannerWorkerTests(unittest.TestCase):
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
        _publish_latest(slot, "latest")
        self.assertEqual(slot.values, ["latest"])
        self.assertGreaterEqual(slot.put_attempts, 3)

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
            started=time.perf_counter()
            worker.submit_snapshot(progress.snapshot,motion,request)
            self.assertLess(time.perf_counter()-started,.05)
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
            started=time.perf_counter()
            worker.submit(graph,PlanningRequest(1,'slow','goal',1,'random',start,goal))
            self.assertLess(time.perf_counter()-started,.05)
            polls=[]
            for _ in range(8):
                before=time.perf_counter();polls.append(worker.poll_latest())
                self.assertLess(time.perf_counter()-before,.02)
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

    def test_pending_and_result_slots_keep_the_latest_request(self):
        graph,start,goal=grid_graph(3)
        worker=PlannerWorker(debug_delay_seconds=.15)
        try:
            worker.submit(graph,PlanningRequest(1,'old','goal',1,'random',start,goal))
            time.sleep(.03)
            worker.submit(graph,PlanningRequest(2,'middle','goal',1,'random',start,goal))
            worker.submit(graph,PlanningRequest(3,'latest','goal',1,'random',start,goal))
            deadline=time.perf_counter()+2;seen=[]
            while time.perf_counter()<deadline:
                result=worker.poll_latest()
                if result is not None:seen.append(result)
                if any(item.request_id=='latest' for item in seen):break
                time.sleep(.02)
            self.assertTrue(any(item.request_id=='latest' for item in seen))
            self.assertFalse(any(item.request_id=='middle' for item in seen))
        finally:
            worker.close()

    def test_rapid_burst_retains_the_newest_submission(self):
        graph,start,goal=grid_graph(8)
        worker=PlannerWorker(debug_delay_seconds=.05)
        try:
            for sequence in range(100):
                self.assertTrue(worker.submit(graph,PlanningRequest(
                    sequence,f'burst-{sequence}','goal',1,'random',start,goal)))
            deadline=time.perf_counter()+3;latest=None
            while time.perf_counter()<deadline:
                result=worker.poll_latest()
                if result is not None:latest=result
                if latest is not None and latest.request_id=='burst-99':break
                time.sleep(.01)
            self.assertIsNotNone(latest)
            self.assertEqual(latest.request_id,'burst-99')
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
            self.assertIsNone(worker.poll_latest())
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
            movements=[];control_times=[]
            for sequence in range(1,13):
                started=time.perf_counter()
                decision=controller.decide(fixture.frame(sequence,body))
                worker.poll_latest()
                control_times.append(time.perf_counter()-started)
                self.assertIs(decision.state,FixedRouteState.RUNNING)
                movements.append(decision.movement)
                body=apply(body,decision.movement,motion)
                time.sleep(.05)
            self.assertTrue(all(movement!=MovementV1() for movement in movements))
            self.assertLess(max(control_times),.03)
            self.assertGreater(body.z,.5)
        finally:
            worker.close()


if __name__=='__main__':
    unittest.main()
