
from mc2p.motion_nav.async_work import AsyncComputationScope
import time
import unittest
from unittest.mock import patch

from mc2p.motion_nav.motion_solver import (
    GapSolveRequest, LandingRegion, SolveStatus,
)
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveJob, MotionResultInbox, MotionSolverWorker, _execute_job,
)
from mc2p.motion_nav.async_work import (
    AsyncWorkIdentity, AsyncWorkKind,
)
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from tests.motion_nav.test_b10_gap_solver import fixture


class B10MotionWorkerTests(unittest.TestCase):
    def test_shared_inbox_drains_once_and_routes_by_full_identity(self):
        anchor, world, target, _ = fixture()
        request = GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12),
        )
        first_identity = AsyncWorkIdentity(
            AsyncComputationScope(anchor.session.value, "task", 1), "owner-a",
            AsyncWorkKind.MOTION_SOLVE, "edge", 1,
        )
        second_identity = AsyncWorkIdentity(
            AsyncComputationScope(anchor.session.value, "task", 1), "owner-b",
            AsyncWorkKind.MOTION_SOLVE, "edge", 1,
        )
        results = tuple(_execute_job(GapMotionSolveJob(
            identity.subject_id, identity.revision, anchor, world, request,
            identity,
        )) for identity in (first_identity, second_identity))

        class SharedWorker:
            def __init__(self):
                self.polls = 0
            def submit(self, _job): return True
            def poll_available(self):
                self.polls += 1
                return results
            def close(self): return None
            def is_alive(self): return True

        worker = SharedWorker()
        inbox = MotionResultInbox(max_results=4)
        self.addCleanup(inbox.close)
        self.assertTrue(inbox.register(first_identity))
        self.assertTrue(inbox.register(second_identity))
        inbox.attach_worker(worker)
        self.assertTrue(inbox.wait_for_receiver(timeout=5.))
        initial_polls = worker.polls
        inbox.drain_once(worker, 7)
        inbox.drain_once(worker, 7)

        # Control drains never call the worker. The receiver may have another
        # background sweep, but both owner deliveries remain exactly once.
        self.assertGreaterEqual(initial_polls, 1)
        self.assertEqual(
            inbox.take(first_identity)[0].work_identity, first_identity,
        )
        self.assertEqual(
            inbox.take(second_identity)[0].work_identity, second_identity,
        )

    def test_solver_exception_becomes_a_typed_failure(self):
        anchor, world, target, _ = fixture()
        job = GapMotionSolveJob(
            "route-1/action-0", 3, anchor, world,
            GapSolveRequest(
                (0, 1), LandingRegion(*target),
                CandidateExecutionWindow(11, 12),
            ),
        )
        with patch(
            "mc2p.motion_nav.motion_worker.solve_one_cell_gap",
            side_effect=RuntimeError("boom"),
        ):
            result = _execute_job(job)

        self.assertIs(result.solve_result.status, SolveStatus.INTERNAL_ERROR)
        self.assertEqual(result.solve_result.reasons, ("RuntimeError",))

    def test_background_worker_returns_the_same_bounded_gap_result(self):
        anchor, world, target, _ = fixture()
        job = GapMotionSolveJob(
            "route-1/action-0", 3, anchor, world,
            GapSolveRequest(
                (0, 1), LandingRegion(*target),
                CandidateExecutionWindow(11, 12),
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
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12),
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

    def test_two_unfinished_slots_include_completed_unconsumed_results(self):
        anchor, world, target, _ = fixture()
        job = GapMotionSolveJob("unconsumed-gap", 1, anchor, world,
            GapSolveRequest((0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12)))
        with MotionSolverWorker(max_pending=2) as worker:
            self.assertTrue(worker.submit(job))
            # Readiness observes transport metadata; it consumes/deserializes
            # nothing. Once ready, the first request FIFO slot is certainly free.
            self.assertTrue(worker._results._reader.poll(5.))
            self.assertTrue(worker.submit(job))
            self.assertFalse(worker.submit(job), "a completed but unconsumed result still owns its service slot")

if __name__ == "__main__":
    unittest.main()
