"""Bounded background isolation for B10 command search."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import multiprocessing
import os
from queue import Empty, Full
import time
from typing import Protocol, runtime_checkable

from mc2p.contracts.common import (
    ContractViolation, require_identifier, require_nonnegative_int,
)
from mc2p.motion_nav.async_work import AsyncWorkIdentity
from mc2p.motion_nav.ground_terminal_search import (
    GroundTerminalSearchResult, GroundTerminalSearchStatus,
    GroundTerminalSolveRequest, solve_ground_terminal_sequence,
)
from mc2p.motion_nav.motion_solver import (
    AirTransitionSolveRequest, GapSolveRequest, SolveResult, SolveStatus,
    MotionCommandTick, VerifiedMotionResult, revalidate_air_transition,
    solve_air_transition, solve_one_cell_gap,
)
from mc2p.motion_nav.online_motion import StateAnchor
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, PhysicsState,
)
from mc2p.motion_nav.world_model import WorldKnowledge, WorldSessionId
from mc2p.contracts.action_v1 import MovementV1


class MotionJobOperation(StrEnum):
    SOLVE = "solve"
    REVALIDATE = "revalidate"


class MotionWorkerReadiness(StrEnum):
    STARTING = "starting"
    READY = "ready"
    INITIALIZATION_FAILED = "initialization_failed"
    DEAD = "dead"
    CLOSED = "closed"


class MotionWorkerCancelStatus(StrEnum):
    ACCEPTED = "accepted"
    ALREADY_FINISHED = "already_finished"
    BACKPRESSURE = "backpressure"
    WORKER_UNAVAILABLE = "worker_unavailable"


@dataclass(frozen=True, slots=True)
class MotionWorkerHealth:
    readiness: MotionWorkerReadiness
    pid: int | None
    ready_monotonic_ns: int | None
    failed_monotonic_ns: int | None
    failure_type: str | None


_CONTROL_RECORD_TTL_NS = 2_000_000_000


@dataclass(frozen=True, slots=True)
class _LocalRetirement:
    status: GroundTerminalSearchStatus
    expires_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class _PendingCancellation:
    cancellation: "MotionWorkerCancellation"
    expires_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class GapMotionSolveJob:
    connection_id: str
    candidate_revision: int
    anchor: StateAnchor
    world: PhysicsWorldView
    request: GapSolveRequest | AirTransitionSolveRequest
    work_identity: AsyncWorkIdentity | None = None
    operation: MotionJobOperation = MotionJobOperation.SOLVE
    proof: VerifiedMotionResult | None = None
    entry_prefix: tuple[MotionCommandTick, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "motion connection id")
        require_nonnegative_int(self.candidate_revision, "candidate revision")
        if (type(self.anchor) is not StateAnchor
                or type(self.world) is not PhysicsWorldView
                or type(self.request) not in {
                    GapSolveRequest, AirTransitionSolveRequest}):
            raise ContractViolation("motion job requires typed solve inputs")
        if (self.work_identity is not None
                and type(self.work_identity) is not AsyncWorkIdentity):
            raise ContractViolation("motion job work identity must be typed")
        if type(self.operation) is not MotionJobOperation:
            raise ContractViolation("motion job operation must be typed")
        if (self.operation is MotionJobOperation.REVALIDATE) != (
                type(self.proof) is VerifiedMotionResult):
            raise ContractViolation("revalidation requires exactly one old proof")
        if (type(self.entry_prefix) is not tuple or len(self.entry_prefix) > 4
                or any(type(command) is not MotionCommandTick for command in self.entry_prefix)):
            raise ContractViolation("motion preparation prefix must be bounded")


@dataclass(frozen=True, slots=True)
class GapMotionSolveResult:
    connection_id: str
    candidate_revision: int
    solve_result: SolveResult
    elapsed_ns: int
    work_identity: AsyncWorkIdentity | None = None

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "motion connection id")
        require_nonnegative_int(self.candidate_revision, "candidate revision")
        if type(self.solve_result) is not SolveResult:
            raise ContractViolation("motion worker result requires a solve result")
        if type(self.elapsed_ns) is not int or self.elapsed_ns < 0:
            raise ContractViolation("motion worker elapsed time must be nonnegative")
        if (self.work_identity is not None
                and type(self.work_identity) is not AsyncWorkIdentity):
            raise ContractViolation("motion result work identity must be typed")


@dataclass(frozen=True, slots=True)
class GroundTerminalSolveJob:
    """One bounded ordinary-ground terminal search submitted to the motion worker."""

    connection_id: str
    candidate_revision: int
    request: GroundTerminalSolveRequest
    submitted_monotonic_ns: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "ground terminal connection id")
        require_nonnegative_int(self.candidate_revision, "candidate revision")
        if type(self.request) is not GroundTerminalSolveRequest:
            raise ContractViolation("ground terminal job requires a typed request")
        if (type(self.submitted_monotonic_ns) is not int
                or self.submitted_monotonic_ns < 0):
            raise ContractViolation("ground terminal submit timestamp is invalid")

    @property
    def work_identity(self) -> AsyncWorkIdentity:
        return self.request.work_identity


@dataclass(frozen=True, slots=True)
class GroundTerminalSolveResult:
    """Typed worker result kept distinct from every air-motion solve result."""

    connection_id: str
    candidate_revision: int
    search_result: GroundTerminalSearchResult
    elapsed_ns: int
    work_identity: AsyncWorkIdentity
    executor_pid: int
    dequeued_monotonic_ns: int = 0
    finished_monotonic_ns: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "ground terminal connection id")
        require_nonnegative_int(self.candidate_revision, "candidate revision")
        if (type(self.search_result) is not GroundTerminalSearchResult
                or type(self.elapsed_ns) is not int or self.elapsed_ns < 0
                or type(self.work_identity) is not AsyncWorkIdentity
                or type(self.executor_pid) is not int
                or self.executor_pid <= 0
                or type(self.dequeued_monotonic_ns) is not int
                or type(self.finished_monotonic_ns) is not int
                or self.dequeued_monotonic_ns < 0
                or self.finished_monotonic_ns < self.dequeued_monotonic_ns):
            raise ContractViolation("ground terminal worker result is malformed")


@dataclass(frozen=True, slots=True)
class MotionWorkerCancellation:
    work_identity: AsyncWorkIdentity
    status: GroundTerminalSearchStatus

    def __post_init__(self) -> None:
        if (type(self.work_identity) is not AsyncWorkIdentity
                or self.status not in {
                    GroundTerminalSearchStatus.CANCELLED,
                    GroundTerminalSearchStatus.STALE,
                    GroundTerminalSearchStatus.TIMEOUT,
                }):
            raise ContractViolation("motion worker cancellation is malformed")


MotionSolveJob = GapMotionSolveJob | GroundTerminalSolveJob
MotionSolveResult = GapMotionSolveResult | GroundTerminalSolveResult


@runtime_checkable
class MotionWorkerComputePort(Protocol):
    """Minimal compute port retained for deterministic standalone fixtures."""

    def submit(self, job: MotionSolveJob) -> bool: ...
    def poll_available(self) -> tuple[MotionSolveResult, ...]: ...
    def is_alive(self) -> bool: ...


@runtime_checkable
class MotionWorkerPort(MotionWorkerComputePort, Protocol):
    """Formal borrowed port; it deliberately has no close operation."""

    @property
    def health(self) -> MotionWorkerHealth: ...
    def cancel(
        self,
        identity: AsyncWorkIdentity,
        status: GroundTerminalSearchStatus = GroundTerminalSearchStatus.CANCELLED,
    ) -> MotionWorkerCancelStatus: ...


@runtime_checkable
class MotionWorkerOwner(MotionWorkerPort, Protocol):
    """Runtime-owned endpoint that alone may close the worker process."""

    def close(self) -> None: ...


def _execute_job(
    job: MotionSolveJob,
    *,
    stop_check=lambda: None,
    dequeued_monotonic_ns: int | None = None,
) -> MotionSolveResult:
    """Turn solver exceptions into a typed result without killing the worker."""
    if type(job) is GroundTerminalSolveJob:
        started = time.perf_counter_ns()
        try:
            solved = solve_ground_terminal_sequence(
                job.request, stop_check=stop_check,
            )
        except Exception as error:
            solved = GroundTerminalSearchResult(
                GroundTerminalSearchStatus.INTERNAL_ERROR,
                reasons=(type(error).__name__,),
            )
        finished = time.perf_counter_ns()
        return GroundTerminalSolveResult(
            job.connection_id, job.candidate_revision, solved,
            finished - started, job.work_identity, os.getpid(),
            started if dequeued_monotonic_ns is None else dequeued_monotonic_ns,
            finished,
        )
    if type(job) is not GapMotionSolveJob:
        raise ContractViolation("motion worker requires a typed job")
    started = time.perf_counter_ns()
    try:
        if job.operation is MotionJobOperation.REVALIDATE:
            solved = revalidate_air_transition(
                job.proof, job.anchor, job.world, job.request.execution_window,
                entry_prefix=job.entry_prefix,
                stop_check=stop_check,
            )
        elif job.entry_prefix:
            from mc2p.motion_nav.motion_solver import solve_prepared_air_transition
            solved = solve_prepared_air_transition(
                job.anchor, job.world, job.request, job.entry_prefix,
                stop_check=stop_check,
            )
        else:
            solved = (
                solve_one_cell_gap(
                    job.anchor, job.world, job.request,
                    stop_check=stop_check,
                )
                if type(job.request) is GapSolveRequest else
                solve_air_transition(
                    job.anchor, job.world, job.request,
                    stop_check=stop_check,
                )
            )
    except Exception as error:
        solved = SolveResult(
            SolveStatus.INTERNAL_ERROR,
            reasons=(type(error).__name__,),
        )
    return GapMotionSolveResult(
        job.connection_id, job.candidate_revision, solved,
        time.perf_counter_ns() - started,
        job.work_identity,
    )


def _prewarm_worker_calculator() -> None:
    """Exercise projection and the 1.21 calculator without publishing work."""
    session = WorldSessionId("motion-worker-prewarm")
    state = PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema,
        session, 1, (.5, 64.0, .5), (0.0, -0.0784, 0.0),
        0.0, 0.0, "standing", .6, 1.8, True, False, True,
        False, False, 0, 0.0, .1, .6, .08, .42, 20, 5.0,
        "survival", (), False, False, False, False, False, False, False,
    )
    projected = project_movement_command(state, MovementV1())
    if projected.status is not ProjectionStatus.READY or projected.tick_input is None:
        raise ContractViolation("motion worker prewarm projection failed")
    world = PhysicsWorldView(
        WorldKnowledge(session).view(), JAVA_1_21_RULESET,
    )
    result = physics_step(
        state, projected.tick_input, world, JAVA_1_21_RULESET,
    )
    if result.status not in {
            CalculationStatus.OK, CalculationStatus.NEEDS_WORLD}:
        raise ContractViolation("motion worker prewarm calculator failed")


def _worker(requests, results, controls, ready, startup) -> None:
    cancelled: dict[AsyncWorkIdentity, GroundTerminalSearchStatus] = {}
    try:
        # Import and instantiate the worker-owned immutable containers before
        # accepting formal work.  Python has no JIT warm-up; touching the
        # solver entry and monotonic clock removes lazy import/startup from the
        # first request without publishing a fabricated result.
        _prewarm_worker_calculator()
        time.perf_counter_ns()
        startup.put_nowait((MotionWorkerReadiness.READY, os.getpid(),
                            time.perf_counter_ns()))
        ready.set()
    except Exception as error:
        startup.put_nowait((MotionWorkerReadiness.INITIALIZATION_FAILED, os.getpid(),
                            type(error).__name__))
        ready.set()
        return

    def drain_controls() -> None:
        while True:
            try:
                item = controls.get_nowait()
            except Empty:
                return
            if type(item) is MotionWorkerCancellation:
                cancelled[item.work_identity] = item.status
                while len(cancelled) > 128:
                    cancelled.pop(next(iter(cancelled)))

    while True:
        job = requests.get()
        if job is None:
            return
        dequeued = time.perf_counter_ns()
        drain_controls()

        identity = getattr(job, "work_identity", None)
        cancelled_status = (
            cancelled.get(identity)
            if type(identity) is AsyncWorkIdentity else None
        )

        def stop_check():
            drain_controls()
            return (
                cancelled.get(identity)
                if type(identity) is AsyncWorkIdentity else None
            )

        result = (
            _cancelled_job_result(job, cancelled_status, dequeued)
            if cancelled_status is not None else
            _execute_job(
                job, stop_check=stop_check,
                dequeued_monotonic_ns=dequeued,
            )
        )
        # Close the race between the last solver checkpoint and result
        # publication.  A retired generation may never publish SOLVED.
        drain_controls()
        final_status = (
            cancelled.get(identity)
            if type(identity) is AsyncWorkIdentity else None
        )
        if final_status is not None:
            result = _cancelled_job_result(job, final_status, dequeued)
        if type(identity) is AsyncWorkIdentity:
            cancelled.pop(identity, None)
        # Backpressure stays in the worker.  The control thread only uses
        # non-blocking submit and poll operations.
        publish_deadline = time.monotonic() + 2.0
        while True:
            drain_controls()
            publish_status = (
                cancelled.get(identity)
                if type(identity) is AsyncWorkIdentity else None
            )
            if publish_status is not None:
                result = _cancelled_job_result(
                    job, publish_status, dequeued,
                )
            try:
                results.put_nowait(result)
                if type(identity) is AsyncWorkIdentity:
                    cancelled.pop(identity, None)
                break
            except Full:
                if time.monotonic() >= publish_deadline:
                    return
                time.sleep(.001)


def _cancelled_job_result(
    job: MotionSolveJob,
    status: GroundTerminalSearchStatus,
    dequeued_monotonic_ns: int,
) -> MotionSolveResult:
    """Build the typed terminal result for work retired before publication."""
    finished = time.perf_counter_ns()
    if type(job) is GroundTerminalSolveJob:
        return GroundTerminalSolveResult(
            job.connection_id,
            job.candidate_revision,
            GroundTerminalSearchResult(status, reasons=(f"worker_{status.value}",)),
            max(0, finished - dequeued_monotonic_ns),
            job.work_identity,
            os.getpid(),
            dequeued_monotonic_ns,
            finished,
        )
    assert type(job) is GapMotionSolveJob
    solve_status = {
        GroundTerminalSearchStatus.CANCELLED: SolveStatus.CANCELLED,
        GroundTerminalSearchStatus.STALE: SolveStatus.STALE,
        GroundTerminalSearchStatus.TIMEOUT: SolveStatus.TIMEOUT,
    }[status]
    return GapMotionSolveResult(
        job.connection_id,
        job.candidate_revision,
        SolveResult(solve_status, reasons=(f"worker_{status.value}",)),
        max(0, finished - dequeued_monotonic_ns),
        job.work_identity,
    )


def _retire_published_result(
    result: MotionSolveResult,
    status: GroundTerminalSearchStatus,
) -> MotionSolveResult:
    """Apply the owner-side retirement gate even if IPC cancellation raced."""
    if type(result) is GroundTerminalSolveResult:
        return GroundTerminalSolveResult(
            result.connection_id,
            result.candidate_revision,
            GroundTerminalSearchResult(
                status, reasons=(f"worker_{status.value}",),
            ),
            result.elapsed_ns,
            result.work_identity,
            result.executor_pid,
            result.dequeued_monotonic_ns,
            result.finished_monotonic_ns,
        )
    solve_status = {
        GroundTerminalSearchStatus.CANCELLED: SolveStatus.CANCELLED,
        GroundTerminalSearchStatus.STALE: SolveStatus.STALE,
        GroundTerminalSearchStatus.TIMEOUT: SolveStatus.TIMEOUT,
    }[status]
    return GapMotionSolveResult(
        result.connection_id,
        result.candidate_revision,
        SolveResult(solve_status, reasons=(f"worker_{status.value}",)),
        result.elapsed_ns,
        result.work_identity,
    )


class MotionSolverWorker:
    """One process and two fixed-capacity FIFO queues for motion solving."""

    def __init__(self, *, max_pending: int = 8) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 64:
            raise ContractViolation("motion worker capacity must be within 1..64")
        context = multiprocessing.get_context("spawn")
        self._requests = context.Queue(maxsize=max_pending)
        self._results = context.Queue(maxsize=max_pending)
        self._controls = context.Queue(maxsize=max_pending * 2)
        self._ready_event = context.Event()
        self._startup = context.Queue(maxsize=1)
        self._process = context.Process(
            target=_worker,
            args=(self._requests, self._results, self._controls,
                  self._ready_event, self._startup),
            name="mc2p-motion-solver",
            daemon=True,
        )
        self._process.start()
        self._closed = False
        self._readiness = MotionWorkerReadiness.STARTING
        self._ready_pid: int | None = None
        self._ready_monotonic_ns: int | None = None
        self._failed_monotonic_ns: int | None = None
        self._failure_type: str | None = None
        self._active_identities: set[AsyncWorkIdentity] = set()
        self._finished_identities: set[AsyncWorkIdentity] = set()
        self._locally_retired: dict[
            AsyncWorkIdentity, _LocalRetirement
        ] = {}
        self._pending_controls: dict[
            AsyncWorkIdentity, _PendingCancellation
        ] = {}
        if not self.wait_until_ready(5.0):
            self._refresh_readiness()
            if self._readiness is MotionWorkerReadiness.STARTING:
                self._readiness = MotionWorkerReadiness.INITIALIZATION_FAILED
                self._failure_type = "ready_timeout"
                self._failed_monotonic_ns = time.perf_counter_ns()

    @property
    def readiness(self) -> MotionWorkerReadiness:
        if self._closed:
            return MotionWorkerReadiness.CLOSED
        self._refresh_readiness()
        if not self._process.is_alive() and self._readiness not in {
                MotionWorkerReadiness.INITIALIZATION_FAILED,
                MotionWorkerReadiness.CLOSED}:
            self._readiness = MotionWorkerReadiness.DEAD
            if self._failed_monotonic_ns is None:
                self._failed_monotonic_ns = time.perf_counter_ns()
        return self._readiness

    @property
    def ready_monotonic_ns(self) -> int | None:
        self._refresh_readiness()
        return self._ready_monotonic_ns

    def _refresh_readiness(self) -> None:
        if self._readiness is not MotionWorkerReadiness.STARTING:
            return
        try:
            status, pid, value = self._startup.get_nowait()
        except Empty:
            return
        self._readiness = status
        if status is MotionWorkerReadiness.READY:
            self._ready_pid = pid
            self._ready_monotonic_ns = value
        else:
            self._failure_type = str(value)
            self._failed_monotonic_ns = time.perf_counter_ns()

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        if type(timeout) not in (int, float) or timeout < 0:
            raise ContractViolation("motion worker ready timeout is invalid")
        if not self._ready_event.wait(float(timeout)):
            self._refresh_readiness()
            return False
        self._refresh_readiness()
        return self._readiness is MotionWorkerReadiness.READY

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def health(self) -> MotionWorkerHealth:
        readiness = self.readiness
        return MotionWorkerHealth(
            readiness,
            self._ready_pid if self._ready_pid is not None else self._process.pid,
            self._ready_monotonic_ns,
            self._failed_monotonic_ns,
            self._failure_type,
        )

    @property
    def failure_type(self) -> str | None:
        self._refresh_readiness()
        return self._failure_type

    @property
    def failed_monotonic_ns(self) -> int | None:
        self._refresh_readiness()
        return self._failed_monotonic_ns

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def submit(self, job: MotionSolveJob) -> bool:
        if self._closed:
            raise ContractViolation("motion solver worker is closed")
        if type(job) not in {GapMotionSolveJob, GroundTerminalSolveJob}:
            raise ContractViolation("motion worker requires a typed job")
        if not self.wait_until_ready(0.0):
            return False
        self._flush_pending_controls()
        try:
            self._requests.put_nowait(job)
        except Full:
            return False
        identity = getattr(job, "work_identity", None)
        if type(identity) is AsyncWorkIdentity:
            self._active_identities.add(identity)
        return True

    def cancel(
        self,
        identity: AsyncWorkIdentity,
        status: GroundTerminalSearchStatus = GroundTerminalSearchStatus.CANCELLED,
    ) -> MotionWorkerCancelStatus:
        if self._closed or self.readiness is not MotionWorkerReadiness.READY:
            return MotionWorkerCancelStatus.WORKER_UNAVAILABLE
        if identity in self._finished_identities:
            return MotionWorkerCancelStatus.ALREADY_FINISHED
        now = time.perf_counter_ns()
        self._prune_control_records(now)
        item = MotionWorkerCancellation(identity, status)
        expires = now + _CONTROL_RECORD_TTL_NS
        self._locally_retired[identity] = _LocalRetirement(status, expires)
        while len(self._locally_retired) > 128:
            self._locally_retired.pop(next(iter(self._locally_retired)))
        try:
            self._controls.put_nowait(item)
        except Full:
            if len(self._pending_controls) < 128:
                self._pending_controls[identity] = _PendingCancellation(
                    item, expires,
                )
            return MotionWorkerCancelStatus.BACKPRESSURE
        return MotionWorkerCancelStatus.ACCEPTED

    def _prune_control_records(self, now: int | None = None) -> None:
        current = time.perf_counter_ns() if now is None else now
        for identity, record in tuple(self._pending_controls.items()):
            if current >= record.expires_monotonic_ns:
                self._pending_controls.pop(identity, None)
        for identity, record in tuple(self._locally_retired.items()):
            if current >= record.expires_monotonic_ns:
                self._locally_retired.pop(identity, None)

    def _flush_pending_controls(self) -> None:
        self._prune_control_records()
        for identity, pending in tuple(self._pending_controls.items()):
            try:
                self._controls.put_nowait(pending.cancellation)
            except Full:
                return
            self._pending_controls.pop(identity, None)

    def poll_available(self) -> tuple[MotionSolveResult, ...]:
        if self._closed:
            return ()
        self._flush_pending_controls()
        available: list[MotionSolveResult] = []
        while True:
            try:
                result = self._results.get_nowait()
            except Empty:
                return tuple(available)
            identity = getattr(result, "work_identity", None)
            if type(identity) is AsyncWorkIdentity:
                self._active_identities.discard(identity)
                self._finished_identities.add(identity)
                while len(self._finished_identities) > 128:
                    self._finished_identities.pop()
                retirement = self._locally_retired.pop(identity, None)
                self._pending_controls.pop(identity, None)
                if retirement is not None:
                    result = _retire_published_result(
                        result, retirement.status,
                    )
            available.append(result)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.is_alive():
            try:
                self._requests.put(None, timeout=.25)
            except Full:
                pass
            self._process.join(2.0)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(2.0)
        for channel in (self._requests, self._results, self._controls,
                        self._startup):
            channel.close()
            channel.cancel_join_thread()

    def __enter__(self) -> MotionSolverWorker:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class MotionResultInbox:
    """Drain one shared worker once per frame and route typed results by owner."""

    def __init__(self, *, max_results: int = 64) -> None:
        if type(max_results) is not int or not 1 <= max_results <= 512:
            raise ContractViolation("motion result inbox capacity is invalid")
        self._max_results = max_results
        self._results: dict[AsyncWorkIdentity, MotionSolveResult] = {}
        self._active: set[AsyncWorkIdentity] = set()
        self._delivered: set[AsyncWorkIdentity] = set()
        self._last_drain_sequence: int | None = None
        self.discarded_results = 0

    def register(self, identity: AsyncWorkIdentity) -> bool:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox registration requires typed identity")
        if identity in self._active:
            return True
        if len(self._active) >= self._max_results:
            return False
        self._active.add(identity)
        return True

    @property
    def active_identities(self) -> tuple[AsyncWorkIdentity, ...]:
        return tuple(sorted(self._active, key=lambda identity: identity.key))

    def drain_once(
        self,
        worker: MotionWorkerComputePort,
        observation_sequence: int,
    ) -> tuple[MotionSolveResult, ...]:
        if not isinstance(worker, MotionWorkerComputePort):
            raise ContractViolation("motion inbox requires a worker")
        require_nonnegative_int(observation_sequence, "motion inbox sequence")
        if self._last_drain_sequence == observation_sequence:
            return ()
        self._last_drain_sequence = observation_sequence
        discarded = []
        for result in worker.poll_available():
            identity = result.work_identity
            if identity is None:
                self.discarded_results += 1
                discarded.append(result)
                continue
            if identity not in self._active or identity in self._delivered:
                self.discarded_results += 1
                discarded.append(result)
                continue
            self._results[identity] = result
            self._delivered.add(identity)
        return tuple(discarded)

    def take(self, identity: AsyncWorkIdentity) -> tuple[MotionSolveResult, ...]:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox take requires typed identity")
        result = self._results.pop(identity, None)
        return () if result is None else (result,)

    def peek(self, identity: AsyncWorkIdentity) -> MotionSolveResult | None:
        """Keep an early result bounded in this inbox until its real entry tick."""
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox peek requires typed identity")
        return self._results.get(identity)

    def retire(self, identity: AsyncWorkIdentity) -> None:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox retirement requires typed identity")
        queued = self.take(identity)
        self.discarded_results += len(queued)
        self._active.discard(identity)
        self._delivered.discard(identity)
