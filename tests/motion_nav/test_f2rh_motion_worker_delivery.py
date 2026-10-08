"""F2-RH typed cancellation and last-publication gates."""
from __future__ import annotations

from queue import Empty, Full
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.motion_nav.async_work import (
    AsyncComputationScope,
    AsyncWorkIdentity,
    AsyncWorkKind,
)
from mc2p.motion_nav.ground_terminal_search import GroundTerminalSearchStatus
from mc2p.motion_nav.motion_solver import (
    GapSolveRequest,
    LandingRegion,
    SolveStatus,
    revalidate_air_transition,
    solve_one_cell_gap,
)
from mc2p.motion_nav.motion_worker import (
    GapMotionSolveJob,
    MotionSolverWorker,
    MotionJobOperation,
    MotionWorkerCancelStatus,
    MotionWorkerCancellation,
    _cancelled_job_result,
    _execute_job,
    _worker,
)
from mc2p.motion_nav.online_motion import CandidateExecutionWindow
from tests.motion_nav.test_b10_gap_solver import fixture
from tests.motion_nav.test_ground_terminal_search import _request
from mc2p.motion_nav.motion_worker import GroundTerminalSolveJob
from mc2p.motion_nav.motion_coordination import MotionRouteCoordinator
from mc2p.motion_nav.world_model import Aabb


def _identity(anchor, revision=1):
    return AsyncWorkIdentity(
        AsyncComputationScope(anchor.session.value, "f2rh-task", 1),
        "f2rh-owner", AsyncWorkKind.MOTION_SOLVE, "terminal", revision,
    )


class _Ready:
    def set(self):
        pass


class _Startup:
    def put_nowait(self, _item):
        pass


class _Requests:
    def __init__(self, *jobs):
        self.items = [*jobs, None]

    def get(self):
        return self.items.pop(0)


class _Results:
    def __init__(self):
        self.items = []

    def put(self, item, timeout=None):
        self.items.append(item)

    def put_nowait(self, item):
        self.put(item)


