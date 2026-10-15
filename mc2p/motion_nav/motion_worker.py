"""Bounded background isolation for B10 command search."""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from enum import StrEnum
import errno
import math
import multiprocessing
import pickle
from queue import Empty, Full
import sys
import threading
import time
from typing import Protocol, runtime_checkable
import weakref
from uuid import uuid4

from mc2p.contracts.action_v1 import ActionSnapshotV1
from mc2p.contracts.common import (
    ContractViolation, require_finite, require_identifier, require_nonnegative_int,
)
from mc2p.contracts.motion_activation import (
    ActivationRejectReason, ActivationResult, ActivationStatus, PreparedActivationAcceptance,
    PreparedActivationDelivery, PreparedActivationTransaction, StreamActivationBinding,
)
from mc2p.motion_nav.async_work import AsyncWorkIdentity
from mc2p.motion_nav.body_control import HandoffDisposition, HandoffEvidence
from mc2p.motion_nav.motion_solver import (
    AirTransitionSolveRequest, GapSolveRequest, SolveResult, SolveStatus,
    MotionCommandTick, VerifiedMotionResult, revalidate_air_transition,
    solve_air_transition, solve_one_cell_gap, prepare_fixed_motion,
)
from mc2p.motion_nav.online_motion import (
    InputApplicationStatus, ProjectionStatus, StateAnchor, project_movement_command,
)
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsRuleset
from mc2p.motion_nav.prepared_motion import (
    PreparedAdmissionResult, PreparedAdmissionStatus, PreparedComputeCounts, PreparedComputeResult,
    PreparedComputeStatus, PreparedCurrentFacts, PreparedMotionBundle, PreparedMotionHandle,
    PreparedMotionRequest, PreparedQueryResult, PreparedQueryStatus, PreparedRejectReason,
    PreparedSubmitResult, PreparedSubmitStatus, SnapshotLease, SnapshotLeaseResult, SnapshotLeaseStatus,
    MotionCloseReport, ProofActionExitEvidence, ProofConsumerIdentity, ProofReleaseEvidence,
    ProofSupervisorReleaseEvidence, ProofUse,
    ReferenceRejectReason, ReferenceResult, ReferenceStatus,
    PreparedActionPreparation,
)
from mc2p.motion_nav.prepared_motion_proof import (
    MAXIMUM_MESSAGE_BYTES, PreparedMessageBudgetExceeded, PreparedWireMessage,
    canonical_motion_bytes, canonical_motion_digest, seal_bundle, seal_compute_failure,
)
from mc2p.motion_nav import prepared_motion_validation
from mc2p.motion_nav.world_snapshot import DependencyStatus, SealedSnapshot, seal_snapshot_version
from mc2p.motion_nav.world_model import WorldView


class MotionJobOperation(StrEnum):
    SOLVE = "solve"
    REVALIDATE = "revalidate"
    PREPARE = "prepare"


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
        if self.operation is MotionJobOperation.PREPARE:
            raise ContractViolation("PREPARE requires a fixed request and sealed snapshot job")
        if (self.operation is MotionJobOperation.REVALIDATE) != (
                type(self.proof) is VerifiedMotionResult):
            raise ContractViolation("revalidation requires exactly one old proof")
        if (type(self.entry_prefix) is not tuple or len(self.entry_prefix) > 4
                or any(type(command) is not MotionCommandTick for command in self.entry_prefix)):
            raise ContractViolation("motion preparation prefix must be bounded")


@dataclass(frozen=True, slots=True)
class PreparedMotionJob:
    request: PreparedMotionRequest
    snapshot: SealedSnapshot

    @property
    def operation(self) -> MotionJobOperation:
        return MotionJobOperation.PREPARE

    def __post_init__(self) -> None:
        if type(self.request) is not PreparedMotionRequest or type(self.snapshot) is not SealedSnapshot:
            raise ContractViolation("PREPARE requires an immutable fixed request and sealed snapshot")
        if self.request.snapshot_identity != self.snapshot.identity:
            raise ContractViolation("PREPARE job substituted its request snapshot")


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

    def submit(self, job: GapMotionSolveJob | PreparedMotionJob) -> bool: ...
    def poll_available(self) -> tuple[GapMotionSolveResult | PreparedWireMessage, ...]: ...
    def close(self) -> None: ...
    def is_alive(self) -> bool: ...


def _execute_job(job: GapMotionSolveJob | PreparedMotionJob) -> GapMotionSolveResult | PreparedWireMessage:
    """Turn solver exceptions into a typed result without killing the worker."""
    if type(job) is PreparedMotionJob:
        prepared = None
        try:
            prepared = prepare_fixed_motion(job.request, job.snapshot)
            return (seal_bundle(prepared.bundle) if prepared.status is PreparedComputeStatus.PREPARED
                    else seal_compute_failure(prepared))
        except PreparedMessageBudgetExceeded:
            counts = PreparedComputeCounts() if prepared is None else prepared.counts
            status = (PreparedComputeStatus.BUDGET_EXHAUSTED
                      if prepared is not None and prepared.status is PreparedComputeStatus.PREPARED
                      else PreparedComputeStatus.WORKER_FAILED)
            return seal_compute_failure(PreparedComputeResult(status, job.request.job_token, counts))
        except Exception:
            counts = PreparedComputeCounts() if prepared is None else prepared.counts
            return seal_compute_failure(PreparedComputeResult(
                PreparedComputeStatus.WORKER_FAILED, job.request.job_token, counts))
    if type(job) is not GapMotionSolveJob:
        raise ContractViolation("motion worker requires a typed job")
    started = time.perf_counter_ns()
    try:
        if job.operation is MotionJobOperation.REVALIDATE:
            solved = revalidate_air_transition(
                job.proof, job.anchor, job.world, job.request.execution_window,
                entry_prefix=job.entry_prefix,
            )
        elif job.entry_prefix:
            from mc2p.motion_nav.motion_solver import solve_prepared_air_transition
            solved = solve_prepared_air_transition(
                job.anchor, job.world, job.request, job.entry_prefix,
            )
        else:
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

    def __init__(self, *, max_pending: int = 2) -> None:
        if type(max_pending) is not int or not 1 <= max_pending <= 64:
            raise ContractViolation("motion worker capacity must be within 1..64")
        self._closed = False
        self._slot_lock = threading.Lock()
        self._unfinished_count = 0
        self._expected_close_feeder_errors = deque(maxlen=64)
        self._unexpected_feeder_errors = deque(maxlen=64)
        self._request_feeder_error_original = None
        self._request_feeder_error_handler = None
        self._requests = self._results = self._process = None
        context = multiprocessing.get_context("spawn")
        try:
            self._requests = context.Queue(maxsize=min(max_pending, 2))
            original_error_handler = self._requests._on_queue_feeder_error
            owner_ref = weakref.ref(self)
            def feeder_error(error, value):
                owner = owner_ref()
                if owner is None:
                    original_error_handler(error, value)
                    return
                diagnostic = (type(error).__name__, getattr(error, "errno", None), getattr(error, "winerror", None))
                if (owner._closed and isinstance(error, OSError)
                        and (getattr(error, "errno", None) == errno.EPIPE
                             or getattr(error, "winerror", None) == 109)):
                    owner._expected_close_feeder_errors.append(diagnostic)
                    return  # Closing a dead worker's read end intentionally wakes this feeder.
                owner._unexpected_feeder_errors.append(diagnostic)
                original_error_handler(error, value)
            self._request_feeder_error_original = original_error_handler
            self._request_feeder_error_handler = feeder_error
            self._requests._on_queue_feeder_error = feeder_error
            self._results = context.Queue(maxsize=min(max_pending, 2))
            self._process = context.Process(
                target=_worker,
                args=(self._requests, self._results),
                name="mc2p-motion-solver",
                daemon=True,
            )
            self._process.start()
        except BaseException:
            # A partially started child still belongs to this constructor.
            self.close(timeout_seconds=5.)
            raise

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def is_alive(self) -> bool:
        return bool(self._process is not None and self._process.is_alive())

    @property
    def exit_code(self) -> int | None:
        code = None if self._process is None else self._process.exitcode
        return code if type(code) is int else None

    @property
    def live_queue_feeders(self) -> int:
        return sum(thread is not None and thread.is_alive()
                   for channel in (self._requests, self._results) if channel is not None
                   for thread in (getattr(channel, "_thread", None),)
                   if isinstance(thread, threading.Thread))

    def submit(self, job: GapMotionSolveJob | PreparedMotionJob) -> bool:
        if self._closed:
            raise ContractViolation("motion solver worker is closed")
        if type(job) not in {GapMotionSolveJob, PreparedMotionJob}:
            raise ContractViolation("motion worker requires a typed job")
        if not self._slot_lock.acquire(False):
            return False
        try:
            if self._unfinished_count >= 2:
                return False
            try:
                self._requests.put_nowait(job)
            except Full:
                return False
            self._unfinished_count += 1
        finally:
            self._slot_lock.release()
        return True

    def poll_available(self) -> tuple[GapMotionSolveResult | PreparedWireMessage, ...]:
        if self._closed:
            return ()
        available: list[GapMotionSolveResult | PreparedWireMessage] = []
        while True:
            try:
                available.append(self._results.get_nowait())
            except Empty:
                with self._slot_lock:
                    self._unfinished_count -= len(available)
                return tuple(available)

    def close(self, *, timeout_seconds: float = 5.) -> MotionCloseReport:
        deadline = _close_deadline(timeout_seconds)
        first_close = not self._closed
        self._closed = True
        process = self._process
        if process is not None and process.pid is not None:
            if process.is_alive() and first_close:
                try:
                    self._requests.put(None, timeout=min(.25, _remaining(deadline)))
                except (Full, ValueError, OSError):
                    pass
                process.join(min(2., _remaining(deadline) / 2))
            if process.is_alive():
                process.terminate()
                process.join(_remaining(deadline))
            else:
                process.join(0)
        for channel in (self._requests, self._results):
            if channel is None:
                continue
            no_child = process is None or process.pid is None or not process.is_alive()
            feeder = getattr(channel, "_thread", None)
            has_feeder = isinstance(feeder, threading.Thread)
            posix_feeder_owns_ends = sys.platform != "win32" and no_child and has_feeder
            channel.cancel_join_thread()
            if first_close and posix_feeder_owns_ends:
                # A dead child cannot drain queued bytes. Close only the read
                # end before publishing Queue's sentinel so a blocked POSIX
                # feeder wakes with the expected EPIPE. The feeder remains
                # the sole owner of the writer while it is alive.
                reader = getattr(channel, "_reader", None)
                if reader is not None:
                    reader.close()
            channel.close()
            feeder_owns_ends = sys.platform == "win32" and no_child and has_feeder
            if no_child and not has_feeder:
                # No child can consume queued bytes now. Closing the parent
                # endpoints is safe because no feeder can race their handles.
                for name in ("_reader", "_writer"):
                    endpoint = getattr(channel, name, None)
                    if endpoint is not None:
                        endpoint.close()
            if has_feeder and feeder is not threading.current_thread():
                _join_queue_feeder(feeder, getattr(channel, "_writer", None), deadline)
            if posix_feeder_owns_ends and not feeder.is_alive():
                # Connection.close() is idempotent. The feeder normally
                # closed both ends in its finally block; this only completes
                # a partially initialized or exceptionally exited feeder.
                for name in ("_reader", "_writer"):
                    endpoint = getattr(channel, name, None)
                    if endpoint is not None:
                        endpoint.close()
            if feeder_owns_ends and feeder.is_alive():
                # The original deadline is exhausted. Retain actual liveness
                # in the report; pipe close is only best-effort cleanup now.
                for name in ("_reader", "_writer"):
                    endpoint = getattr(channel, name, None)
                    if endpoint is not None:
                        try:
                            endpoint.close()
                        except OSError:
                            pass
            self._restore_request_feeder_error_handler(channel, feeder)
        alive, feeders = int(self.is_alive()), self.live_queue_feeders
        return MotionCloseReport(worker_exit_code=self.exit_code, live_workers=alive,
            live_queue_feeders=feeders, timed_out=bool(alive or feeders),
            expected_close_feeder_errors=len(getattr(self, "_expected_close_feeder_errors", ())),
            unexpected_feeder_errors=len(getattr(self, "_unexpected_feeder_errors", ())))

    def _restore_request_feeder_error_handler(self, channel, feeder) -> None:
        if channel is not self._requests:
            return
        installed = getattr(self, "_request_feeder_error_handler", None)
        original = getattr(self, "_request_feeder_error_original", None)
        if installed is None or original is None:
            return
        if isinstance(feeder, threading.Thread) and feeder.is_alive():
            return
        if getattr(channel, "_on_queue_feeder_error", None) is installed:
            channel._on_queue_feeder_error = original
        self._request_feeder_error_handler = None

    def __enter__(self) -> MotionSolverWorker:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def _close_deadline(timeout_seconds: float) -> float:
    require_finite(timeout_seconds, "motion close timeout")
    if type(timeout_seconds) not in (int, float) or timeout_seconds < 0:
        raise ContractViolation("motion close timeout must be nonnegative")
    return time.monotonic() + min(timeout_seconds, 5.)


