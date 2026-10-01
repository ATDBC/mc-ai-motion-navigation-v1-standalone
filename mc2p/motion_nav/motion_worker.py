"""Bounded background isolation for B10 command search."""
from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
from queue import Empty, Full
import time
from typing import Protocol, runtime_checkable

from mc2p.contracts.common import (
    ContractViolation, require_identifier, require_nonnegative_int,
)
from mc2p.motion_nav.async_work import AsyncWorkIdentity
from mc2p.motion_nav.motion_solver import (
    AirTransitionSolveRequest, GapSolveRequest, SolveResult, SolveStatus,
    solve_air_transition, solve_one_cell_gap,
)
from mc2p.motion_nav.online_motion import StateAnchor
from mc2p.motion_nav.physics_adapter import PhysicsWorldView


@dataclass(frozen=True, slots=True)
class GapMotionSolveJob:
    connection_id: str
    candidate_revision: int
    anchor: StateAnchor
    world: PhysicsWorldView
    request: GapSolveRequest | AirTransitionSolveRequest
    work_identity: AsyncWorkIdentity | None = None

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


@runtime_checkable
class MotionWorkerPort(Protocol):
    """Non-blocking motion worker contract shared by process and inline tests."""

    def submit(self, job: GapMotionSolveJob) -> bool: ...
    def poll_available(self) -> tuple[GapMotionSolveResult, ...]: ...
    def close(self) -> None: ...
    def is_alive(self) -> bool: ...


def _execute_job(job: GapMotionSolveJob) -> GapMotionSolveResult:
    """Turn solver exceptions into a typed result without killing the worker."""
    if type(job) is not GapMotionSolveJob:
        raise ContractViolation("motion worker requires a typed job")
    started = time.perf_counter_ns()
    try:
        solved = (
            solve_one_cell_gap(job.anchor, job.world, job.request)
            if type(job.request) is GapSolveRequest else
            solve_air_transition(job.anchor, job.world, job.request)
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


def _worker(requests, results) -> None:
    while True:
        job = requests.get()
        if job is None:
            return
        result = _execute_job(job)
        # Backpressure stays in the worker.  The control thread only uses
        # non-blocking submit and poll operations.
        results.put(result)


class MotionSolverWorker:
    """One process and two fixed-capacity FIFO queues for motion solving."""

    def __init__(self, *, max_pending: int = 8) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 64:
            raise ContractViolation("motion worker capacity must be within 1..64")
        context = multiprocessing.get_context("spawn")
        self._requests = context.Queue(maxsize=max_pending)
        self._results = context.Queue(maxsize=max_pending)
        self._process = context.Process(
            target=_worker,
            args=(self._requests, self._results),
            name="mc2p-motion-solver",
            daemon=True,
        )
        self._process.start()
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def submit(self, job: GapMotionSolveJob) -> bool:
        if self._closed:
            raise ContractViolation("motion solver worker is closed")
        if type(job) is not GapMotionSolveJob:
            raise ContractViolation("motion worker requires a typed job")
        try:
            self._requests.put_nowait(job)
        except Full:
            return False
        return True

    def poll_available(self) -> tuple[GapMotionSolveResult, ...]:
        if self._closed:
            return ()
        available: list[GapMotionSolveResult] = []
        while True:
            try:
                available.append(self._results.get_nowait())
            except Empty:
                return tuple(available)

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
        for channel in (self._requests, self._results):
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
        self._results: dict[AsyncWorkIdentity, GapMotionSolveResult] = {}
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
        worker: MotionWorkerPort,
        observation_sequence: int,
    ) -> None:
        if not isinstance(worker, MotionWorkerPort):
            raise ContractViolation("motion inbox requires a worker")
        require_nonnegative_int(observation_sequence, "motion inbox sequence")
        if self._last_drain_sequence == observation_sequence:
            return
        self._last_drain_sequence = observation_sequence
        for result in worker.poll_available():
            identity = result.work_identity
            if identity is None:
                # Compatibility-only worker calls do not participate in the
                # shared formal routing contract.
                self.discarded_results += 1
                continue
            if identity not in self._active or identity in self._delivered:
                self.discarded_results += 1
                continue
            self._results[identity] = result
            self._delivered.add(identity)

    def take(self, identity: AsyncWorkIdentity) -> tuple[GapMotionSolveResult, ...]:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox take requires typed identity")
        result = self._results.pop(identity, None)
        return () if result is None else (result,)

    def retire(self, identity: AsyncWorkIdentity) -> None:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox retirement requires typed identity")
        queued = self.take(identity)
        self.discarded_results += len(queued)
        self._active.discard(identity)
        self._delivered.discard(identity)
