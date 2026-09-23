"""Bounded background isolation for B10 command search."""
from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
from queue import Empty, Full
import time

from mc2p.contracts.common import (
    ContractViolation, require_identifier, require_nonnegative_int,
)
from mc2p.motion_nav.motion_solver import (
    GapSolveRequest, SolveResult, solve_one_cell_gap,
)
from mc2p.motion_nav.online_motion import StateAnchor
from mc2p.motion_nav.physics_adapter import PhysicsWorldView


@dataclass(frozen=True, slots=True)
class GapMotionSolveJob:
    connection_id: str
    candidate_revision: int
    anchor: StateAnchor
    world: PhysicsWorldView
    request: GapSolveRequest

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "motion connection id")
        require_nonnegative_int(self.candidate_revision, "candidate revision")
        if (type(self.anchor) is not StateAnchor
                or type(self.world) is not PhysicsWorldView
                or type(self.request) is not GapSolveRequest):
            raise ContractViolation("gap motion job requires typed solve inputs")


@dataclass(frozen=True, slots=True)
class GapMotionSolveResult:
    connection_id: str
    candidate_revision: int
    solve_result: SolveResult
    elapsed_ns: int

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "motion connection id")
        require_nonnegative_int(self.candidate_revision, "candidate revision")
        if type(self.solve_result) is not SolveResult:
            raise ContractViolation("motion worker result requires a solve result")
        if type(self.elapsed_ns) is not int or self.elapsed_ns < 0:
            raise ContractViolation("motion worker elapsed time must be nonnegative")


def _worker(requests, results) -> None:
    while True:
        job = requests.get()
        if job is None:
            return
        started = time.perf_counter_ns()
        solved = solve_one_cell_gap(job.anchor, job.world, job.request)
        result = GapMotionSolveResult(
            job.connection_id, job.candidate_revision, solved,
            time.perf_counter_ns() - started,
        )
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