def _remaining(deadline: float) -> float:
    return max(0., deadline - time.monotonic())


def _join_queue_feeder(feeder: threading.Thread, writer, deadline: float) -> None:
    if sys.platform != "win32":
        feeder.join(_remaining(deadline))
        return
    # PipeConnection.close() can run between WriteFile and publication of
    # _send_ov. Its first cancellation then misses the new overlapped write.
    # Re-read that existing operation after each short bounded join; cancel
    # the operation, never the Python thread or its reported liveness.
    while feeder.is_alive():
        feeder.join(min(.01, _remaining(deadline)))
        if not feeder.is_alive():
            return
        pending = None if writer is None else getattr(writer, "_send_ov", None)
        if pending is not None:
            try:
                pending.cancel()
            except (OSError, ValueError):
                pass  # Keep the feeder live in the report if it cannot exit.
        if not _remaining(deadline):
            return


@dataclass(slots=True)
class _MotionAdmissionRecord:
    """The existing active computation record, also used during snapshot work."""
    identity: AsyncWorkIdentity
    request: PreparedMotionRequest | None = None
    lease: SnapshotLease | None = None
    world_port: object | None = None
    bounds: PhysicsWorldBounds | None = None
    ruleset: PhysicsRuleset | None = None
    snapshot_result: SnapshotLeaseResult | None = None
    expected_inputs: tuple | None = None
    expected_digest: str | None = None
    canonical_request: bytes | None = None
    sealed_request: bytes | None = None
    reserved_bytes: int = 0
    snapshot_pending: bool = False
    submitted: bool = False
    processing: bool = False
    finished: bool = False
    retired: bool = False
    admission: PreparedAdmissionResult | None = None
    computation: PreparedComputeResult | None = None
    submission: PreparedSubmitResult | None = None
    action_preparation: PreparedActionPreparation | None = None
    action_selection: tuple[StateAnchor, GapSolveRequest | AirTransitionSolveRequest] | None = None


@dataclass(slots=True)
class _PreparedRegistration:
    identity: AsyncWorkIdentity
    job_token: str
    bundle: PreparedMotionBundle
    handle: PreparedMotionHandle
    lease: SnapshotLease
    world_port: object | None
    ready_query: PreparedQueryResult
    admission: PreparedAdmissionResult
    candidate: bool = True
    executing: ProofConsumerIdentity | None = None
    safe_tail: ProofConsumerIdentity | None = None
    transaction: PreparedActivationTransaction | None = None
    acceptance: PreparedActivationAcceptance | None = None
    handoff: HandoffEvidence | None = None
    binding: StreamActivationBinding | None = None
    commands: tuple[ActionSnapshotV1, ...] = ()
    required_damage_points: float = 0.
    maximum_commands: int = 0


