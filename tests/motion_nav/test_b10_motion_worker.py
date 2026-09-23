import time
import unittest

from mc2p.motion_nav.motion_solver import (
    GapSolveRequest, LandingRegion, SolveStatus,
)
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveJob, MotionSolverWorker,
)
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from tests.motion_nav.test_b10_gap_solver import fixture


class B10MotionWorkerTests(unittest.TestCase):
    def test_background_worker_returns_the_same_bounded_gap_result(self):
        anchor, world, target, _ = fixture()
        job = GapMotionSolveJob(
            "route-1/action-0", 3, anchor, world,
            GapSolveRequest(
                (0, 1), LandingRegion(*target),
                CandidateExecutionWindow(11, 20),
            ),
        )
        with MotionSolverWorker(max_pending=4) as worker:
            self.assertTrue(worker.submit(job))
            deadline = time.perf_counter() + 5.0
            result = None
            while result is None and time.perf_counter() < deadline:
                available = worker.poll_available()
                if available:
                    result = available[0]
                else:
                    time.sleep(.01)

        self.assertIsNotNone(result)
        self.assertEqual(result.connection_id, job.connection_id)
        self.assertEqual(result.candidate_revision, 3)
        self.assertIs(result.solve_result.status, SolveStatus.SOLVED)
        self.assertGreater(result.elapsed_ns, 0)

    def test_distinct_connections_are_not_replaced_by_arrival_order(self):
        anchor, world, target, _ = fixture()
        request = GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 20),
        )
        jobs = (
            GapMotionSolveJob("route-1/action-0", 1, anchor, world, request),
            GapMotionSolveJob("route-1/action-2", 1, anchor, world, request),
        )
        with MotionSolverWorker(max_pending=4) as worker:
            self.assertTrue(all(worker.submit(job) for job in jobs))
            results = []
            deadline = time.perf_counter() + 5.0
            while len(results) < len(jobs) and time.perf_counter() < deadline:
                results.extend(worker.poll_available())
                if len(results) < len(jobs):
                    time.sleep(.01)

        self.assertEqual(
            {result.connection_id for result in results},
            {job.connection_id for job in jobs},
        )

if __name__ == "__main__":
    unittest.main()
