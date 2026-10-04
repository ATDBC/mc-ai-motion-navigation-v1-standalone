"""Task-scoped retry counts, witnessed progress, and fixed wait windows."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Callable

from mc2p.contracts.common import ContractViolation, require_identifier


class RetryCause(StrEnum):
    EXECUTION = "execution"
    DEPENDENCY = "dependency"
    INFORMATION = "information"
    ACQUISITION = "acquisition"
    PLANNING = "planning"


class LocalAttemptVerdict(StrEnum):
    """Result of one owner-local rebuild chain."""

    RETRY = "retry"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class LocalAttemptRegistration:
    verdict: LocalAttemptVerdict
    first_seen: bool


class LocalAttemptChain:
    """A small idempotent bound for one action or planning owner.

    Attempt labels identify duplicate deliveries only.  They do not classify
    the failure and therefore cannot create a new allowance when the cause or
    worker revision changes.
    """

    def __init__(self, *, maximum_failures: int = 3) -> None:
        if (type(maximum_failures) is not int
                or not 1 <= maximum_failures <= 8):
            raise ContractViolation(
                "local attempt limit must be within the fixed capacity"
            )
        self.maximum_failures = maximum_failures
        self._results: dict[str, LocalAttemptVerdict] = {}
        self._exhausted = False

    @property
    def failure_count(self) -> int:
        return len(self._results)

    def record(self, attempt_id: str) -> LocalAttemptRegistration:
        require_identifier(attempt_id, "local attempt id")
        known = self._results.get(attempt_id)
        if known is not None:
            return LocalAttemptRegistration(known, False)
        if self._exhausted:
            return LocalAttemptRegistration(LocalAttemptVerdict.EXHAUSTED, False)
        verdict = (
            LocalAttemptVerdict.EXHAUSTED
            if len(self._results) + 1 >= self.maximum_failures
            else LocalAttemptVerdict.RETRY
        )
        self._results[attempt_id] = verdict
        self._exhausted = verdict is LocalAttemptVerdict.EXHAUSTED
        return LocalAttemptRegistration(verdict, True)

    def reset(self) -> None:
        self._results.clear()
        self._exhausted = False


class RecoveryBudgetKind(StrEnum):
    FINITE = "finite"
    PERSISTENT = "persistent"


_MAX_TASK_RECOVERIES = 12


@dataclass(frozen=True, slots=True)
class RecoveryBudgetPolicy:
    """The two task-level recovery policies currently supported by navigation."""

    kind: RecoveryBudgetKind
    maximum_recoveries: int
    recovery_window_ns: int | None = None
    maximum_recovery_ns: int | None = None
    maximum_no_progress_ns: int | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not RecoveryBudgetKind:
            raise ContractViolation("recovery budget kind must be typed")
        if (type(self.maximum_recoveries) is not int
                or not 0 < self.maximum_recoveries <= _MAX_TASK_RECOVERIES):
            raise ContractViolation(
                "recovery budget count must be within the frozen task maximum"
            )
        persistent_limits = (
            self.recovery_window_ns,
            self.maximum_recovery_ns,
            self.maximum_no_progress_ns,
        )
        if self.kind is RecoveryBudgetKind.FINITE:
            if any(value is not None for value in persistent_limits):
                raise ContractViolation("finite recovery budget cannot have time limits")
        elif any(type(value) is not int or value <= 0
                 for value in persistent_limits):
            raise ContractViolation("persistent recovery budget requires positive time limits")

    @classmethod
    def finite(cls, *, maximum_recoveries: int = 12) -> "RecoveryBudgetPolicy":
        return cls(RecoveryBudgetKind.FINITE, maximum_recoveries)

    @classmethod
    def persistent(
        cls, *, maximum_recoveries: int = 12,
        recovery_window_ns: int = 60_000_000_000,
        maximum_recovery_ns: int = 10_000_000_000,
        maximum_no_progress_ns: int = 30_000_000_000,
    ) -> "RecoveryBudgetPolicy":
        return cls(
            RecoveryBudgetKind.PERSISTENT,
            maximum_recoveries,
            recovery_window_ns,
            maximum_recovery_ns,
            maximum_no_progress_ns,
        )


class RecoveryLimitStatus(StrEnum):
    ALLOWED = "allowed"
    FINITE_TOTAL_EXHAUSTED = "finite_total_exhausted"
    PERSISTENT_RATE_EXHAUSTED = "persistent_rate_exhausted"
    SINGLE_RECOVERY_EXHAUSTED = "single_recovery_exhausted"
    NO_PROGRESS_EXHAUSTED = "no_progress_exhausted"
    TARGET_ACTIONS_STOPPED = "target_actions_stopped"


@dataclass(frozen=True, slots=True)
class RecoveryIdentity:
    """A task-local ordered identity; labels are diagnostic only."""

    sequence: int
    label: str | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ContractViolation("recovery sequence must be a non-negative integer")
        if self.label is not None:
            require_identifier(self.label, "recovery diagnostic label")


@dataclass(frozen=True, slots=True)
class RecoveryStartRegistration:
    status: RecoveryLimitStatus
    first_seen: bool


class RecoveryFinishKind(StrEnum):
    BODY_HANDOFF = "body_handoff"
    SAFE_RELEASE = "safe_release"
    RESPONSIBILITY_TRANSFERRED = "responsibility_transferred"


@dataclass(frozen=True, slots=True)
class RecoveryFinishEvidence:
    kind: RecoveryFinishKind

    def __post_init__(self) -> None:
        if type(self.kind) is not RecoveryFinishKind:
            raise ContractViolation("recovery finish kind must be typed")


class TaskDemandState(StrEnum):
    UNMET = "unmet"
    STABLE_SATISFIED = "stable_satisfied"
    TERMINAL_CLEANUP = "terminal_cleanup"


class ProgressKind(StrEnum):
    ROUTE_FRONTIER = "route_frontier"
    ACTION_COMPLETED = "action_completed"
    BLOCKING_FACT = "blocking_fact"


@dataclass(frozen=True, slots=True)
class ProgressEvidence:
    kind: ProgressKind
    observation_sequence: int
    support: tuple[int, int, int] | None = None
    valid_corridor: tuple[tuple[int, int, int], ...] = ()
    action_id: str | None = None
    fact_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not ProgressKind:
            raise ContractViolation("progress kind must be typed")
        if type(self.observation_sequence) is not int or self.observation_sequence < 0:
            raise ContractViolation("progress observation sequence is invalid")
        if self.kind is ProgressKind.ROUTE_FRONTIER:
            if (self.support is None or len(self.support) != 3
                    or any(type(value) is not int for value in self.support)
                    or any(len(node) != 3 or any(type(value) is not int
                              for value in node) for node in self.valid_corridor)):
                raise ContractViolation("route progress requires a support and corridor")
        elif self.kind is ProgressKind.ACTION_COMPLETED:
            require_identifier(self.action_id, "completed action id")
        else:
            require_identifier(self.fact_id, "blocking fact id")


@dataclass(frozen=True, slots=True)
class WaitPolicy:
    maximum_movement_ticks: int
    maximum_elapsed_ns: int

    def __post_init__(self) -> None:
        if (type(self.maximum_movement_ticks) is not int
                or self.maximum_movement_ticks <= 0
                or type(self.maximum_elapsed_ns) is not int
                or self.maximum_elapsed_ns <= 0):
            raise ContractViolation("wait policy requires positive limits")


@dataclass(frozen=True, slots=True)
class WaitToken:
    wait_id: str
    owner_id: str
    policy: WaitPolicy
    started_movement_tick: int
    started_monotonic_ns: int


class WaitVerdict(StrEnum):
    WAITING = "waiting"
    EXHAUSTED_TICKS = "exhausted_ticks"
    EXHAUSTED_CLOCK = "exhausted_clock"


class RetryLedgerCapacityExceeded(RuntimeError):
    """The fixed task ledger is full; existing responsibility is preserved."""


_MAX_PROGRESS_IDENTITIES = 4096
_MAX_RECENT_PROGRESS_IDENTITIES = 8192
_MAX_ACTIVE_WAITS = 8
_MAX_BLOCKING_FACTS = 128


class RetryLedger:
    """One task owns the count; route, goal, and world revisions do not."""

    def __init__(
        self, task_id: str, *,
        initial_support: tuple[int, int, int] | None = None,
        policy: RecoveryBudgetPolicy | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        require_identifier(task_id, "retry task id")
        if policy is not None and type(policy) is not RecoveryBudgetPolicy:
            raise ContractViolation("retry ledger policy must be typed")
        if not callable(clock_ns):
            raise ContractViolation("retry ledger clock must be callable")
        self.task_id = task_id
        self._policy = policy or RecoveryBudgetPolicy.finite()
        self._clock_ns = clock_ns
        initial_now_ns = clock_ns()
        if type(initial_now_ns) is not int or initial_now_ns < 0:
            raise ContractViolation("retry ledger clock must return non-negative integer nanoseconds")
        self._last_clock_ns = initial_now_ns
        self._limit_status = RecoveryLimitStatus.ALLOWED
        self._active_recovery_identity: RecoveryIdentity | None = None
        self._active_recovery_started_ns: int | None = None
        self._recovery_starts: deque[tuple[int, int]] = deque()
        self._recovery_starts_at_limit_decision: int | None = None
        self._recovery_results: dict[int, RecoveryLimitStatus] = {}
        self._recovery_high_water = -1
        self._total_recovery_starts = 0
        self._recovery_cause_counts = {cause: 0 for cause in RetryCause}
        self._demand_state = TaskDemandState.UNMET
        self._last_true_progress_ns: int | None = initial_now_ns
        self._terminal_cleanup_no_progress_elapsed_ns: int | None = None
        self._recent_progress: dict[tuple[object, ...], int] = {}
        self._visited_supports: set[tuple[int, int, int]] = set()
        if initial_support is not None:
            if (len(initial_support) != 3
                    or any(type(value) is not int for value in initial_support)):
                raise ContractViolation("initial retry support must be an integer cell")
            self._visited_supports.add(initial_support)
            if self._policy.kind is RecoveryBudgetKind.PERSISTENT:
                self._recent_progress[(ProgressKind.ROUTE_FRONTIER, initial_support)] = initial_now_ns
        self._completed_actions: set[str] = set()
        self._seen_facts: set[str] = set()
        self._blockers: set[str] = set()
        self._last_progress_sequence = -1
        self._waits: dict[str, WaitToken] = {}
        self._progress_capacity_exhausted = False
        self._last_progress_evidence: ProgressEvidence | None = None
        self._progress_version = 0

    @property
    def policy(self) -> RecoveryBudgetPolicy:
        return self._policy

    @property
    def active_recovery_id(self) -> RecoveryIdentity | None:
        return self._active_recovery_identity

    @property
    def active_recovery_started_ns(self) -> int | None:
        return self._active_recovery_started_ns

    @property
    def total_recovery_starts(self) -> int:
        return self._total_recovery_starts

    @property
    def last_true_progress_ns(self) -> int | None:
        return self._last_true_progress_ns

    @property
    def recovery_limit_status(self) -> RecoveryLimitStatus:
        return self._limit_status

    @property
    def recovery_cause_counts(self) -> tuple[tuple[RetryCause, int], ...]:
        return tuple((cause, self._recovery_cause_counts[cause])
                     for cause in RetryCause)

    @property
    def recovery_starts_in_window(self) -> int:
        now_ns = self._task_now_ns()
        self._prune_recovery_history(now_ns)
        return len(self._recovery_starts)

    @property
    def recovery_starts_at_limit_decision(self) -> int | None:
        """Return the frozen window count that caused rate exhaustion."""
        return self._recovery_starts_at_limit_decision

    @property
    def recent_progress_identity_count(self) -> int:
        return len(self._recent_progress)

    @property
    def remembered_recovery_result_count(self) -> int:
        return len(self._recovery_results)

    @property
    def progress_capacity_exhausted(self) -> bool:
        return self._progress_capacity_exhausted

    @property
    def last_progress_evidence(self) -> ProgressEvidence | None:
        return self._last_progress_evidence

    @property
    def progress_version(self) -> int:
        return self._progress_version

    def begin_recovery(
        self, identity: RecoveryIdentity, cause: RetryCause,
    ) -> RecoveryStartRegistration:
        """Start one charged recovery cycle, or repeat its stable identity."""
        if type(identity) is not RecoveryIdentity:
            raise ContractViolation("recovery identity must be typed")
        if type(cause) is not RetryCause:
            raise ContractViolation("retry cause must be typed")
        now_ns = self._task_now_ns()
        known = self._recovery_results.get(identity.sequence)
        if known is not None:
            return RecoveryStartRegistration(known, False)
        if identity.sequence <= self._recovery_high_water:
            return RecoveryStartRegistration(RecoveryLimitStatus.ALLOWED, False)
        if self._demand_state is TaskDemandState.TERMINAL_CLEANUP:
            return RecoveryStartRegistration(
                RecoveryLimitStatus.TARGET_ACTIONS_STOPPED, False,
            )
        if self._active_recovery_identity is not None:
            raise ContractViolation("another recovery identity is already active")
        if self._limit_status is not RecoveryLimitStatus.ALLOWED:
            return RecoveryStartRegistration(self._limit_status, False)

        self._prune_recovery_history(now_ns)
        if len(self._recovery_starts) >= self._policy.maximum_recoveries:
            status = (
                RecoveryLimitStatus.FINITE_TOTAL_EXHAUSTED
                if self._policy.kind is RecoveryBudgetKind.FINITE
                else RecoveryLimitStatus.PERSISTENT_RATE_EXHAUSTED
            )
            self._limit_status = status
            if status is RecoveryLimitStatus.PERSISTENT_RATE_EXHAUSTED:
                self._recovery_starts_at_limit_decision = len(
                    self._recovery_starts
                )
            self._recovery_high_water = identity.sequence
            self._recovery_results[identity.sequence] = status
            return RecoveryStartRegistration(status, True)

        self._recovery_high_water = identity.sequence
        self._recovery_starts.append((identity.sequence, now_ns))
        self._total_recovery_starts += 1
        self._recovery_results[identity.sequence] = RecoveryLimitStatus.ALLOWED
        self._active_recovery_identity = identity
        self._active_recovery_started_ns = now_ns
        self._recovery_cause_counts[cause] += 1
        return RecoveryStartRegistration(RecoveryLimitStatus.ALLOWED, True)

    def record_recovery_cause(
        self, identity: RecoveryIdentity, cause: RetryCause,
    ) -> None:
        """Record a diagnostic within the active cycle without buying another cycle."""
        if type(identity) is not RecoveryIdentity:
            raise ContractViolation("recovery identity must be typed")
        if type(cause) is not RetryCause:
            raise ContractViolation("retry cause must be typed")
        self._task_now_ns()
        if (self._active_recovery_identity is None
                or identity.sequence != self._active_recovery_identity.sequence):
            raise ContractViolation("recovery diagnostic identity is not active")
        self._recovery_cause_counts[cause] += 1

    def finish_recovery(
        self, identity: RecoveryIdentity, evidence: RecoveryFinishEvidence,
    ) -> None:
        """Finish only after the caller supplies typed handoff or release evidence."""
        if type(identity) is not RecoveryIdentity:
            raise ContractViolation("recovery identity must be typed")
        if type(evidence) is not RecoveryFinishEvidence:
            raise ContractViolation("recovery finish evidence must be typed")
        now_ns = self._task_now_ns()
        if (self._active_recovery_identity is None
                or identity.sequence != self._active_recovery_identity.sequence):
            raise ContractViolation("recovery finish identity is not active")
        self._check_task_limits(now_ns)
        self._active_recovery_identity = None
        self._active_recovery_started_ns = None

    def observe_task_activity(
        self, demand_state: TaskDemandState,
        progress: ProgressEvidence | None = None,
    ) -> bool:
        """Absorb the frame's formal demand and progress before checking limits."""
        accepted = self.record_task_activity(demand_state, progress)
        self.finalize_task_activity()
        return accepted

    def observe_task_activity_status(
        self, demand_state: TaskDemandState,
        progress: ProgressEvidence | None = None,
    ) -> RecoveryLimitStatus:
        """Return the typed task limit after absorbing this frame's evidence."""
        self.record_task_activity(demand_state, progress)
        return self.finalize_task_activity()

    def record_task_activity(
        self, demand_state: TaskDemandState,
        progress: ProgressEvidence | None = None,
    ) -> bool:
        """Collect demand/progress without making this frame's final limit decision."""
        if type(demand_state) is not TaskDemandState:
            raise ContractViolation("task demand state must be typed")
        if progress is not None and type(progress) is not ProgressEvidence:
            raise ContractViolation("progress requires typed evidence")
        now_ns = self._task_now_ns()
        if (self._demand_state is TaskDemandState.TERMINAL_CLEANUP
                and demand_state is not TaskDemandState.TERMINAL_CLEANUP):
            raise ContractViolation("terminal cleanup demand state is immutable")
        if self._limit_status is not RecoveryLimitStatus.ALLOWED:
            return False
        if demand_state is not self._demand_state:
            previous_demand = self._demand_state
            if (previous_demand is TaskDemandState.UNMET
                    and demand_state is TaskDemandState.TERMINAL_CLEANUP
                    and self._policy.kind is RecoveryBudgetKind.PERSISTENT
                    and self._last_true_progress_ns is not None):
                self._check_task_limits(now_ns)
                self._terminal_cleanup_no_progress_elapsed_ns = max(
                    0, now_ns - self._last_true_progress_ns,
                )
            elif (demand_state is TaskDemandState.TERMINAL_CLEANUP
                  and self._policy.kind is RecoveryBudgetKind.PERSISTENT):
                self._terminal_cleanup_no_progress_elapsed_ns = 0
            self._demand_state = demand_state
            self._recent_progress.clear()
            self._last_progress_sequence = -1
            self._last_true_progress_ns = now_ns if demand_state is TaskDemandState.UNMET else None

        accepted = False
        if progress is not None and demand_state is TaskDemandState.UNMET:
            deadline = self._policy.maximum_no_progress_ns
            already_late = (
                self._policy.kind is RecoveryBudgetKind.PERSISTENT
                and deadline is not None
                and self._last_true_progress_ns is not None
                and now_ns - self._last_true_progress_ns > deadline
            )
            if not already_late:
                accepted = self._record_progress_at(progress, now_ns)

        return accepted

    def finalize_task_activity(self) -> RecoveryLimitStatus:
        """Make the current frame's limit decision after all evidence is collected."""
        return self._check_task_limits(self._task_now_ns())

    def current_limit_status(self) -> RecoveryLimitStatus:
        """Recheck the task clock immediately before a recovery is charged."""
        return self._check_task_limits(self._task_now_ns())

    def can_resume_same_task_continuation(self) -> bool:
        """Recheck the task clock before issuing continuation evidence."""
        now_ns = self._task_now_ns()
        self._check_task_limits(now_ns)
        if (self._limit_status is not RecoveryLimitStatus.ALLOWED
                or self._active_recovery_identity is not None
                or self._waits):
            return False
        if self._demand_state not in {
            TaskDemandState.UNMET,
            TaskDemandState.STABLE_SATISFIED,
            TaskDemandState.TERMINAL_CLEANUP,
        }:
            raise ContractViolation(
                "same-task continuation requires terminal cleanup state"
            )
        return True

    def resume_same_task_continuation(self) -> bool:
        """Resume one released Session shell without resetting task history."""
        now_ns = self._task_now_ns()
        self._check_task_limits(now_ns)
        if (self._limit_status is not RecoveryLimitStatus.ALLOWED
                or self._active_recovery_identity is not None
                or self._waits):
            return False
        if self._demand_state not in {
            TaskDemandState.UNMET,
            TaskDemandState.STABLE_SATISFIED,
            TaskDemandState.TERMINAL_CLEANUP,
        }:
            raise ContractViolation(
                "same-task continuation requires terminal cleanup state"
            )
        previous_demand = self._demand_state
        self._demand_state = TaskDemandState.UNMET
        if previous_demand is TaskDemandState.TERMINAL_CLEANUP:
            elapsed = self._terminal_cleanup_no_progress_elapsed_ns
            if self._policy.kind is RecoveryBudgetKind.PERSISTENT:
                if elapsed is None:
                    raise ContractViolation(
                        "persistent continuation lost its no-progress clock"
                    )
                self._last_true_progress_ns = now_ns - elapsed
            elif self._last_true_progress_ns is None:
                self._last_true_progress_ns = now_ns
        elif previous_demand is TaskDemandState.STABLE_SATISFIED:
            self._last_true_progress_ns = now_ns
        self._terminal_cleanup_no_progress_elapsed_ns = None
        return True

    def _check_task_limits(self, now_ns: int) -> RecoveryLimitStatus:
        if self._policy.kind is RecoveryBudgetKind.FINITE:
            return self._limit_status
        assert self._policy.maximum_recovery_ns is not None
        assert self._policy.maximum_no_progress_ns is not None
        if (self._active_recovery_started_ns is not None
                and now_ns - self._active_recovery_started_ns
                >= self._policy.maximum_recovery_ns):
            self._limit_status = RecoveryLimitStatus.SINGLE_RECOVERY_EXHAUSTED
        elif (self._demand_state is TaskDemandState.UNMET
              and self._last_true_progress_ns is not None
              and now_ns - self._last_true_progress_ns
              >= self._policy.maximum_no_progress_ns):
            self._limit_status = RecoveryLimitStatus.NO_PROGRESS_EXHAUSTED
        return self._limit_status

    def _task_now_ns(self) -> int:
        now_ns = self._clock_ns()
        if type(now_ns) is not int or now_ns < 0:
            raise ContractViolation("retry ledger clock must return non-negative integer nanoseconds")
        if now_ns < self._last_clock_ns:
            raise ContractViolation("retry ledger task clock moved backwards")
        self._last_clock_ns = now_ns
        return now_ns

    def _prune_recovery_history(self, now_ns: int) -> None:
        if self._policy.kind is RecoveryBudgetKind.FINITE:
            return
        assert self._policy.recovery_window_ns is not None
        cutoff = now_ns - self._policy.recovery_window_ns
        while self._recovery_starts and self._recovery_starts[0][1] <= cutoff:
            recovery_sequence, _ = self._recovery_starts.popleft()
            if (self._active_recovery_identity is None
                    or recovery_sequence != self._active_recovery_identity.sequence):
                self._recovery_results.pop(recovery_sequence, None)

    def set_blockers(self, fact_ids: tuple[str, ...]) -> None:
        if len(fact_ids) > _MAX_BLOCKING_FACTS:
            raise RetryLedgerCapacityExceeded("navigation blocking fact ledger is full")
        for fact_id in fact_ids:
            require_identifier(fact_id, "blocking fact id")
        self._blockers = set(fact_ids)

    def record_progress(self, evidence: ProgressEvidence) -> bool:
        now_ns = self._task_now_ns()
        return self._record_progress_at(evidence, now_ns)

    def _record_progress_at(self, evidence: ProgressEvidence, now_ns: int) -> bool:
        if type(evidence) is not ProgressEvidence:
            raise ContractViolation("progress requires typed evidence")
        if evidence.observation_sequence < self._last_progress_sequence:
            return False
        if self._policy.kind is RecoveryBudgetKind.PERSISTENT:
            return self._record_persistent_progress(evidence, now_ns)
        same_sample = evidence.observation_sequence == self._last_progress_sequence
        if evidence.kind is ProgressKind.ROUTE_FRONTIER:
            key = evidence.support
            if key not in evidence.valid_corridor or key in self._visited_supports:
                return False
            if self._progress_full():
                return False
            self._visited_supports.add(key)
        elif evidence.kind is ProgressKind.ACTION_COMPLETED:
            key = evidence.action_id
            if key in self._completed_actions:
                return False
            if self._progress_full():
                return False
            self._completed_actions.add(key)
        else:
            key = evidence.fact_id
            if key not in self._blockers or key in self._seen_facts:
                return False
            if self._progress_full():
                return False
            self._seen_facts.add(key)
            self._blockers.remove(key)
        if same_sample:
            # Another fact from the same observation is remembered, but one
            # sample cannot grant several fresh retry rounds.
            return False
        self._last_progress_sequence = evidence.observation_sequence
        self._last_progress_evidence = evidence
        self._progress_version += 1
        return True

    def _record_persistent_progress(
        self, evidence: ProgressEvidence, now_ns: int,
    ) -> bool:
        same_sample = evidence.observation_sequence == self._last_progress_sequence
        key: tuple[object, ...]
        if evidence.kind is ProgressKind.ROUTE_FRONTIER:
            if evidence.support not in evidence.valid_corridor:
                return False
            key = (evidence.kind, evidence.support)
        elif evidence.kind is ProgressKind.ACTION_COMPLETED:
            key = (evidence.kind, evidence.action_id)
        else:
            if evidence.fact_id not in self._blockers:
                return False
            key = (evidence.kind, evidence.fact_id)
        self._prune_recent_progress(now_ns)
        if key in self._recent_progress:
            return False
        if len(self._recent_progress) >= _MAX_RECENT_PROGRESS_IDENTITIES:
            self._progress_capacity_exhausted = True
            raise RetryLedgerCapacityExceeded(
                "recent navigation progress ledger is full"
            )
        self._recent_progress[key] = now_ns
        if evidence.kind is ProgressKind.BLOCKING_FACT:
            assert evidence.fact_id is not None
            self._blockers.remove(evidence.fact_id)
        if same_sample:
            return False
        self._last_progress_sequence = evidence.observation_sequence
        self._last_progress_evidence = evidence
        self._last_true_progress_ns = now_ns
        self._progress_version += 1
        return True

    def _prune_recent_progress(self, now_ns: int) -> None:
        assert self._policy.maximum_no_progress_ns is not None
        cutoff = now_ns - self._policy.maximum_no_progress_ns
        expired = tuple(
            key for key, seen_ns in self._recent_progress.items()
            if seen_ns < cutoff
        )
        for key in expired:
            del self._recent_progress[key]

    def _progress_full(self) -> bool:
        full = (len(self._visited_supports) + len(self._completed_actions)
                + len(self._seen_facts) >= _MAX_PROGRESS_IDENTITIES)
        self._progress_capacity_exhausted |= full
        return full

    def begin_wait(self, wait_id: str, owner_id: str, policy: WaitPolicy,
                   movement_tick: int, monotonic_ns: int) -> WaitToken:
        require_identifier(wait_id, "wait id")
        require_identifier(owner_id, "wait owner id")
        if type(policy) is not WaitPolicy:
            raise ContractViolation("wait policy must be typed")
        if (type(movement_tick) is not int or movement_tick < 0
                or type(monotonic_ns) is not int or monotonic_ns < 0):
            raise ContractViolation("wait start requires valid clocks")
        token = self._waits.get(wait_id)
        if token is None:
            if len(self._waits) >= _MAX_ACTIVE_WAITS:
                raise RetryLedgerCapacityExceeded("active navigation wait ledger is full")
            token = WaitToken(
                wait_id, owner_id, policy, movement_tick, monotonic_ns,
            )
            self._waits[wait_id] = token
        elif token.owner_id != owner_id:
            raise ContractViolation(
                "active navigation wait cannot change owner"
            )
        return token

    def check_wait(self, wait_id: str, movement_tick: int,
                   monotonic_ns: int) -> WaitVerdict:
        token = self._waits[wait_id]
        if movement_tick < token.started_movement_tick or monotonic_ns < token.started_monotonic_ns:
            raise ContractViolation("wait clock moved backwards")
        if movement_tick - token.started_movement_tick >= token.policy.maximum_movement_ticks:
            return WaitVerdict.EXHAUSTED_TICKS
        if monotonic_ns - token.started_monotonic_ns >= token.policy.maximum_elapsed_ns:
            return WaitVerdict.EXHAUSTED_CLOCK
        return WaitVerdict.WAITING

    def end_wait_owned(self, wait_id: str, owner_id: str) -> bool:
        """End one wait only when the caller presents its owning identity."""
        require_identifier(wait_id, "wait id")
        require_identifier(owner_id, "wait owner id")
        token = self._waits.get(wait_id)
        if token is None:
            return False
        if token.owner_id != owner_id:
            raise ContractViolation("active navigation wait owner does not match")
        del self._waits[wait_id]
        return True

    def end_owner_waits(self, owner_id: str) -> tuple[str, ...]:
        """Close every live wait when its owning action leaves the task."""
        require_identifier(owner_id, "wait owner id")
        ended = tuple(sorted(
            wait_id for wait_id, token in self._waits.items()
            if token.owner_id == owner_id
        ))
        for wait_id in ended:
            del self._waits[wait_id]
        return ended

    def active_waits(self, owner_id: str | None = None) -> tuple[WaitToken, ...]:
        """Return an immutable diagnostic view without transferring ownership."""
        if owner_id is not None:
            require_identifier(owner_id, "wait owner id")
        return tuple(
            self._waits[wait_id]
            for wait_id in sorted(self._waits)
            if owner_id is None or self._waits[wait_id].owner_id == owner_id
        )
