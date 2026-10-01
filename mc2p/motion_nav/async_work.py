"""Small immutable contracts shared by bounded asynchronous work.

The domain owners still decide what a result means.  These values only make
identity, time windows and admission evidence explicit across phase changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from hashlib import sha256
from uuid import uuid4

from mc2p.contracts.common import ContractViolation, require_identifier


class AsyncWorkKind(StrEnum):
    PLANNING = "planning"
    INFORMATION = "information"
    MOTION_SOLVE = "motion_solve"
    PLACEMENT_CONFIRMATION = "placement_confirmation"


@dataclass(frozen=True, slots=True)
class AsyncWorkIdentity:
    world_session_id: str
    task_id: str
    owner_instance_id: str
    work_kind: AsyncWorkKind
    subject_id: str
    revision: int

    def __post_init__(self) -> None:
        require_identifier(self.world_session_id, "async work world session")
        require_identifier(self.task_id, "async work task")
        require_identifier(self.owner_instance_id, "async work owner")
        if type(self.work_kind) is not AsyncWorkKind:
            raise ContractViolation("async work kind must be typed")
        require_identifier(self.subject_id, "async work subject")
        if type(self.revision) is not int or self.revision < 1:
            raise ContractViolation("async work revision must be positive")

    @property
    def key(self) -> str:
        value = json.dumps((
            self.world_session_id,
            self.work_kind.value,
            self.task_id,
            self.owner_instance_id,
            self.subject_id,
            self.revision,
        ), separators=(",", ":"))
        return f"{self.work_kind.value}:{sha256(value.encode('utf-8')).hexdigest()}"


class AsyncOwnerScope:
    """A parent allocates instances independently of goals and geometry."""

    def __init__(self) -> None:
        self._scope = uuid4().hex
        self._generation = 0

    def allocate(self) -> str:
        self._generation += 1
        return f"{self._scope}:{self._generation}"


@dataclass(frozen=True, slots=True)
class AsyncWorkWindow:
    started_movement_tick: int
    started_monotonic_ns: int
    deadline_monotonic_ns: int

    def __post_init__(self) -> None:
        if (type(self.started_movement_tick) is not int
                or self.started_movement_tick < 0):
            raise ContractViolation("async work movement tick is invalid")
        if (type(self.started_monotonic_ns) is not int
                or self.started_monotonic_ns < 0
                or type(self.deadline_monotonic_ns) is not int
                or self.deadline_monotonic_ns <= self.started_monotonic_ns):
            raise ContractViolation("async work window is invalid")

    def expired(self, monotonic_ns: int) -> bool:
        if type(monotonic_ns) is not int or monotonic_ns < 0:
            raise ContractViolation("async work clock is invalid")
        return monotonic_ns >= self.deadline_monotonic_ns


class AsyncAdmissionDisposition(StrEnum):
    APPLIED = "applied"
    RECOMPUTE = "recompute"
    DISCARDED_LATE = "discarded_late"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class AsyncAdmissionRecord:
    identity: AsyncWorkIdentity
    accepted_monotonic_ns: int
    deadline_monotonic_ns: int
    identity_matched: bool
    facts_valid: bool | None
    disposition: AsyncAdmissionDisposition

    def __post_init__(self) -> None:
        if type(self.identity) is not AsyncWorkIdentity:
            raise ContractViolation("async admission identity must be typed")
        if (type(self.accepted_monotonic_ns) is not int
                or self.accepted_monotonic_ns < 0
                or type(self.deadline_monotonic_ns) is not int
                or self.deadline_monotonic_ns < 0):
            raise ContractViolation("async admission time is invalid")
        if type(self.identity_matched) is not bool:
            raise ContractViolation("async admission identity flag is invalid")
        if self.facts_valid is not None and type(self.facts_valid) is not bool:
            raise ContractViolation("async admission fact flag is invalid")
        if type(self.disposition) is not AsyncAdmissionDisposition:
            raise ContractViolation("async admission disposition must be typed")


@dataclass(frozen=True, slots=True)
class WorkRetirementSummary:
    identity: AsyncWorkIdentity
    cause: str
    already_retired: bool

    def __post_init__(self) -> None:
        if type(self.identity) is not AsyncWorkIdentity:
            raise ContractViolation("retirement identity must be typed")
        require_identifier(self.cause, "retirement cause")
        if type(self.already_retired) is not bool:
            raise ContractViolation("retirement repeat flag is invalid")


class WorkCheck(StrEnum):
    READY = "ready"
    MISMATCH = "mismatch"
    FINISHED = "finished"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class AsyncWorkEvent:
    identity: AsyncWorkIdentity
    window: AsyncWorkWindow
    operation: str
    monotonic_ns: int
    cause: str = ""


@dataclass(frozen=True, slots=True)
class AsyncFactQueryEvidence:
    identity: AsyncWorkIdentity
    observation_sequence: int
    position: tuple[int, int, int]
    requirement_kind: str
    cell_fact: object
    acquired: bool


@dataclass(frozen=True, slots=True)
class AsyncOwnerDiagnostics:
    owner_instance_id: str
    active_identity: AsyncWorkIdentity | None
    active_window: AsyncWorkWindow | None
    events: tuple[AsyncWorkEvent, ...]
    admissions: tuple[AsyncAdmissionRecord, ...]
    resources: tuple[tuple[AsyncWorkIdentity | None, str], ...] = ()
    fact_queries: tuple[AsyncFactQueryEvidence, ...] = ()


class AsyncWorkLifecycle:
    """One authoritative active work; domains still validate and apply facts."""

    def __init__(self, *, history_limit: int = 64) -> None:
        if type(history_limit) is not int or not 1 <= history_limit <= 512:
            raise ContractViolation("async work history capacity is invalid")
        self.identity: AsyncWorkIdentity | None = None
        self.window: AsyncWorkWindow | None = None
        self.retirements: dict[AsyncWorkIdentity, WorkRetirementSummary] = {}
        self._history_limit = history_limit
        self._events: list[AsyncWorkEvent] = []
        self._scope: tuple[str, str, str] | None = None
        self._last_revisions: dict[AsyncWorkKind, int] = {}

    @property
    def events(self) -> tuple[AsyncWorkEvent, ...]:
        return tuple(self._events)

    def begin(self, identity: AsyncWorkIdentity, window: AsyncWorkWindow) -> None:
        if type(identity) is not AsyncWorkIdentity or type(window) is not AsyncWorkWindow:
            raise ContractViolation("async work begin requires identity and window")
        scope = (identity.world_session_id, identity.task_id, identity.owner_instance_id)
        if (self.identity is not None or identity in self.retirements
                or identity.revision <= self._last_revisions.get(identity.work_kind, 0)
                or (self._scope is not None and scope != self._scope)):
            raise ContractViolation("async work cannot restart an active or retired identity")
        self.identity, self.window = identity, window
        self._scope = scope
        self._last_revisions[identity.work_kind] = identity.revision
        self._append(AsyncWorkEvent(identity, window, "begin", window.started_monotonic_ns))

    def check(self, identity: AsyncWorkIdentity, now_ns: int) -> WorkCheck:
        if type(identity) is not AsyncWorkIdentity:
            raise ContractViolation("async work check requires identity")
        if type(now_ns) is not int or now_ns < 0:
            raise ContractViolation("async work processing clock is invalid")
        if identity != self.identity:
            return WorkCheck.FINISHED if identity in self.retirements else WorkCheck.MISMATCH
        assert self.window is not None
        return WorkCheck.EXPIRED if self.window.expired(now_ns) else WorkCheck.READY

    def finish(self, identity: AsyncWorkIdentity, cause: str, now_ns: int) -> WorkRetirementSummary:
        require_identifier(cause, "async work completion cause")
        self.check(identity, now_ns)
        previous = self.retirements.get(identity)
        if identity != self.identity:
            return WorkRetirementSummary(identity, previous.cause if previous else cause, True)
        assert self.window is not None
        summary = WorkRetirementSummary(identity, cause, False)
        self._append(AsyncWorkEvent(identity, self.window, "finish", now_ns, cause))
        if len(self.retirements) >= self._history_limit:
            self.retirements.pop(next(iter(self.retirements)))
        self.retirements[identity] = summary
        self.identity, self.window = None, None
        return summary

    def try_apply(self, identity: AsyncWorkIdentity, now_ns: int) -> bool:
        if self.check(identity, now_ns) is not WorkCheck.READY:
            return False
        assert self.window is not None
        self._append(AsyncWorkEvent(identity, self.window, "apply", now_ns))
        return True

    def _append(self, event: AsyncWorkEvent) -> None:
        if len(self._events) >= self._history_limit * 2:
            del self._events[0]
        self._events.append(event)