class MotionResultInbox:
    """One receiver, bounded computation records and one proof registry.

    Control methods publish/reference bounded metadata. The receiver alone
    polls the shared worker, seals requests/snapshots, decodes messages,
    performs full validation and publishes registered handles.
    """

    MAX_UNFINISHED = 2
    MAX_BUNDLES = 8
    MAX_LEASES = 8
    MAX_BYTES = 16 * 1024 * 1024
    MAX_HISTORY = 64
    _consumer_lock = threading.Lock()
    _consumers = weakref.WeakKeyDictionary()
    _NOT_READY = PreparedQueryResult(PreparedQueryStatus.NOT_READY)
    _OVERDUE = PreparedQueryResult(PreparedQueryStatus.OVERDUE)

    def __init__(self, *, max_results: int = 64, worker: MotionWorkerPort | None = None,
                 world=None, current_facts: PreparedCurrentFacts | None = None) -> None:
        if type(max_results) is not int or not 1 <= max_results <= 512:
            raise ContractViolation("motion result inbox capacity is invalid")
        if current_facts is not None and type(current_facts) is not PreparedCurrentFacts:
            raise ContractViolation("prepared current facts require the owner's immutable projection")
        self._max_results = max_results
        self._lock = threading.Lock()
        self._cycle_condition = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._receiver: threading.Thread | None = None
        self._lease_returner: threading.Thread | None = None
        self._lease_return_condition = threading.Condition(self._lock)
        self._lease_return_generation = 0
        self._worker: MotionWorkerPort | None = None
        self._world = world
        self._current_facts = current_facts
        self._action_preparation_facts: PreparedActionPreparation | None = None
        self._closed = False
        self._close_worker_requested = False
        self._shutdown_deadline = 0.
        self._worker_close_report: MotionCloseReport | None = None
        self._worker_close_attempted = False
        self._cycle = 0
        self._requested_cycle = 0
        self._active: dict[AsyncWorkIdentity, _MotionAdmissionRecord] = {}
        self._results: dict[AsyncWorkIdentity, GapMotionSolveResult] = {}
        self._discarded = deque(maxlen=self.MAX_HISTORY)
        self._history: dict[AsyncWorkIdentity, _MotionAdmissionRecord] = {}
        self._token_history: deque[str] = deque(maxlen=self.MAX_HISTORY)
        self._registry: dict[str, _PreparedRegistration] = {}
        self._leases: dict[str, tuple[SnapshotLease, object | None, int]] = {}
        self._bytes: dict[int, tuple[bytes, int]] = {}
        self._reserved_bytes = 0
        self._validation_bytes = 0
        self._releases = deque()
        self._last_drain_sequence: int | None = None
        # Read-only timing evidence.  These rows never participate in
        # admission, activation or retry decisions and are bounded like the
        # other diagnostic histories owned by this inbox.
        self._receiver_diagnostics = deque(maxlen=4096)
        self._diagnostic_consumed = deque(maxlen=self.MAX_HISTORY)
        self._receiver_diagnostics_enabled = False
        self._diagnostic_cycle = 0
        self._diagnostic_movement_tick: int | None = None
        self.discarded_results = 0
        if worker is not None:
            try:
                self.attach_worker(worker)
            except BaseException:
                self.close(close_worker=False)
                raise

    @property
    def receiver_thread_id(self) -> int | None:
        return None if self._receiver is None else self._receiver.ident

    def publish_diagnostic_movement_tick(self, movement_tick: int) -> None:
        """Publish a read-only clock label for receiver evidence.

        The label is never read by admission or execution decisions.  Formal
        probes call this before a control decision so background publication
        can be related to Minecraft movement ticks without adding a barrier.
        """
        require_nonnegative_int(movement_tick, "receiver diagnostic movement tick")
        with self._lock:
            self._receiver_diagnostics_enabled = True
            self._diagnostic_movement_tick = movement_tick

    def _diagnostic_tick_locked(self) -> int | None:
        current = self._current_facts
        return (
            current.movement_tick
            if current is not None else self._diagnostic_movement_tick
        )

    def _mark_diagnostic_consumed_locked(self, identity: AsyncWorkIdentity) -> bool:
        """Remember one first-consume row without retaining identities forever."""
        if identity in self._diagnostic_consumed:
            return False
        self._diagnostic_consumed.append(identity)
        return True

    def attach_worker(self, worker: MotionWorkerPort) -> None:
        if not isinstance(worker, MotionWorkerPort):
            raise ContractViolation("motion inbox requires a worker")
        with self._lock:
            if self._closed:
                return
            if self._worker is worker:
                return
            if self._worker is not None:
                raise ContractViolation("one motion service cannot change its result source")
        with self._consumer_lock:
            existing = self._consumers.get(worker)
            owner = None if existing is None else existing()
            if owner is not None and owner is not self:
                raise ContractViolation("a shared motion worker has exactly one result receiver")
            self._consumers[worker] = weakref.ref(self)
        with self._lock:
            self._worker = worker
        self._start_receiver()

    def _start_receiver(self) -> None:
        with self._lock:
            if self._receiver is not None or self._closed:
                return
            self._receiver = threading.Thread(target=self._receive, name="mc2p-motion-receiver", daemon=True)
            self._receiver.start()
        self._wake.set()

    def bind_world(self, world) -> None:
        """Borrow a new formal port; existing leases keep their original port."""
        with self._lock:
            self._world = world

    def publish_current_facts(self, facts: PreparedCurrentFacts) -> None:
        if type(facts) is not PreparedCurrentFacts:
            raise ContractViolation("prepared facts must be the owner's immutable typed projection")
        with self._lock:
            self._current_facts = facts
            # This is candidate eligibility only. Existing body uses outlive
            # both the old computation scope and the next preparation stream.
            for record in tuple(self._registry.values()):
                if record.candidate and (record.identity.scope != facts.scope
                        or record.handle.hot_view.stream != facts.stream
                        or record.handle.owner_id != facts.body_owner_id):
                    self._release_candidate_locked(record)
        self._wake.set()

    def _unfinished_locked(self) -> int:
        # Retired computations still in the worker retain their service slot.
        return sum(not record.finished for record in self._active.values())

    def _byte_count_locked(self) -> int:
        return sum(len(value) for value, _ in self._bytes.values()) + self._reserved_bytes + self._validation_bytes

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._byte_count_locked()

    def _retain_bytes_locked(self, value: bytes) -> bool:
        stored = self._bytes.get(id(value))
        if stored is not None:
            self._bytes[id(value)] = (value, stored[1] + 1)
            return True
        if self._byte_count_locked() + len(value) > self.MAX_BYTES:
            return False
        self._bytes[id(value)] = (value, 1)
        return True

    def _release_bytes_locked(self, value: bytes | None) -> None:
        if value is None:
            return
        stored = self._bytes[id(value)]
        if stored[1] == 1:
            del self._bytes[id(value)]
        else:
            self._bytes[id(value)] = (value, stored[1] - 1)

    def _retain_lease_locked(self, lease: SnapshotLease, port) -> bool:
        stored = self._leases.get(lease.lease_token)
        if stored is not None:
            if stored[0] is not lease:
                raise ContractViolation("a snapshot lease token cannot identify a copied or foreign lease")
            if stored[2] == 0:
                return False  # The original world port is already returning it.
            self._leases[lease.lease_token] = (lease, stored[1], stored[2] + 1)
            return True
        if len(self._leases) >= self.MAX_LEASES or not self._retain_bytes_locked(lease.canonical_bytes):
            return False
        self._leases[lease.lease_token] = (lease, port, 1)
        return True

    def _release_lease_locked(self, lease: SnapshotLease | None) -> None:
        if lease is None:
            return
        stored = self._leases[lease.lease_token]
        if stored[2] == 0:
            return  # This exact lease already has a queued or in-flight return.
        if stored[2] > 1:
            self._leases[lease.lease_token] = (stored[0], stored[1], stored[2] - 1)
            return
        if stored[1] is not None:
            # A queued/in-flight return still owns the sealed snapshot bytes.
            self._leases[lease.lease_token] = (stored[0], stored[1], 0)
            self._releases.append((stored[1], lease))
        else:
            del self._leases[lease.lease_token]
            self._release_bytes_locked(lease.canonical_bytes)

    def register(self, identity: AsyncWorkIdentity) -> bool:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox registration requires typed identity")
        with self._lock:
            old = self._active.get(identity)
            if old is not None:
                return not old.retired
            if (self._closed or identity in self._history
                    or len(self._active) >= self._max_results
                    or self._unfinished_locked() >= self.MAX_UNFINISHED):
                return False
            self._active[identity] = _MotionAdmissionRecord(identity)
        self._wake.set()
        return True

    def mark_submitted(self, identity: AsyncWorkIdentity) -> None:
        """Record the legacy owner's existing nonblocking submission fact."""
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion submission requires the existing typed computation identity")
        submitted = False
        with self._lock:
            record = self._active.get(identity)
            if record is not None and not record.finished:
                record.submitted = True
                submitted = True
        if submitted:
            self._wake.set()

    def prepare_snapshot(self, bounds: PhysicsWorldBounds, *, ruleset: PhysicsRuleset,
                         identity: AsyncWorkIdentity) -> SnapshotLeaseResult:
        if (type(bounds) is not PhysicsWorldBounds or type(ruleset) is not PhysicsRuleset
                or type(identity) is not AsyncWorkIdentity):
            raise ContractViolation("snapshot preparation requires typed bounds, rules and existing identity")
        not_ready = SnapshotLeaseResult(SnapshotLeaseStatus.NOT_READY)
        with self._lock:
            record = self._active.get(identity)
            if record is not None:
                if record.bounds != bounds or record.ruleset != ruleset:
                    raise ContractViolation("an active preparation cannot substitute its snapshot bounds or rules")
                return not_ready if record.retired or record.snapshot_result is None else record.snapshot_result
            if self._closed or self._world is None or identity in self._history:
                return not_ready
            if (self._unfinished_locked() >= self.MAX_UNFINISHED
                    or len(self._active) >= self._max_results
                    or len(self._leases) >= self.MAX_LEASES):
                return SnapshotLeaseResult(SnapshotLeaseStatus.CAPACITY)
            record = _MotionAdmissionRecord(identity, world_port=self._world, bounds=bounds,
                                            ruleset=ruleset, snapshot_pending=True)
            self._active[identity] = record
        self._start_receiver()
        self._wake.set()
        return not_ready

    def register_preparation(self, request: PreparedMotionRequest, lease: SnapshotLease) -> PreparedSubmitResult:
        if type(request) is not PreparedMotionRequest or type(lease) is not SnapshotLease:
            raise ContractViolation("prepared registration requires an independently built request and lease")
        if request.snapshot_identity != lease.identity or request.snapshot_lease_token != lease.lease_token:
            raise ContractViolation("prepared registration cannot replace the independent source lease")
        result = PreparedSubmitResult(PreparedSubmitStatus.ACCEPTED, request.job_token)
        with self._lock:
            if self._closed:
                return PreparedSubmitResult(PreparedSubmitStatus.CLOSED)
            record = self._active.get(request.async_identity)
            if (request.job_token in self._token_history
                    or any(item.job_token == request.job_token
                           for item in self._registry.values())
                    or any(item.request is not None and item.request.job_token == request.job_token
                           for item in self._active.values())
                    or (record is not None and (record.request is not None or record.retired))
                    or (record is None and self._unfinished_locked() >= self.MAX_UNFINISHED)
                    or (record is None and len(self._active) >= self._max_results)
                    or len(self._registry) >= self.MAX_BUNDLES
                    or self._byte_count_locked() + 2 * MAXIMUM_MESSAGE_BYTES
                    + (0 if id(lease.canonical_bytes) in self._bytes else len(lease.canonical_bytes)) > self.MAX_BYTES):
                return PreparedSubmitResult(PreparedSubmitStatus.QUEUE_FULL)
            if record is None:
                if request.async_identity in self._history:
                    return PreparedSubmitResult(PreparedSubmitStatus.QUEUE_FULL)
                record = _MotionAdmissionRecord(request.async_identity, world_port=self._world)
                if not self._retain_lease_locked(lease, record.world_port):
                    return PreparedSubmitResult(PreparedSubmitStatus.QUEUE_FULL)
                record.lease = lease
                self._active[request.async_identity] = record
            elif record.lease is not lease:
                raise ContractViolation("prepared registration must use its active record's exact published lease")
            # Store independent references before any worker submission. No
            # returned bundle participates in constructing these expectations.
            record.request = request
            record.expected_inputs = request.expected_inputs
            record.expected_digest = request.expected_input_digest
            record.reserved_bytes = 2 * MAXIMUM_MESSAGE_BYTES
            self._reserved_bytes += record.reserved_bytes
            record.submission = result
        self._start_receiver()
        self._wake.set()
        return result

    def register_action_preparation(self, preparation: PreparedActionPreparation,
                                    bounds: PhysicsWorldBounds) -> PreparedSubmitResult:
        """Schedule parent request construction in the existing receiver record."""
        if type(preparation) is not PreparedActionPreparation or type(bounds) is not PhysicsWorldBounds:
            raise ContractViolation("action preparation needs typed raw fields and bounded source scope")
        accepted = PreparedSubmitResult(PreparedSubmitStatus.ACCEPTED, preparation.job_token)
        with self._lock:
            if self._closed:
                return PreparedSubmitResult(PreparedSubmitStatus.CLOSED)
            if (self._world is None or preparation.async_identity in self._active
                    or preparation.async_identity in self._history
                    or preparation.job_token in self._token_history
                    or self._unfinished_locked() >= self.MAX_UNFINISHED
                    or len(self._active) >= self._max_results or len(self._leases) >= self.MAX_LEASES
                    or len(self._registry) >= self.MAX_BUNDLES
                    or self._byte_count_locked() + 2 * MAXIMUM_MESSAGE_BYTES > self.MAX_BYTES):
                return PreparedSubmitResult(PreparedSubmitStatus.QUEUE_FULL)
            self._active[preparation.async_identity] = _MotionAdmissionRecord(
                preparation.async_identity, world_port=self._world, bounds=bounds, ruleset=preparation.ruleset,
                snapshot_pending=True, submission=accepted, action_preparation=preparation)
            self._action_preparation_facts = preparation
            for record in tuple(self._registry.values()):
                if record.candidate and (record.identity.scope != preparation.async_identity.scope
                        or record.handle.hot_view.stream != preparation.stream):
                    self._release_candidate_locked(record)
        self._start_receiver()
        self._wake.set()
        return accepted

    def register_action_selection(self, identity: AsyncWorkIdentity, anchor: StateAnchor,
                                  request: GapSolveRequest | AirTransitionSolveRequest,
                                  bounds: PhysicsWorldBounds) -> PreparedSubmitResult:
        """Prepare a SOLVE source on the same receiver before input selection."""
        if (type(identity) is not AsyncWorkIdentity or type(anchor) is not StateAnchor
                or type(request) not in (GapSolveRequest, AirTransitionSolveRequest)
                or type(bounds) is not PhysicsWorldBounds):
            raise ContractViolation("action selection requires bounded immutable source fields")
        selected = PreparedSubmitResult(PreparedSubmitStatus.ACCEPTED, "selection-" + uuid4().hex)
        with self._lock:
            if self._closed:
                return PreparedSubmitResult(PreparedSubmitStatus.CLOSED)
            if (self._world is None or identity in self._active or identity in self._history
                    or self._unfinished_locked() >= self.MAX_UNFINISHED
                    or len(self._active) >= self._max_results or len(self._leases) >= self.MAX_LEASES):
                return PreparedSubmitResult(PreparedSubmitStatus.QUEUE_FULL)
            self._active[identity] = _MotionAdmissionRecord(identity, world_port=self._world,
                bounds=bounds, ruleset=JAVA_1_21_RULESET,
                snapshot_pending=True, submission=selected, action_selection=(anchor, request))
        self._start_receiver()
        self._wake.set()
        return selected

    @property
    def active_identities(self) -> tuple[AsyncWorkIdentity, ...]:
        with self._lock:
            return tuple(sorted((identity for identity, record in self._active.items() if not record.retired),
                                key=lambda identity: identity.key))

    @property
    def registered_handles(self) -> tuple[PreparedMotionHandle, ...]:
        with self._lock:
            return tuple(record.handle for record in self._registry.values())

    def has_registered_handle(self, handle: PreparedMotionHandle) -> bool:
        if type(handle) is not PreparedMotionHandle:
            return False
        with self._lock:
            record = self._registry.get(handle.proof_token)
            return record is not None and record.handle is handle

    def job_active(self, identity: AsyncWorkIdentity) -> bool:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion job query requires a complete computation identity")
        with self._lock:
            record = self._active.get(identity)
            return record is not None and not record.retired

    @staticmethod
    def _usage_locked(record: _PreparedRegistration, use: ProofUse) -> bool:
        return (record.candidate if use is ProofUse.CANDIDATE else
                record.executing is not None if use is ProofUse.EXECUTING else
                record.safe_tail is not None)

    def usage_active(self, handle: PreparedMotionHandle, use: ProofUse) -> bool:
        if type(handle) is not PreparedMotionHandle or type(use) is not ProofUse:
            raise ContractViolation("proof use query requires a typed handle and use")
        with self._lock:
            record = self._registry.get(handle.proof_token)
            return record is not None and record.handle is handle and self._usage_locked(record, use)

    def _reclaim_registration_locked(self, record: _PreparedRegistration) -> None:
        if record.candidate or record.executing is not None or record.safe_tail is not None or record.acceptance is not None:
            return
        del self._registry[record.handle.proof_token]
        self._release_bytes_locked(record.bundle.sealed_bytes)
        self._release_lease_locked(record.lease)
        computation = self._active.get(record.identity)
        if computation is not None:
            computation.admission = PreparedAdmissionResult(PreparedAdmissionStatus.STALE)
            self._remember_locked(computation)

    def _release_candidate_locked(self, record: _PreparedRegistration) -> None:
        record.candidate = False
        self._reclaim_registration_locked(record)

    def release_candidate(self, handle: PreparedMotionHandle) -> bool:
        if type(handle) is not PreparedMotionHandle:
            raise ContractViolation("candidate release requires an exact registered handle")
        with self._lock:
            record = self._registry.get(handle.proof_token)
            if record is None or record.handle is not handle or not record.candidate:
                return False
            self._release_candidate_locked(record)
        self._wake.set()
        return True

    def _activation_rejection_locked(self, record, transaction, handoff) -> ActivationResult | None:
        binding, request, current = transaction.binding, record.bundle.request, self._current_facts
        mismatch = ActivationStatus.ACTIVATION_MISMATCH
        if self._closed or not record.candidate or current is None or current.scope != record.identity.scope:
            return ActivationResult._from_native_references(ActivationStatus.STALE_GENERATION, ActivationRejectReason.GENERATION)
        if binding.generation_id != record.handle.generation_id or current.stream != request.stream:
            return ActivationResult._from_native_references(ActivationStatus.STALE_GENERATION, ActivationRejectReason.GENERATION)
        if binding.proof_token != record.handle.proof_token:
            return ActivationResult._from_native_references(mismatch, ActivationRejectReason.PROOF)
        if binding.owner_id != record.handle.owner_id or current.body_owner_id != record.handle.owner_id:
            return ActivationResult._from_native_references(mismatch, ActivationRejectReason.OWNER)
        if binding.ledger_id != request.ledger_id or current.ledger_id != request.ledger_id:
            return ActivationResult._from_native_references(mismatch, ActivationRejectReason.LEDGER)
        if binding.world_session_id != request.world_session_id or current.world_session_id != request.world_session_id:
            return ActivationResult._from_native_references(mismatch, ActivationRejectReason.WORLD_SESSION)
        window = request.requested_window
        if (binding.requested_window.earliest_tick != window.earliest_start_tick
                or binding.requested_window.latest_tick != window.latest_start_tick
                or current.permitted_window != window or current.movement_tick > request.deadline_tick
                or binding.submitted_movement_tick != current.movement_tick):
            return ActivationResult._from_native_references(mismatch, ActivationRejectReason.WINDOW)
        if (current.ruleset != request.ruleset or current.dependency_versions != record.handle.dependency_versions
                or current.damage_remaining_points < record.required_damage_points
                or current.damage_remaining_points > request.damage_budget.maximum_expected_damage_points):
            return ActivationResult._from_native_references(mismatch, ActivationRejectReason.PROOF)
        entry = next((branch.trajectory[0] for branch in record.handle.hot_view.branches
                      if branch.trajectory[0].movement_tick_id == binding.submitted_movement_tick), None)
        if entry is None or current.current_state != entry:
            return ActivationResult._from_native_references(mismatch, ActivationRejectReason.PROOF)
        final_yaw = entry.yaw_radians + math.radians(binding.compound_action.look.yaw_delta_degrees)
        projected = project_movement_command(entry, binding.compound_action.movement, movement_yaw_radians=final_yaw)
        if (binding.compound_action.valid_for_ticks != 1
                or projected.status is not ProjectionStatus.READY
                or projected.tick_input != request.expected_inputs[0]):
            return ActivationResult._from_native_references(mismatch, ActivationRejectReason.MOVEMENT)
        if (handoff.disposition is not HandoffDisposition.TRANSFERABLE
                or handoff.owner_id != record.handle.owner_id
                or handoff.successor_id != record.handle.owner_id
                or handoff.world_session.value != request.world_session_id
                or handoff.movement_tick_id != binding.submitted_movement_tick
                or handoff.successor_action_index != record.handle.action_index
                or handoff.successor_route_revision != request.route_revision
                or handoff.control_sequence != binding.first_control_sequence
                or handoff.movement != binding.compound_action.movement):
            return ActivationResult._from_native_references(mismatch, ActivationRejectReason.HANDOFF)
        return None

    def refresh_activation_observation(self, handle: PreparedMotionHandle, *, scope, state,
                                       dependency_versions, ledger_id: str, damage_remaining: float) -> bool:
        """Refresh live facts without resurrecting a cancelled qualification or scope."""
        with self._lock:
            record = self._registry.get(handle.proof_token)
            current = self._current_facts
            if (record is None or record.handle is not handle or not record.candidate or self._closed
                    or current is None or current.scope != scope or record.identity.scope != scope
                    or current.stream != handle.hot_view.stream or current.ledger_id != ledger_id):
                return False
            self._current_facts = current._with_activation_observation(state, dependency_versions, damage_remaining)
            return True

    def accept_activation(self, handle: PreparedMotionHandle, transaction: PreparedActivationTransaction,
                          handoff: HandoffEvidence) -> PreparedActivationAcceptance | ActivationResult:
        if (type(handle) is not PreparedMotionHandle or type(transaction) is not PreparedActivationTransaction
                or type(handoff) is not HandoffEvidence):
            raise ContractViolation("activation admission requires typed handle, transaction and handoff facts")
        with self._lock:
            record = self._registry.get(handle.proof_token)
            if record is None or record.handle is not handle:
                return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.PROOF)
            if record.acceptance is not None or record.binding is not None:
                return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.ALREADY_BOUND)
            rejected = self._activation_rejection_locked(record, transaction, handoff)
            if rejected is not None:
                return rejected
            accepted = PreparedActivationAcceptance._from_native_references(transaction.transaction_token, "accept-" + uuid4().hex,
                                                     transaction.binding, transaction.permission_token)
            record.transaction, record.acceptance, record.handoff = transaction, accepted, handoff
            return accepted

    def commit_activation(self, accepted: PreparedActivationAcceptance,
                          delivered: PreparedActivationDelivery, *, action_commit=None) -> ActivationResult:
        if type(accepted) is not PreparedActivationAcceptance or type(delivered) is not PreparedActivationDelivery:
            raise ContractViolation("activation commit requires typed admission and delivery facts")
        with self._lock:
            record = self._registry.get(accepted.binding.proof_token)
            if record is None or record.acceptance is not accepted:
                return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.TRANSACTION)
            transaction = record.transaction
            if (delivered.transaction_token != transaction.transaction_token
                    or delivered.acceptance_token != accepted.acceptance_token
                    or delivered.binding != transaction.binding
                    or delivered.delivered_action != transaction.new_action):
                return ActivationResult._from_native_references(ActivationStatus.ACTIVATION_MISMATCH, ActivationRejectReason.TRANSACTION)
            rejected = self._activation_rejection_locked(record, transaction, record.handoff)
            if rejected is not None:
                return rejected
            executing = ProofConsumerIdentity.from_handle(record.handle, ProofUse.EXECUTING)
            tail = (None if action_commit is None else
                    ProofConsumerIdentity.from_handle(record.handle, ProofUse.SAFE_TAIL))
            if action_commit is not None:
                # The current RouteControl uses only local, bounded fields here.
                # It neither queries this lock nor performs I/O. A rejected
                # commit leaves both the action and all proof uses unchanged.
                result = action_commit.commit_prepared_activation(delivered)
                if result.status is not ActivationStatus.ACTIVATED:
                    return result
            record.executing, record.safe_tail = executing, tail
            record.binding = transaction.binding
            record.commands = (transaction.binding.compound_action,)
            record.candidate = False
            record.transaction = record.acceptance = record.handoff = None
            return ActivationResult._from_native_references(ActivationStatus.ACTIVATED)

    def abort_activation(self, accepted: PreparedActivationAcceptance) -> ReferenceResult:
        if type(accepted) is not PreparedActivationAcceptance:
            raise ContractViolation("activation abort requires the exact issued acceptance")
        with self._lock:
            record = self._registry.get(accepted.binding.proof_token)
            if record is None or record.acceptance is not accepted:
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.TRANSACTION)
            record.transaction = record.acceptance = record.handoff = None
            self._reclaim_registration_locked(record)
        self._wake.set()
        self._return_closed_leases()
        return ReferenceResult(ReferenceStatus.RELEASED)

    def retain_safe_tail(self, handle: PreparedMotionHandle, *, action_index: int) -> ReferenceResult:
        if type(handle) is not PreparedMotionHandle:
            raise ContractViolation("tail retention requires a typed registered handle")
        require_nonnegative_int(action_index, "tail action index")
        with self._lock:
            record = self._registry.get(handle.proof_token)
            if record is None or record.handle is not handle:
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.HANDLE)
            if action_index != handle.action_index:
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.ACTION)
            if record.safe_tail is not None:
                return ReferenceResult(ReferenceStatus.RETAINED)
            if self._closed:
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.CLOSED)
            if record.executing is None:
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.USE_NOT_ACTIVE)
            record.safe_tail = ProofConsumerIdentity.from_handle(handle, ProofUse.SAFE_TAIL)
            return ReferenceResult(ReferenceStatus.RETAINED)

    def record_command(self, handle: PreparedMotionHandle, *, command_index: int,
                       action: ActionSnapshotV1) -> ReferenceResult:
        """Record an actual Runtime/ledger command fact, without sending it."""
        if type(handle) is not PreparedMotionHandle or type(action) is not ActionSnapshotV1:
            raise ContractViolation("actual command recording requires typed handle and final command")
        require_nonnegative_int(command_index, "actual command index")
        with self._lock:
            record = self._registry.get(handle.proof_token)
            if record is None or record.handle is not handle:
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.HANDLE)
            if record.executing is None and record.safe_tail is None:
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.USE_NOT_ACTIVE)
            if self._closed:
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.CLOSED)
            if command_index < len(record.commands):
                return (ReferenceResult(ReferenceStatus.RETAINED) if record.commands[command_index] is action else
                        ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.COMMAND_IDENTITY))
            if (command_index != len(record.commands) or command_index >= record.maximum_commands
                    or action.request_sequence_id <= record.commands[-1].request_sequence_id
                    or action.episode_id != record.commands[0].episode_id):
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.COMMAND_IDENTITY)
            record.commands += (action,)
            return ReferenceResult(ReferenceStatus.RETAINED)

    @staticmethod
    def _release_rejection_locked(record, use, evidence) -> ReferenceRejectReason | None:
        consumer = ProofConsumerIdentity.from_handle(record.handle, use)
        if evidence.consumer is None or evidence.consumer != consumer:
            return ReferenceRejectReason.CONSUMER
        if use is ProofUse.CANDIDATE:
            return None
        terminals = evidence.command_terminals
        if terminals is None or not terminals or len(terminals) != len(record.commands):
            return ReferenceRejectReason.COMMAND_TERMINALS_MISSING
        terminal_states = {InputApplicationStatus.APPLIED, InputApplicationStatus.APPLIED_OUTSIDE_WINDOW,
                           InputApplicationStatus.EXPIRED, InputApplicationStatus.REJECTED}
        for actual, terminal in zip(record.commands, terminals):
            if (terminal.action != actual or terminal.control_sequence != actual.request_sequence_id
                    or terminal.session.value != consumer.world_session_id):
                return ReferenceRejectReason.COMMAND_IDENTITY
            # SUPERSEDED is the ledger's local terminal summary after a newer
            # applied sample. It is not an explicit terminal receipt for the
            # proof registry, even though the client sequence gate rejects any
            # later request whose sequence is no longer newer.
            if terminal.status is InputApplicationStatus.SUPERSEDED:
                return ReferenceRejectReason.COMMAND_REVOCATION_UNCONFIRMED
            if terminal.status not in terminal_states or terminal.resolved_at_tick is None:
                return ReferenceRejectReason.COMMAND_UNRESOLVED
        action_exit = evidence.action_handoff
        if (type(action_exit) is not ProofActionExitEvidence or action_exit.consumer != consumer):
            return ReferenceRejectReason.ACTION_UNRESOLVED
        handoff = action_exit.handoff
        if (handoff.owner_id != consumer.owner_id
                or handoff.world_session.value != consumer.world_session_id
                or handoff.disposition is HandoffDisposition.RETAIN or handoff.movement_tick_id is None
                or any(terminal.resolved_at_tick > handoff.movement_tick_id for terminal in terminals)):
            return ReferenceRejectReason.ACTION_UNRESOLVED
        if (handoff.disposition is HandoffDisposition.TRANSFERABLE
                and handoff.successor_id == consumer.owner_id
                and handoff.successor_action_index == consumer.action_index):
            return ReferenceRejectReason.ACTION_UNRESOLVED
        confirmed = evidence.supervisor_confirmation
        if (type(confirmed) is not ProofSupervisorReleaseEvidence or confirmed.consumer != consumer
                or confirmed.confirmed_exit_id != action_exit.exit_id
                or confirmed.handoff != handoff):
            return ReferenceRejectReason.SUPERVISOR_UNCONFIRMED
        return None

    def release_usage(self, handle: PreparedMotionHandle, *, use: ProofUse,
                      evidence: ProofReleaseEvidence) -> ReferenceResult:
        if type(handle) is not PreparedMotionHandle or type(use) is not ProofUse or type(evidence) is not ProofReleaseEvidence:
            raise ContractViolation("proof release requires typed handle, use and independent release facts")
        with self._lock:
            record = self._registry.get(handle.proof_token)
            if record is None or record.handle is not handle:
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.HANDLE)
            if not self._usage_locked(record, use):
                return ReferenceResult(ReferenceStatus.REJECTED, ReferenceRejectReason.USE_NOT_ACTIVE)
            rejected = self._release_rejection_locked(record, use, evidence)
            if rejected is not None:
                return ReferenceResult(ReferenceStatus.REJECTED, rejected)
            if use is ProofUse.CANDIDATE:
                record.candidate = False
            elif use is ProofUse.EXECUTING:
                record.executing = None
            else:
                record.safe_tail = None
            self._reclaim_registration_locked(record)
        self._wake.set()
        self._return_closed_leases()
        return ReferenceResult(ReferenceStatus.RELEASED)

    def query_prepared(self, identity: AsyncWorkIdentity, *, movement_tick: int) -> PreparedQueryResult:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("prepared query requires a complete computation identity")
        require_nonnegative_int(movement_tick, "prepared query movement tick")
        with self._lock:
            registered = next((record for record in self._registry.values()
                               if record.identity == identity and record.candidate), None)
            pending = self._active.get(identity)
            deadline = (registered.bundle.request.deadline_tick if registered is not None else
                        pending.request.deadline_tick if pending is not None and pending.request is not None else None)
            if deadline is not None and movement_tick > deadline:
                return self._OVERDUE
            if registered is None:
                return self._NOT_READY
            if (self._receiver_diagnostics_enabled
                    and self._mark_diagnostic_consumed_locked(identity)):
                self._receiver_diagnostics.append({
                    "schema_version": "mc2p.motion-receiver-diagnostic.v2",
                    "event": "first_control_consume",
                    "identity": repr(identity.key),
                    "movement_tick": movement_tick,
                    "monotonic_ns": time.perf_counter_ns(),
                })
            # Returning a cached object also avoids the value constructor's
            # recursive immutable-graph check on the control thread.
            # This is registration availability, not entry/body authorization.
            # The owner compares hot_view with current facts in Tasks 6/7.
            return registered.ready_query

    def _diagnostic_record(self, identity: AsyncWorkIdentity):
        return self._active.get(identity) or self._history.get(identity)

    def prepared_admission(self, identity: AsyncWorkIdentity) -> PreparedAdmissionResult | None:
        with self._lock:
            record = self._diagnostic_record(identity)
            if record is not None and record.admission is not None:
                return record.admission
            registered = next((item for item in self._registry.values() if item.identity == identity), None)
            return None if registered is None else registered.admission

    def prepared_compute_result(self, identity: AsyncWorkIdentity) -> PreparedComputeResult | None:
        with self._lock:
            record = self._diagnostic_record(identity)
            return None if record is None else record.computation

    def drain_once(self, worker: MotionWorkerPort, observation_sequence: int) -> tuple[GapMotionSolveResult, ...]:
        """Compatibility read of already sorted legacy results; never polls."""
        require_nonnegative_int(observation_sequence, "motion inbox sequence")
        self.attach_worker(worker)
        self._wake.set()
        with self._lock:
            if self._last_drain_sequence == observation_sequence:
                return ()
            self._last_drain_sequence = observation_sequence
            result = tuple(self._discarded)
            self._discarded.clear()
            return result

    def take(self, identity: AsyncWorkIdentity) -> tuple[GapMotionSolveResult, ...]:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox take requires typed identity")
        with self._lock:
            result = self._results.pop(identity, None)
            if (result is not None and self._receiver_diagnostics_enabled
                    and self._mark_diagnostic_consumed_locked(identity)):
                self._receiver_diagnostics.append({
                    "schema_version": "mc2p.motion-receiver-diagnostic.v2",
                    "event": "first_control_consume",
                    "identity": repr(identity.key),
                    "movement_tick": self._diagnostic_tick_locked(),
                    "monotonic_ns": time.perf_counter_ns(),
                })
        return () if result is None else (result,)

    def drain_receiver_diagnostics(self) -> tuple[dict, ...]:
        """Return bounded read-only timing rows without changing control state."""
        with self._lock:
            rows = tuple(self._receiver_diagnostics)
            self._receiver_diagnostics.clear()
            return rows

    def peek(self, identity: AsyncWorkIdentity) -> GapMotionSolveResult | None:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox peek requires typed identity")
        with self._lock:
            return self._results.get(identity)

    def _remember_locked(self, record: _MotionAdmissionRecord) -> None:
        # Historical records hold diagnostics only, never the large object graph.
        history = _MotionAdmissionRecord(record.identity, finished=True, retired=record.retired,
            admission=(None if record.admission is not None
                       and record.admission.status is PreparedAdmissionStatus.REGISTERED else record.admission),
            computation=record.computation, submission=record.submission)
        self._history[record.identity] = history
        while len(self._history) > self.MAX_HISTORY:
            del self._history[next(iter(self._history))]
        if record.request is not None and record.request.job_token not in self._token_history:
            self._token_history.append(record.request.job_token)

    def _finish_locked(self, record: _MotionAdmissionRecord) -> None:
        record.finished = True
        self._reserved_bytes -= record.reserved_bytes
        record.reserved_bytes = 0
        self._release_bytes_locked(record.canonical_request)
        self._release_bytes_locked(record.sealed_request)
        record.canonical_request = record.sealed_request = None
        self._remember_locked(record)
        if record.retired:
            self._remove_active_locked(record)

    def _remove_active_locked(self, record: _MotionAdmissionRecord) -> None:
        self._active.pop(record.identity, None)
        self._release_lease_locked(record.lease)
        record.lease = None
        record.admission = None

    def retire(self, identity: AsyncWorkIdentity) -> None:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("motion inbox retirement requires typed identity")
        with self._lock:
            record = self._active.get(identity)
            queued = self._results.pop(identity, None)
            self.discarded_results += queued is not None
            if record is None:
                return
            record.retired = True
            if not record.finished:
                record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.CANCELLED)
            self._remember_locked(record)
            # An in-flight validator/worker retains its slot and lease. Its
            # own completion clears them after it has lost publication rights.
            if record.finished or (not record.submitted and not record.processing):
                self._finish_locked(record)
        self._wake.set()

    def wake_receiver(self) -> None:
        self._wake.set()

    def wait_for_receiver(self, *, timeout: float = 5.) -> bool:
        """Explicit test/tool barrier. Production control methods never call it."""
        with self._cycle_condition:
            if self._receiver is None:
                return False
            self._requested_cycle += 1
            requested = self._requested_cycle
            self._wake.set()
            return self._cycle_condition.wait_for(lambda: self._cycle >= requested or self._stop.is_set(), timeout)

    def _seal_snapshot(self, record: _MotionAdmissionRecord) -> None:
        with self._lock:
            if record.retired or self._closed:
                record.processing = False
                self._finish_locked(record)
                return
        port = record.world_port  # Keep the port from submission across session replacement.
        result = SnapshotLeaseResult(SnapshotLeaseStatus.NOT_READY)
        try:
            version = port.borrow_snapshot_version(record.bounds, ruleset=record.ruleset)
            if version is not None:
                sealed = seal_snapshot_version(version, record.ruleset)
                if port.publish_sealed_snapshot(version, sealed):
                    result = port.acquire_published_snapshot(record.bounds, ruleset=record.ruleset)
        except Exception:
            pass
        with self._lock:
            record.snapshot_pending = False
            if result.lease is not None:
                if record.retired or self._closed or not self._retain_lease_locked(result.lease, port):
                    self._releases.append((port, result.lease))
                    result = SnapshotLeaseResult(SnapshotLeaseStatus.CAPACITY)
                else:
                    record.lease = result.lease
            record.snapshot_result = result
            record.processing = (result.status is SnapshotLeaseStatus.READY and not record.retired
                                  and (record.action_preparation is not None or record.action_selection is not None))
            if record.retired:
                self._finish_locked(record)
                return
        preparation = record.action_preparation
        selection = record.action_selection
        if selection is not None:
            anchor, request = selection
            if result.status is not SnapshotLeaseStatus.READY:
                with self._lock:
                    self._results[record.identity] = GapMotionSolveResult(record.identity.subject_id,
                        record.identity.revision, SolveResult(SolveStatus.NEEDS_WORLD,
                            reasons=("prepared_selection_snapshot_unavailable",)), 0, record.identity)
                    self._finish_locked(record)
                return
            try:
                job = GapMotionSolveJob(record.identity.subject_id, record.identity.revision, anchor,
                    PhysicsWorldView(WorldView.from_sealed_snapshot(result.lease.snapshot), record.ruleset),
                    request, record.identity)
                with self._lock:
                    if record.retired or self._closed:
                        record.processing = False
                        self._finish_locked(record)
                        return
                submitted = self._worker is not None and self._worker.submit(job)
            except Exception:
                submitted = False
            with self._lock:
                record.processing = False
                record.submitted = submitted
                if not submitted:
                    self._results[record.identity] = GapMotionSolveResult(record.identity.subject_id,
                        record.identity.revision, SolveResult(SolveStatus.INTERNAL_ERROR,
                            reasons=("prepared_selection_not_submitted",)), 0, record.identity)
                    self._finish_locked(record)
        if preparation is not None:
            if result.status is not SnapshotLeaseStatus.READY:
                with self._lock:
                    if not record.retired:
                        record.computation = PreparedComputeResult(PreparedComputeStatus.MISSING_INFORMATION,
                            preparation.job_token, PreparedComputeCounts())
                        record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.REJECTED,
                            reason=PreparedRejectReason.WORLD_SNAPSHOT)
                        self._finish_locked(record)
                return
            try:
                # This is the parent's immutable selection, not worker output.
                # Build, digest and validate on the sole receiver before IPC.
                request = preparation.build_request(result.lease)
                current = PreparedCurrentFacts(request.async_identity.scope, request.stream,
                    request.stream.owner_id, request.world_session_id, request.ruleset,
                    result.lease.dependency_versions, request.ledger_id, result.lease.identity,
                    preparation.current_state, request.damage_remaining_points,
                    preparation.current_state.movement_tick_id, request.requested_window)
                submitted = self.register_preparation(request, result.lease)
                with self._lock:
                    record.processing = False
                    if (not record.retired and self._active.get(record.identity) is record
                            and self._action_preparation_facts is preparation
                            and submitted.status is PreparedSubmitStatus.ACCEPTED):
                        self._current_facts = current
                    elif not record.finished:
                        record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.CANCELLED)
                        record.retired = True
                        self._finish_locked(record)
            except Exception as error:
                with self._lock:
                    record.processing = False
                    if record.retired:
                        record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.CANCELLED)
                    elif isinstance(error, (ContractViolation, ValueError, TypeError)):
                        record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.REJECTED,
                            reason=PreparedRejectReason.REQUEST)
                    else:
                        record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.STALE)
                        record.computation = PreparedComputeResult(PreparedComputeStatus.WORKER_FAILED,
                            preparation.job_token, PreparedComputeCounts())
                    self._finish_locked(record)

    def _submit_prepared(self, record: _MotionAdmissionRecord) -> None:
        with self._lock:
            if record.retired or self._closed:
                record.processing = False
                self._finish_locked(record)
                return
        request = record.request
        try:
            # A copied wrapper is not an issued source lease. Read the formal
            # owner before submitting, outside the inbox metadata lock.
            if record.world_port is not None and not record.world_port.snapshot_lease_is_live(record.lease):
                with self._lock:
                    record.processing = False
                    record.admission = (PreparedAdmissionResult(PreparedAdmissionStatus.CANCELLED)
                        if record.retired or self._closed else PreparedAdmissionResult(
                            PreparedAdmissionStatus.REJECTED, reason=PreparedRejectReason.WORLD_SNAPSHOT))
                    self._finish_locked(record)
                return
            # Both canonical parent bindings and IPC serialization happen here,
            # before submission, outside the short publication metadata lock.
            request.__post_init__()
            if (request.expected_inputs is not record.expected_inputs
                    or request.expected_input_digest != record.expected_digest
                    or canonical_motion_digest(record.expected_inputs) != record.expected_digest):
                raise ContractViolation("independent parent inputs changed before submission")
            canonical = canonical_motion_bytes(request)
            job = PreparedMotionJob(request, record.lease.sealed_snapshot())
            transport = pickle.dumps(job, protocol=5)
            if max(len(canonical), len(transport)) > MAXIMUM_MESSAGE_BYTES:
                raise PreparedMessageBudgetExceeded("prepared request exceeds its sealed byte budget")
            with self._lock:
                self._reserved_bytes -= record.reserved_bytes
                record.reserved_bytes = 0
                if record.retired or self._closed:
                    record.processing = False
                    self._finish_locked(record)
                    return
                if (self._byte_count_locked() + len(canonical) + len(transport) > self.MAX_BYTES):
                    raise PreparedMessageBudgetExceeded("prepared service retained-byte capacity is full")
                self._retain_bytes_locked(canonical)
                self._retain_bytes_locked(transport)
                record.canonical_request, record.sealed_request = canonical, transport
            submitted = self._worker is not None and self._worker.submit(job)
            with self._lock:
                record.processing = False
                record.submitted = submitted
                if not submitted:
                    record.submission = PreparedSubmitResult(PreparedSubmitStatus.QUEUE_FULL)
                    record.computation = PreparedComputeResult(PreparedComputeStatus.BUDGET_EXHAUSTED,
                                                                request.job_token, PreparedComputeCounts())
                    self._finish_locked(record)
        except Exception as error:
            with self._lock:
                record.processing = False
                record.computation = PreparedComputeResult(
                    PreparedComputeStatus.BUDGET_EXHAUSTED if isinstance(error, PreparedMessageBudgetExceeded)
                    else PreparedComputeStatus.WORKER_FAILED, request.job_token, PreparedComputeCounts())
                record.submission = PreparedSubmitResult(PreparedSubmitStatus.QUEUE_FULL)
                self._finish_locked(record)

    def _process_legacy(self, result: GapMotionSolveResult) -> None:
        with self._lock:
            record = self._active.get(result.work_identity)
            if (record is None or record.retired or record.finished
                    or record.request is not None or result.work_identity in self._results):
                self.discarded_results += 1
                self._discarded.append(result)
                if record is not None and record.retired:
                    self._finish_locked(record)
                return
            self._results[result.work_identity] = result
            if self._receiver_diagnostics_enabled:
                self._receiver_diagnostics.append({
                    "schema_version": "mc2p.motion-receiver-diagnostic.v2",
                    "event": "registry_publish",
                    "identity": repr(result.work_identity.key),
                    "movement_tick": self._diagnostic_tick_locked(),
                    "monotonic_ns": time.perf_counter_ns(),
                })
            self._finish_locked(record)

    def _publish_prepared(self, record, validated, current, ready_query) -> None:
        # This is the same linearization boundary as retire(). Validation,
        # hashes, handle construction and query constructors happened outside.
        with self._lock:
            record.processing = False
            latest = self._current_facts
            if record.retired or self._closed or self._active.get(record.identity) is not record:
                record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.CANCELLED)
            elif (latest is None or latest.scope != record.request.async_identity.scope
                    or latest.stream != record.request.stream
                    or latest.body_owner_id != record.request.stream.owner_id
                    or latest.world_session_id != record.request.world_session_id
                    or latest.ledger_id != record.request.ledger_id
                    or latest.ruleset != record.request.ruleset
                    or latest.permitted_window != record.request.requested_window
                    or latest.movement_tick > record.request.deadline_tick):
                record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.STALE)
            elif validated.bundle is None:
                record.admission = validated.admission
                record.computation = validated.computation
            elif len(self._registry) >= self.MAX_BUNDLES or not self._retain_bytes_locked(validated.bundle.sealed_bytes):
                record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.REJECTED,
                                                            reason=PreparedRejectReason.MESSAGE)
                record.submission = PreparedSubmitResult(PreparedSubmitStatus.QUEUE_FULL)
            else:
                handle = validated.admission.handle
                self._retain_lease_locked(record.lease, record.world_port)
                registered = _PreparedRegistration(record.identity, record.request.job_token,
                                                    validated.bundle, handle, record.lease,
                                                    record.world_port, ready_query, validated.admission,
                    required_damage_points=max(tail.expected_damage_points for tail in handle.hot_view.safety_tails),
                    maximum_commands=len(handle.hot_view.inputs) + max(len(tail.inputs) for tail in handle.hot_view.safety_tails))
                self._registry[handle.proof_token] = registered
                record.admission = validated.admission
            if self._receiver_diagnostics_enabled:
                self._receiver_diagnostics.append({
                    "schema_version": "mc2p.motion-receiver-diagnostic.v2",
                    "event": "registry_publish",
                    "identity": repr(record.identity.key),
                    "movement_tick": self._diagnostic_tick_locked(),
                    "monotonic_ns": time.perf_counter_ns(),
                    "status": (
                        None if record.admission is None else record.admission.status.value
                    ),
                })
            self._finish_locked(record)

    def _process_prepared(self, message: PreparedWireMessage) -> None:
        with self._lock:
            record = next((item for item in self._active.values() if item.request is not None
                           and item.request.job_token == message.job_token), None)
            current = self._current_facts
            if record is None or record.finished or record.processing:
                self.discarded_results += 1
                return
            if record.retired:
                record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.CANCELLED)
                self.discarded_results += 1
                self._finish_locked(record)
                return
            # Reserve decoded sealed bytes + canonical proof/bundle scratch.
            # The payload itself is counted by reference and deduplicated.
            reserve = 3 * MAXIMUM_MESSAGE_BYTES
            if (current is None or type(message.payload) is not bytes
                    or self._byte_count_locked() + len(message.payload) + reserve > self.MAX_BYTES):
                record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.REJECTED,
                                                            reason=PreparedRejectReason.MESSAGE)
                record.submission = PreparedSubmitResult(PreparedSubmitStatus.QUEUE_FULL)
                self._finish_locked(record)
                return
            self._retain_bytes_locked(message.payload)
            self._validation_bytes += reserve
            record.processing = True
        reserve_live = True
        decoded_bytes = None
        try:
            arguments = (message, record.request, record.lease.sealed_snapshot(), current)
            if prepared_motion_validation.local_worker_result_is_trusted(self._worker):
                validated = prepared_motion_validation._validate_for_registration(
                    *arguments, trusted_local_worker=True)
            else:
                validated = prepared_motion_validation._validate_for_registration(*arguments)
            if validated.bundle is not None and record.world_port is not None:
                # These are reads from the formal owner, outside inbox locks.
                # A source lease is not authenticated by matching token text.
                if not record.world_port.snapshot_lease_is_live(record.lease):
                    validated = prepared_motion_validation._reject(PreparedRejectReason.WORLD_SNAPSHOT)
                elif record.world_port.dependency_changes(
                        validated.bundle.proof.dependency_versions).status is not DependencyStatus.UNCHANGED:
                    validated = prepared_motion_validation._reject(PreparedRejectReason.DEPENDENCY)
            ready = (None if validated.admission.handle is None else
                     PreparedQueryResult(PreparedQueryStatus.READY_MATCH, validated.admission.handle))
            # Convert scratch reservation to the decoded bundle's exact byte
            # reference atomically. Transport and decoded storage stay charged
            # while a publish/cancel race is still unresolved.
            with self._lock:
                self._validation_bytes -= reserve
                reserve_live = False
                if validated.bundle is not None:
                    decoded_bytes = validated.bundle.sealed_bytes
                    if not self._retain_bytes_locked(decoded_bytes):
                        raise PreparedMessageBudgetExceeded("decoded bundle exceeded its admission reservation")
            self._publish_prepared(record, validated, current, ready)
        except Exception:
            with self._lock:
                record.processing = False
                record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.REJECTED,
                                                            reason=PreparedRejectReason.PROOF)
                self._finish_locked(record)
        finally:
            with self._lock:
                if reserve_live:
                    self._validation_bytes -= reserve
                self._release_bytes_locked(message.payload)
                if decoded_bytes is not None and id(decoded_bytes) in self._bytes:
                    self._release_bytes_locked(decoded_bytes)

    def _receive(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait(.01)
                self._wake.clear()
                result_count = 0
                with self._lock:
                    diagnostics_enabled = self._receiver_diagnostics_enabled
                    cycle_started_ns = (
                        time.perf_counter_ns() if diagnostics_enabled else None
                    )
                    requested = self._requested_cycle
                    records = tuple(record for record in self._active.values()
                                    if not record.retired and not record.processing and not record.finished
                                    and (record.snapshot_pending or (record.request is not None and not record.submitted)))
                    for record in records:
                        record.processing = True
                    worker = self._worker
                for record in records:
                    if record.snapshot_pending:
                        self._seal_snapshot(record)
                    else:
                        self._submit_prepared(record)
                if worker is not None and not self._closed:
                    try:
                        results = worker.poll_available()
                        alive = worker.is_alive()
                    except Exception:
                        results, alive = (), False
                    result_count = len(results)
                    for result in results:
                        identity = getattr(result, "work_identity", None)
                        if diagnostics_enabled and type(identity) is AsyncWorkIdentity:
                            with self._lock:
                                self._receiver_diagnostics.append({
                                    "schema_version": "mc2p.motion-receiver-diagnostic.v2",
                                    "event": "receiver_observed_result",
                                    "identity": repr(identity.key),
                                    "movement_tick": self._diagnostic_tick_locked(),
                                    "monotonic_ns": time.perf_counter_ns(),
                                })
                        if type(result) is PreparedWireMessage:
                            self._process_prepared(result)
                        elif type(result) is GapMotionSolveResult:
                            self._process_legacy(result)
                        else:
                            with self._lock:
                                self.discarded_results += 1
                    if not alive:
                        with self._lock:
                            for record in tuple(self._active.values()):
                                if not record.finished and record.action_selection is not None:
                                    self._results[record.identity] = GapMotionSolveResult(record.identity.subject_id,
                                        record.identity.revision, SolveResult(SolveStatus.INTERNAL_ERROR,
                                            reasons=("prepared_selection_worker_died",)), 0, record.identity)
                                    self._finish_locked(record)
                                elif not record.finished and (record.request is not None or record.action_preparation is not None):
                                    token = (record.request.job_token if record.request is not None
                                             else record.action_preparation.job_token)
                                    record.computation = PreparedComputeResult(PreparedComputeStatus.WORKER_FAILED,
                                        token, PreparedComputeCounts())
                                    record.admission = PreparedAdmissionResult(PreparedAdmissionStatus.STALE)
                                    self._finish_locked(record)
                self._return_leases()
                with self._cycle_condition:
                    if diagnostics_enabled:
                        cycle_finished_ns = time.perf_counter_ns()
                        self._diagnostic_cycle += 1
                        self._receiver_diagnostics.append({
                            "schema_version": "mc2p.motion-receiver-diagnostic.v2",
                            "event": "receiver_cycle",
                            "cycle": self._diagnostic_cycle,
                            "movement_tick": self._diagnostic_tick_locked(),
                            "started_monotonic_ns": cycle_started_ns,
                            "finished_monotonic_ns": cycle_finished_ns,
                            "duration_ns": cycle_finished_ns - cycle_started_ns,
                            "result_count": result_count,
                        })
                    self._cycle = max(self._cycle, requested)
                    self._cycle_condition.notify_all()
        finally:
            # close() may have exhausted its bounded join. This thread still
            # owns all polling rights until its actual exit cleanup completes.
            try:
                with self._lock:
                    if self._closed:
                        for record in tuple(self._active.values()):
                            if not record.processing:
                                self._finish_locked(record)
                self._return_leases()
                self._close_worker_until(self._shutdown_deadline)
            finally:
                with self._cycle_condition:
                    self._cycle_condition.notify_all()
                self._return_consumer_claim()

    def _return_consumer_claim(self) -> None:
        if self._worker is None:
            return
        with self._consumer_lock:
            claim = self._consumers.get(self._worker)
            if claim is not None and claim() is self:
                self._consumers.pop(self._worker, None)

    def _return_leases(self, *, exclude: set[int] | None = None) -> set[int]:
        """Attempt one batch; leave prior failures for a later external wake."""
        with self._lock:
            releases = tuple((port, lease) for port, lease in self._releases
                             if exclude is None or id(lease) not in exclude)
            self._releases = deque((port, lease) for port, lease in self._releases
                                   if exclude is not None and id(lease) in exclude)
        seen: set[int] = set()
        for port, lease in releases:
            if id(lease) in seen:
                continue
            seen.add(id(lease))
            try:
                returned = port.release_snapshot(lease.lease_token,
                    expected_snapshot=lease.snapshot, expected_lease=lease)
                if returned is False and port.snapshot_lease_is_live(lease):
                    raise ContractViolation("the source world retained a supposedly returned snapshot")
            except Exception:
                with self._lock:
                    self._releases.append((port, lease))
                continue
            with self._lock:
                stored = self._leases.get(lease.lease_token)
                if stored is not None and stored[0] is lease and stored[2] == 0:
                    del self._leases[lease.lease_token]
                    self._release_bytes_locked(lease.canonical_bytes)
        return seen

    def _return_closed_leases_until_idle(self) -> None:
        """One source caller drains arrivals; failed items wait for a new trigger."""
        attempted: set[int] = set()
        with self._lease_return_condition:
            observed_generation = self._lease_return_generation
        while True:
            attempted.update(self._return_leases(exclude=attempted))
            with self._lease_return_condition:
                if any(id(lease) not in attempted for _, lease in self._releases):
                    continue  # A use ended while the previous source call ran.
                if self._lease_return_generation != observed_generation:
                    observed_generation += 1
                    attempted.clear()  # Exactly one new attempt per explicit trigger.
                    continue
                if not self._leases and not self._releases:
                    # The closed service cannot add new work. Ending only
                    # after full drain means no close can hand a queued retry
                    # to a Thread whose target has already returned.
                    return
                while self._lease_return_generation == observed_generation:
                    self._lease_return_condition.wait()  # Atomic unlock/wait; no lost wake.
                observed_generation += 1
                attempted.clear()

    def _schedule_closed_lease_return(self) -> threading.Thread | None:
        """Retry source-port returns off the caller; one attempt per trigger."""
        with self._lease_return_condition:
            if not self._closed or not self._releases:
                return None
            if self._lease_returner is not None and self._lease_returner.is_alive():
                self._lease_return_generation += 1
                self._lease_return_condition.notify()
                return self._lease_returner
            if self._receiver is not None and self._receiver.is_alive():
                return self._receiver
            returner = threading.Thread(target=self._return_closed_leases_until_idle,
                                        name="mc2p-motion-lease-return",
                                        daemon=True)
            self._lease_returner = returner
            returner.start()
            return returner

    def _return_closed_leases(self) -> None:
        self._schedule_closed_lease_return()

    def _close_worker_until(self, deadline: float) -> None:
        if not self._closed or not self._close_worker_requested or self._worker is None:
            return
        if type(self._worker) is MotionSolverWorker:
            self._worker_close_report = self._worker.close(timeout_seconds=_remaining(deadline))
        elif not self._worker_close_attempted and threading.current_thread() is self._receiver:
            # Non-process ports close on this existing receiver, so a faulty
            # port cannot create an unbounded wait in the calling thread.
            self._worker_close_attempted = True
            try:
                self._worker.close()
            except Exception:
                pass  # Actual liveness in the report records the incomplete close.

    def _close_report(self) -> MotionCloseReport:
        worker, receiver = self._worker, self._receiver
        alive = int(worker is not None and worker.is_alive())
        code = None if worker is None else getattr(worker, "exit_code", None)
        code = code if type(code) is int else None
        feeders = 0 if type(worker) is not MotionSolverWorker else worker.live_queue_feeders
        expected_errors = 0 if type(worker) is not MotionSolverWorker else len(worker._expected_close_feeder_errors)
        unexpected_errors = 0 if type(worker) is not MotionSolverWorker else len(worker._unexpected_feeder_errors)
        with self._lock:
            records = tuple(self._registry.values())
            responsibilities = tuple(consumer for record in records
                                     for consumer in (record.executing, record.safe_tail) if consumer is not None)
            live_receiver = int(receiver is not None and receiver.is_alive())
            returner = self._lease_returner
            live_returner = int(returner is not None and returner.is_alive())
            return MotionCloseReport(worker_exit_code=code, live_workers=alive, live_receivers=live_receiver,
                live_lease_returners=live_returner,
                live_queue_feeders=feeders, unfinished_computations=self._unfinished_locked(),
                expected_close_feeder_errors=expected_errors, unexpected_feeder_errors=unexpected_errors,
                candidate_references=sum(record.candidate for record in records),
                executing_references=sum(record.executing is not None for record in records),
                safe_tail_references=sum(record.safe_tail is not None for record in records),
                activation_references=sum(record.acceptance is not None for record in records),
                lease_count=len(self._leases), queued_returns=len(self._releases),
                retained_bytes=self._byte_count_locked(),
                timed_out=bool(live_receiver or live_returner or feeders or self._releases
                                  or (alive and self._close_worker_requested)),
                retained_responsibilities=responsibilities)

    def close(self, *, timeout_seconds: float = 5., close_worker: bool = True) -> MotionCloseReport:
        deadline = _close_deadline(timeout_seconds)
        if type(close_worker) is not bool:
            raise ContractViolation("motion worker close ownership must be a boolean")
        with self._lock:
            self._closed = True
            self._close_worker_requested |= close_worker
            self._shutdown_deadline = deadline
            identities = tuple(self._active)
            for record in tuple(self._registry.values()):
                # No accepted transaction can commit after closure. It has
                # not sent/advanced anything, so revoke that temporary use.
                record.transaction = record.acceptance = record.handoff = None
                record.candidate = False
                self._reclaim_registration_locked(record)
        for identity in identities:
            self.retire(identity)
        self._stop.set()
        self._wake.set()
        receiver = self._receiver
        if receiver is not None and receiver.ident is not None and receiver is not threading.current_thread():
            receiver.join(_remaining(deadline))
        # A validator still running owns its local references and will lose the
        # publish race. Do not reclaim those records before it has returned.
        with self._lock:
            if receiver is None or not receiver.is_alive():
                for record in tuple(self._active.values()):
                    if not record.processing:
                        self._finish_locked(record)
        if receiver is None or not receiver.is_alive():
            returner = self._schedule_closed_lease_return()
            self._close_worker_until(deadline)
            if (returner is not None and returner is not threading.current_thread()
                    and returner.ident is not None):
                returner.join(_remaining(deadline))
        if self._worker is not None and (receiver is None or not receiver.is_alive()):
            self._return_consumer_claim()
        return self._close_report()