class F2RHMotionWorkerDeliveryTests(unittest.TestCase):
    def _matrix_job(self, family, revision):
        anchor, world, target, _ = fixture()
        if family == "air":
            return GapMotionSolveJob(
                f"f2rh/{family}/{revision}", revision, anchor, world,
                GapSolveRequest(
                    (0, 1), LandingRegion(*target),
                    CandidateExecutionWindow(11, 12),
                ),
                _identity(anchor, revision),
            )
        request = _request(
            bounds=Aabb(.17, 63.99, .32, .23, 64.01, .38), lead_ticks=12,
        )
        identity = replace(
            request.work_identity,
            subject_id=f"ground-terminal-{revision}", revision=revision,
        )
        request = replace(
            request, work_identity=identity,
        )
        return GroundTerminalSolveJob(
            f"f2rh/{family}/{revision}", revision, request, 1,
        )

    @staticmethod
    def _terminal_status(result):
        return (
            result.solve_result.status
            if hasattr(result, "solve_result") else
            result.search_result.status
        )

    def test_air_solver_observes_the_same_cooperative_stop_contract(self):
        anchor, world, target, _ = fixture()
        request = GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12),
        )
        result = solve_one_cell_gap(
            anchor, world, request,
            stop_check=lambda: GroundTerminalSearchStatus.CANCELLED,
        )
        self.assertIs(result.status, SolveStatus.CANCELLED)
        self.assertEqual(result.candidates_evaluated, 0)

    def test_air_revalidation_observes_the_same_cooperative_stop_contract(self):
        anchor, world, target, _ = fixture()
        request = GapSolveRequest(
            (0, 1), LandingRegion(*target), CandidateExecutionWindow(11, 12),
        )
        solved = solve_one_cell_gap(anchor, world, request)
        self.assertIs(solved.status, SolveStatus.SOLVED)
        self.assertIsNotNone(solved.proof)

        stopped = revalidate_air_transition(
            solved.proof, anchor, world, request.execution_window,
            stop_check=lambda: GroundTerminalSearchStatus.STALE,
        )

        self.assertIs(stopped.status, SolveStatus.STALE)
        self.assertEqual(stopped.candidates_evaluated, 0)

        job = GapMotionSolveJob(
            "f2rh/air-revalidate", 2, anchor, world, request,
            _identity(anchor, 2), MotionJobOperation.REVALIDATE,
            solved.proof,
        )
        worker_result = _execute_job(
            job, stop_check=lambda: GroundTerminalSearchStatus.STALE,
        )
        self.assertIs(worker_result.solve_result.status, SolveStatus.STALE)

    def test_cancel_reports_typed_backpressure_finished_and_unavailable(self):
        anchor, world, target, _ = fixture()
        identity = _identity(anchor)
        job = GapMotionSolveJob(
            "f2rh/air", 1, anchor, world,
            GapSolveRequest(
                (0, 1), LandingRegion(*target),
                CandidateExecutionWindow(11, 12),
            ),
            identity,
        )
        worker = MotionSolverWorker(max_pending=1)
        self.addCleanup(worker.close)
        with patch.object(worker._controls, "put_nowait", side_effect=Full):
            self.assertIs(
                worker.cancel(identity), MotionWorkerCancelStatus.BACKPRESSURE,
            )
        self.assertTrue(worker.submit(job))
        deadline = __import__("time").perf_counter() + 5.0
        while __import__("time").perf_counter() < deadline:
            results = worker.poll_available()
            if results:
                self.assertIs(results[0].solve_result.status, SolveStatus.CANCELLED)
                break
        else:
            self.fail("cancelled result was not delivered")
        self.assertIs(
            worker.cancel(identity), MotionWorkerCancelStatus.ALREADY_FINISHED,
        )
        worker.close()
        self.assertIs(
            worker.cancel(_identity(anchor, 2)),
            MotionWorkerCancelStatus.WORKER_UNAVAILABLE,
        )

    def test_cancel_backpressure_records_expire_without_unbounded_retry(self):
        anchor, _world, _target, _ = fixture()
        identity = _identity(anchor)
        worker = MotionSolverWorker(max_pending=1)
        self.addCleanup(worker.close)
        clock = [10_000]
        with patch(
                "mc2p.motion_nav.motion_worker.time.perf_counter_ns",
                side_effect=lambda: clock[0]), patch.object(
                worker._controls, "put_nowait", side_effect=Full):
            self.assertIs(
                worker.cancel(identity),
                MotionWorkerCancelStatus.BACKPRESSURE,
            )
            self.assertIn(identity, worker._pending_controls)
            self.assertIn(identity, worker._locally_retired)
            clock[0] += 2_000_000_001
            worker._flush_pending_controls()
        self.assertNotIn(identity, worker._pending_controls)
        self.assertNotIn(identity, worker._locally_retired)

    def test_coordinator_cancel_retry_expires_with_typed_reason(self):
        anchor, _world, _target, _ = fixture()
        identity = _identity(anchor)

        class BackpressuredWorker:
            def cancel(self, *_args):
                return MotionWorkerCancelStatus.BACKPRESSURE

        clock = [2_000_000_001]
        coordinator = object.__new__(MotionRouteCoordinator)
        coordinator.worker = BackpressuredWorker()
        coordinator._clock = lambda: clock[0]
        coordinator._pending_cancellations = {
            identity: (
                GroundTerminalSearchStatus.CANCELLED,
                2_000_000_000,
            ),
        }
        coordinator.last_failure_reason = ""

        coordinator._retry_pending_cancellations()

        self.assertEqual(coordinator._pending_cancellations, {})
        self.assertEqual(
            coordinator.last_failure_reason,
            "motion_cancel_retry_expired",
        )

    def test_final_control_drain_replaces_a_solved_result(self):
        request = _request(
            bounds=Aabb(.17, 63.99, .32, .23, 64.01, .38), lead_ticks=12,
        )
        job = GroundTerminalSolveJob("f2rh/ground", 1, request, 1)
        solved = _execute_job(job)
        executed = [False]

        class Controls:
            emitted = False

            def get_nowait(self):
                if executed[0] and not self.emitted:
                    self.emitted = True
                    return MotionWorkerCancellation(
                        job.work_identity, GroundTerminalSearchStatus.STALE,
                    )
                raise Empty

        results = _Results()

        def execute(*_args, **_kwargs):
            executed[0] = True
            return solved

        with patch(
                "mc2p.motion_nav.motion_worker._prewarm_worker_calculator"), patch(
                "mc2p.motion_nav.motion_worker._execute_job",
                side_effect=execute):
            _worker(
                _Requests(job), results, Controls(), _Ready(), _Startup(),
            )
        self.assertEqual(len(results.items), 1)
        self.assertIs(
            results.items[0].search_result.status,
            GroundTerminalSearchStatus.STALE,
        )

    def test_result_backpressure_rechecks_last_moment_stale_before_retry(self):
        request = _request(
            bounds=Aabb(.17, 63.99, .32, .23, 64.01, .38), lead_ticks=12,
        )
        job = GroundTerminalSolveJob("f2rh/backpressure", 1, request, 1)
        solved = _execute_job(job)

        class Controls:
            allow_stale = False
            emitted = False

            def get_nowait(self):
                if self.allow_stale and not self.emitted:
                    self.emitted = True
                    return MotionWorkerCancellation(
                        job.work_identity, GroundTerminalSearchStatus.STALE,
                    )
                raise Empty

        controls = Controls()

        class FullThenAcceptResults(_Results):
            def put_nowait(self, item):
                if not controls.allow_stale:
                    controls.allow_stale = True
                    raise Full
                super().put_nowait(item)

        results = FullThenAcceptResults()
        with patch(
                "mc2p.motion_nav.motion_worker._prewarm_worker_calculator"), patch(
                "mc2p.motion_nav.motion_worker._execute_job",
                return_value=solved):
            _worker(
                _Requests(job), results, controls, _Ready(), _Startup(),
            )

        self.assertEqual(len(results.items), 1)
        self.assertIs(
            results.items[0].search_result.status,
            GroundTerminalSearchStatus.STALE,
        )

    def test_queued_old_work_matrix_skips_solver_and_starts_current_fifo_job(self):
        combinations = (
            ("ground", "ground"),
            ("air", "ground"),
            ("ground", "air"),
        )
        statuses = (
            GroundTerminalSearchStatus.CANCELLED,
            GroundTerminalSearchStatus.STALE,
            GroundTerminalSearchStatus.TIMEOUT,
        )
        for old_family, current_family in combinations:
            for status in statuses:
                with self.subTest(
                        old=old_family, current=current_family,
                        status=status.value):
                    old = self._matrix_job(old_family, 1)
                    current = self._matrix_job(current_family, 2)
                    controls = [MotionWorkerCancellation(
                        old.work_identity, status,
                    )]

                    class Controls:
                        def get_nowait(self):
                            if controls:
                                return controls.pop(0)
                            raise Empty

                    executed = []

                    def execute(job, **kwargs):
                        executed.append(job.work_identity)
                        return _cancelled_job_result(
                            job, GroundTerminalSearchStatus.TIMEOUT,
                            kwargs["dequeued_monotonic_ns"],
                        )

                    results = _Results()
                    with patch(
                            "mc2p.motion_nav.motion_worker."
                            "_prewarm_worker_calculator"), patch(
                            "mc2p.motion_nav.motion_worker._execute_job",
                            side_effect=execute):
                        _worker(
                            _Requests(old, current), results, Controls(),
                            _Ready(), _Startup(),
                        )

                    self.assertEqual(executed, [current.work_identity])
                    self.assertEqual(len(results.items), 2)
                    expected = {
                        GroundTerminalSearchStatus.CANCELLED: SolveStatus.CANCELLED,
                        GroundTerminalSearchStatus.STALE: SolveStatus.STALE,
                        GroundTerminalSearchStatus.TIMEOUT: SolveStatus.TIMEOUT,
                    }[status] if old_family == "air" else status
                    self.assertIs(
                        self._terminal_status(results.items[0]), expected,
                    )

    def test_running_old_work_matrix_stops_at_checkpoint_before_current_job(self):
        combinations = (
            ("ground", "ground"),
            ("air", "ground"),
            ("ground", "air"),
        )
        statuses = (
            GroundTerminalSearchStatus.CANCELLED,
            GroundTerminalSearchStatus.STALE,
            GroundTerminalSearchStatus.TIMEOUT,
        )
        for old_family, current_family in combinations:
            for status in statuses:
                with self.subTest(
                        old=old_family, current=current_family,
                        status=status.value):
                    old = self._matrix_job(old_family, 1)
                    current = self._matrix_job(current_family, 2)
                    running = [False]
                    emitted = [False]

                    class Controls:
                        def get_nowait(self):
                            if running[0] and not emitted[0]:
                                emitted[0] = True
                                return MotionWorkerCancellation(
                                    old.work_identity, status,
                                )
                            raise Empty

                    executed = []

                    def execute(job, *, stop_check, dequeued_monotonic_ns):
                        executed.append(job.work_identity)
                        if job is old:
                            running[0] = True
                            stopped = stop_check()
                            self.assertIs(stopped, status)
                            return _cancelled_job_result(
                                job, stopped, dequeued_monotonic_ns,
                            )
                        return _cancelled_job_result(
                            job, GroundTerminalSearchStatus.TIMEOUT,
                            dequeued_monotonic_ns,
                        )

                    results = _Results()
                    with patch(
                            "mc2p.motion_nav.motion_worker."
                            "_prewarm_worker_calculator"), patch(
                            "mc2p.motion_nav.motion_worker._execute_job",
                            side_effect=execute):
                        _worker(
                            _Requests(old, current), results, Controls(),
                            _Ready(), _Startup(),
                        )

                    self.assertEqual(
                        executed, [old.work_identity, current.work_identity],
                    )
                    self.assertEqual(len(results.items), 2)

    def test_two_valid_cross_family_jobs_keep_fifo_order(self):
        for first_family, second_family in (
                ("air", "ground"), ("ground", "air")):
            with self.subTest(first=first_family, second=second_family):
                first = self._matrix_job(first_family, 1)
                second = self._matrix_job(second_family, 2)
                executed = []

                def execute(job, *, dequeued_monotonic_ns, **_kwargs):
                    executed.append(job.work_identity)
                    return _cancelled_job_result(
                        job, GroundTerminalSearchStatus.TIMEOUT,
                        dequeued_monotonic_ns,
                    )

                results = _Results()
                with patch(
                        "mc2p.motion_nav.motion_worker."
                        "_prewarm_worker_calculator"), patch(
                        "mc2p.motion_nav.motion_worker._execute_job",
                        side_effect=execute):
                    _worker(
                        _Requests(first, second), results,
                        type("NoControls", (), {
                            "get_nowait": lambda _self: (_ for _ in ()).throw(Empty),
                        })(),
                        _Ready(), _Startup(),
                    )

                self.assertEqual(
                    executed, [first.work_identity, second.work_identity],
                )

    def test_real_running_air_solve_and_revalidation_stop_then_start_ground(self):
        expected_status = {
            GroundTerminalSearchStatus.CANCELLED: SolveStatus.CANCELLED,
            GroundTerminalSearchStatus.STALE: SolveStatus.STALE,
            GroundTerminalSearchStatus.TIMEOUT: SolveStatus.TIMEOUT,
        }
        for operation in (MotionJobOperation.SOLVE,
                          MotionJobOperation.REVALIDATE):
            for status in expected_status:
                with self.subTest(operation=operation.value, status=status.value):
                    old = self._matrix_job("air", 1)
                    if operation is MotionJobOperation.REVALIDATE:
                        baseline = solve_one_cell_gap(
                            old.anchor, old.world, old.request,
                        )
                        self.assertIs(baseline.status, SolveStatus.SOLVED)
                        old = replace(
                            old, operation=operation, proof=baseline.proof,
                        )
                    current = self._matrix_job("ground", 2)

                    class DelayedControls:
                        calls = 0
                        emitted = False

                        def get_nowait(self):
                            self.calls += 1
                            # Call 1 is the worker's pre-dequeue drain.  The
                            # fourth call is inside real air replay/recovery,
                            # so an entry-only stop check cannot pass.
                            if self.calls == 4 and not self.emitted:
                                self.emitted = True
                                return MotionWorkerCancellation(
                                    old.work_identity, status,
                                )
                            raise Empty

                    controls = DelayedControls()
                    results = _Results()
                    with patch(
                            "mc2p.motion_nav.motion_worker."
                            "_prewarm_worker_calculator"):
                        _worker(
                            _Requests(old, current), results, controls,
                            _Ready(), _Startup(),
                        )

                    self.assertTrue(controls.emitted)
                    self.assertEqual(len(results.items), 2)
                    self.assertIs(
                        results.items[0].solve_result.status,
                        expected_status[status],
                    )
                    self.assertEqual(
                        results.items[1].work_identity,
                        current.work_identity,
                    )


if __name__ == "__main__":
    unittest.main()
