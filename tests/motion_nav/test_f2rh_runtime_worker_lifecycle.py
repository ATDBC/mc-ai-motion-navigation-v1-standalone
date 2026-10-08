"""F2-RH Runtime ownership and typed Motion worker lifecycle gates."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
import unittest

from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.motion_worker import (
    MotionSolverWorker,
    MotionWorkerCancelStatus,
    MotionWorkerHealth,
    MotionWorkerReadiness,
)
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
)
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.navigation_session_driver import (
    RuntimeNavigationDriver,
    RuntimeNavigationDriverState,
)
from tests.motion_nav.test_b07_step_route import step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_b09_air_transitions import air_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal
from tests.motion_nav.test_runtime_navigation_verified_handoff import (
    _GapRuntimeBackend,
)
from tests.test_player_runtime import _RecordingTrace


class _LifecycleWorker:
    def __init__(self, readiness=MotionWorkerReadiness.READY):
        self.readiness = readiness
        self.pid = 24680
        self.close_calls = 0

    @property
    def health(self):
        return MotionWorkerHealth(
            self.readiness, self.pid, 1 if self.readiness is MotionWorkerReadiness.READY else None,
            None if self.readiness is MotionWorkerReadiness.READY else 2,
            None if self.readiness is MotionWorkerReadiness.READY else "injected_failure",
        )

    def is_alive(self):
        return self.readiness is MotionWorkerReadiness.READY

    def submit(self, _job):
        return self.is_alive()

    def poll_available(self):
        return ()

    def cancel(self, *_args):
        return (
            MotionWorkerCancelStatus.ACCEPTED if self.is_alive()
            else MotionWorkerCancelStatus.WORKER_UNAVAILABLE
        )

    def close(self):
        self.close_calls += 1
        self.readiness = MotionWorkerReadiness.CLOSED


def _profiles():
    return NavigationSessionProfiles(
        replace(
            ordinary_profile(),
            support_materials=frozenset({"minecraft:grass_block"}),
        ),
        jump_profile(), step_profile(),
        air=(air_profile(MovementMode.JUMP_GAP),),
    )


class F2RHRuntimeWorkerLifecycleTests(unittest.TestCase):
    def test_session_source_has_no_motion_worker_lifetime_paths(self):
        source = (Path(__file__).parents[2] / "mc2p" / "motion_nav"
                  / "navigation_session.py").read_text(encoding="utf-8")
        self.assertNotIn("MotionSolverWorker", source)
        self.assertNotIn("_owns_motion_worker", source)
        self.assertNotIn("self._motion_worker.close()", source)

    def _runtime(self, worker):
        clock = [100_000_000]
        backend = _GapRuntimeBackend(clock)
        runtime = PlayerRuntimeV1(
            backend, _RecordingTrace(), lambda: clock[0],
            motion_worker_factory=lambda: worker,
        )
        reset = runtime.reset(ResetRequestV0(
            "f2rh-reset", "episode-verified-runtime", "test", 1,
            10_000_000_000,
        ))
        self.assertTrue(reset.succeeded)
        return runtime, clock

    def test_runtime_reuses_one_borrowed_worker_and_closes_it_once(self):
        worker = _LifecycleWorker()
        runtime, clock = self._runtime(worker)
        self.addCleanup(runtime.close)
        first = NavigationSession(
            "f2rh-first", _profiles(), planner_worker=_InlinePlanner(),
            clock_ns=lambda: clock[0],
        )
        RuntimeNavigationDriver(runtime, first, clock_ns=lambda: clock[0])
        self.assertIs(first.borrowed_motion_worker, worker)
        first.close()
        self.assertEqual(worker.close_calls, 0)

        second = NavigationSession(
            "f2rh-second", _profiles(), planner_worker=_InlinePlanner(),
            clock_ns=lambda: clock[0],
        )
        RuntimeNavigationDriver(runtime, second, clock_ns=lambda: clock[0])
        self.assertIs(second.borrowed_motion_worker, worker)
        self.assertEqual(runtime.motion_worker_health.pid, 24680)
        second.close()
        self.assertEqual(worker.close_calls, 0)
        runtime.close()
        self.assertEqual(worker.close_calls, 1)

    def test_unavailable_worker_fails_before_input_source_registration(self):
        worker = _LifecycleWorker(MotionWorkerReadiness.INITIALIZATION_FAILED)
        runtime, clock = self._runtime(worker)
        self.addCleanup(runtime.close)
        session = NavigationSession(
            "f2rh-unavailable", _profiles(), planner_worker=_InlinePlanner(),
            clock_ns=lambda: clock[0],
        )
        driver = RuntimeNavigationDriver(
            runtime, session, clock_ns=lambda: clock[0],
        )
        before = runtime.ordered_source_stats
        driver.start(
            "f2rh-goal", 1, _goal((.5, 64.0, 2.5)), clock[0],
        )
        self.assertIs(driver.state, RuntimeNavigationDriverState.FAILED)
        self.assertEqual(driver.reason, "motion_worker_unavailable")
        self.assertIsNone(driver.source)
        self.assertEqual(runtime.ordered_source_stats, before)
        self.assertIsNone(session.report.goal_id)

    def test_worker_factory_exception_becomes_typed_initialization_failure(self):
        clock = [100_000_000]
        backend = _GapRuntimeBackend(clock)

        def fail_factory():
            raise RuntimeError("spawn failed")

        runtime = PlayerRuntimeV1(
            backend, _RecordingTrace(), lambda: clock[0],
            motion_worker_factory=fail_factory,
        )
        self.addCleanup(runtime.close)
        reset = runtime.reset(ResetRequestV0(
            "f2rh-factory-reset", "episode-verified-runtime", "test", 1,
            10_000_000_000,
        ))
        self.assertTrue(reset.succeeded)
        session = NavigationSession(
            "f2rh-factory-failure", _profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: clock[0],
        )

        driver = RuntimeNavigationDriver(
            runtime, session, clock_ns=lambda: clock[0],
        )
        driver.start(
            "f2rh-factory-goal", 1, _goal((.5, 64.0, 2.5)), clock[0],
        )

        self.assertIs(
            runtime.motion_worker_health.readiness,
            MotionWorkerReadiness.INITIALIZATION_FAILED,
        )
        self.assertEqual(
            runtime.motion_worker_health.failure_type, "RuntimeError",
        )
        self.assertIs(driver.state, RuntimeNavigationDriverState.FAILED)
        self.assertIsNone(driver.source)

    def test_ready_process_death_is_reported_as_dead(self):
        worker = MotionSolverWorker(max_pending=1)
        self.addCleanup(worker.close)
        self.assertIs(worker.readiness, MotionWorkerReadiness.READY)
        pid = worker.health.pid
        worker._process.terminate()
        worker._process.join(2.0)
        self.assertIs(worker.readiness, MotionWorkerReadiness.DEAD)
        self.assertEqual(worker.health.pid, pid)
        self.assertIsNotNone(worker.health.failed_monotonic_ns)

    def test_seal_then_close_closes_the_runtime_worker_once(self):
        worker = _LifecycleWorker()
        runtime, _clock = self._runtime(worker)
        self.assertIs(runtime.borrow_motion_worker(worker), worker)

        runtime._seal()
        runtime.close()

        self.assertEqual(worker.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
