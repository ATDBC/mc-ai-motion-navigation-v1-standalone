"""Frozen v7 transport delay; solver and formal activity remain unchanged."""
from collections import deque
from copy import deepcopy
import math

from mc2p.motion_nav.motion_solver import GapSolveRequest, MotionSolveKind
from mc2p.motion_nav.motion_worker import GapMotionSolveJob, _execute_job
from tests.sim.async_monitor import ObservedAsyncActivity


DELIVERY_PROFILE = {
    "id": "mc2p.navigation-motion-delivery.r28-v7.v1",
    "base_manifest_sha256": "9571d30dafc93608806dff04e10a1042caf3a7a0421958e44ee3f9467b2a192e",
    "clock": "movement_tick",
    "cold_first_job_ticks": 7,
    "warm_ticks": {"jump_gap": 1, "jump_up": 1, "drop_2": 2, "drop_5": 3},
    "revalidation": "corresponding_action_warm_ticks",
    "max_pending": 8,
}


def validate_profile(profile):
    if profile != DELIVERY_PROFILE:
        raise ValueError("motion delivery profile differs from frozen v7 model")
    return deepcopy(DELIVERY_PROFILE)


def action_name(job):
    if type(job.request) is GapSolveRequest:
        return "jump_gap"
    if job.request.kind is MotionSolveKind.JUMP_UP:
        return "jump_up"
    height = job.anchor.physics_state.position[1] - job.request.landing.surface_y
    for declared in (2, 5):
        if math.isclose(height, declared, abs_tol=1e-6):
            return f"drop_{declared}"
    raise ValueError(f"v7 delivery has no declared drop height: {height}")


class DeterministicMotionWorker:
    """Bounded FIFO driven by explicit physical ticks, never by poll count."""

    pid = None

    def __init__(self, profile=None, *, max_pending=None):
        self.profile = validate_profile(DELIVERY_PROFILE if profile is None else profile)
        self._capacity = self.profile["max_pending"] if max_pending is None else max_pending
        if type(self._capacity) is not int or not 1 <= self._capacity <= 64:
            raise ValueError("motion delivery capacity must be within 1..64")
        self._pending = deque()
        self._tick = None
        self._closed = False
        self.poll_count = 0
        self.activity = []
        self.records = []

    def advance_tick(self, tick):
        if (type(tick) is not int or tick < 0
                or self._tick is not None and tick < self._tick):
            raise ValueError("motion delivery requires monotonic physical ticks")
        if tick != self._tick:
            # The product ruler's receive-cycle identity follows its virtual
            # clock.  Counting background-thread calls would make otherwise
            # identical runs depend on host scheduling.
            self.poll_count += 1
        self._tick = tick

    def observe_simulated_movement_tick(self, movement_tick: int) -> None:
        """Bind delivery to the shared harness clock used by every worker."""
        self.advance_tick(movement_tick)

    def is_alive(self):
        return not self._closed

    def submit(self, job):
        if self._closed or self._tick is None:
            raise ValueError("motion delivery worker is closed or has no physical clock")
        if type(job) is not GapMotionSolveJob:
            raise TypeError("motion delivery requires a typed job")
        if len(self._pending) >= self._capacity:
            return False
        action = action_name(job)
        cold = not self.records
        delay = self.profile["cold_first_job_ticks"] if cold else self.profile["warm_ticks"][action]
        record = {"index": len(self.records) + 1, "operation": job.operation.value,
                  "action": action, "cold_start": cold, "delay_ticks": delay,
                  "source_tick": job.anchor.movement_tick_id,
                  "submission_tick": self._tick, "ready_tick": self._tick + delay,
                  "delivery_tick": None, "submission_poll": self.poll_count,
                  "delivery_poll": None, "status": None,
                  "preparation_ticks": len(job.entry_prefix)}
        self.records.append(record)
        self._pending.append((job, record))
        self.activity.append(ObservedAsyncActivity(job.work_identity, "submit"))
        return True

    def poll_available(self):
        if self._closed:
            return ()
        if self._tick is None:
            raise ValueError("motion delivery has no physical clock")
        ready = []
        while self._pending and self._pending[0][1]["ready_tick"] <= self._tick:
            job, record = self._pending.popleft()
            result = _execute_job(job)
            record.update(delivery_tick=self._tick, delivery_poll=self.poll_count,
                          status=result.solve_result.status.value)
            self.activity.append(ObservedAsyncActivity(job.work_identity, "poll"))
            ready.append(result)
        return tuple(ready)

    def close(self):
        self._closed = True
        self._pending.clear()

    def control_step(self, context):
        # ``runner.run`` publishes this movement tick before it asks any
        # control source to decide.  Keep the explicit call idempotent for
        # direct users of this control step outside that runner.
        self.observe_simulated_movement_tick(context.backend.movement_tick)
        inbox = context.session._motion_result_inbox
        if (inbox.receiver_thread_id is not None
                and not inbox.wait_for_receiver(timeout=10.)):
            raise AssertionError(
                "motion receiver did not observe the advanced virtual tick"
            )
        # This is the runner's normal control step with only transport time set.
        from mc2p.contracts.behavior import BehaviorProfileV0
        context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
        if (inbox.receiver_thread_id is not None
                and not inbox.wait_for_receiver(timeout=10.)):
            raise AssertionError(
                "motion receiver did not finish the virtual-tick control cycle"
            )
        return ()
