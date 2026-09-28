"""Task-scoped retry counts, witnessed progress, and fixed wait windows."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mc2p.contracts.common import ContractViolation, require_identifier


class RetryCause(StrEnum):
    EXECUTION = "execution"
    DEPENDENCY = "dependency"
    INFORMATION = "information"
    ACQUISITION = "acquisition"
    PLANNING = "planning"


class RetryVerdict(StrEnum):
    RETRY = "retry"
    CAUSE_EXHAUSTED = "cause_exhausted"
    ROUND_EXHAUSTED = "round_exhausted"
    TASK_EXHAUSTED = "task_exhausted"


@dataclass(frozen=True, slots=True)
class FailureRegistration:
    verdict: RetryVerdict
    first_seen: bool


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
_MAX_ACTIVE_WAITS = 8
_MAX_BLOCKING_FACTS = 128


class RetryLedger:
    """One task owns the count; route, goal, and world revisions do not."""

    def __init__(self, task_id: str, *,
                 initial_support: tuple[int, int, int] | None = None) -> None:
        require_identifier(task_id, "retry task id")
        self.task_id = task_id
        self._failure_verdicts: dict[str, RetryVerdict] = {}
        self._terminal_verdict: RetryVerdict | None = None
        self._round_by_cause = {cause: 0 for cause in RetryCause}
        self._round_failures = 0
        self._total_failures = 0
        self._approved_round = 0
        self._approved_total = 0
        self._approved_by_cause = {cause: 0 for cause in RetryCause}
        self._visited_supports: set[tuple[int, int, int]] = set()
        if initial_support is not None:
            if (len(initial_support) != 3
                    or any(type(value) is not int for value in initial_support)):
                raise ContractViolation("initial retry support must be an integer cell")
            self._visited_supports.add(initial_support)
        self._completed_actions: set[str] = set()
        self._seen_facts: set[str] = set()
        self._blockers: set[str] = set()
        self._last_progress_sequence = -1
        self._waits: dict[str, WaitToken] = {}
        self._progress_capacity_exhausted = False
        self._last_failure_attempt_id: str | None = None
        self._last_progress_evidence: ProgressEvidence | None = None
        self._progress_version = 0

    @property
    def round_failures(self) -> int:
        return self._round_failures

    @property
    def total_failures(self) -> int:
        return self._total_failures

    @property
    def approved_round_retries(self) -> int:
        return self._approved_round

    @property
    def approved_total_retries(self) -> int:
        return self._approved_total

    @property
    def approved_cause_counts(self) -> tuple[tuple[RetryCause, int], ...]:
        return tuple((cause, self._approved_by_cause[cause])
                     for cause in RetryCause)

    @property
    def progress_capacity_exhausted(self) -> bool:
        return self._progress_capacity_exhausted

    @property
    def last_failure_attempt_id(self) -> str | None:
        return self._last_failure_attempt_id

    @property
    def last_progress_evidence(self) -> ProgressEvidence | None:
        return self._last_progress_evidence

    @property
    def progress_version(self) -> int:
        return self._progress_version

    @property
    def cause_counts(self) -> tuple[tuple[RetryCause, int], ...]:
        return tuple((cause, self._round_by_cause[cause]) for cause in RetryCause)

    def count_for(self, cause: RetryCause) -> int:
        return self._round_by_cause[cause]

    def record_failure(self, attempt_id: str, cause: RetryCause) -> FailureRegistration:
        require_identifier(attempt_id, "retry attempt id")
        if type(cause) is not RetryCause:
            raise ContractViolation("retry cause must be typed")
        if attempt_id in self._failure_verdicts:
            return FailureRegistration(self._failure_verdicts[attempt_id], False)
        if self._terminal_verdict is not None:
            return FailureRegistration(self._terminal_verdict, False)
        self._round_by_cause[cause] += 1
        self._round_failures += 1
        self._total_failures += 1
        if self._total_failures > 12:
            verdict = RetryVerdict.TASK_EXHAUSTED
        elif self._round_by_cause[cause] > 2:
            verdict = RetryVerdict.CAUSE_EXHAUSTED
        elif self._round_failures > 6:
            verdict = RetryVerdict.ROUND_EXHAUSTED
        else:
            verdict = RetryVerdict.RETRY
        self._failure_verdicts[attempt_id] = verdict
        self._last_failure_attempt_id = attempt_id
        if verdict is RetryVerdict.RETRY:
            self._approved_by_cause[cause] += 1
            self._approved_round += 1
            self._approved_total += 1
        if verdict is not RetryVerdict.RETRY:
            self._terminal_verdict = verdict
        return FailureRegistration(verdict, True)

    def set_blockers(self, fact_ids: tuple[str, ...]) -> None:
        if len(fact_ids) > _MAX_BLOCKING_FACTS:
            raise RetryLedgerCapacityExceeded("navigation blocking fact ledger is full")
        for fact_id in fact_ids:
            require_identifier(fact_id, "blocking fact id")
        self._blockers = set(fact_ids)

    def record_progress(self, evidence: ProgressEvidence) -> bool:
        if type(evidence) is not ProgressEvidence:
            raise ContractViolation("progress requires typed evidence")
        if evidence.observation_sequence < self._last_progress_sequence:
            return False
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
        self._round_by_cause = {cause: 0 for cause in RetryCause}
        self._round_failures = 0
        self._approved_by_cause = {cause: 0 for cause in RetryCause}
        self._approved_round = 0
        return True

    def _progress_full(self) -> bool:
        full = (len(self._visited_supports) + len(self._completed_actions)
                + len(self._seen_facts) >= _MAX_PROGRESS_IDENTITIES)
        self._progress_capacity_exhausted |= full
        return full

    def begin_wait(self, wait_id: str, policy: WaitPolicy,
                   movement_tick: int, monotonic_ns: int) -> WaitToken:
        require_identifier(wait_id, "wait id")
        if type(policy) is not WaitPolicy:
            raise ContractViolation("wait policy must be typed")
        if (type(movement_tick) is not int or movement_tick < 0
                or type(monotonic_ns) is not int or monotonic_ns < 0):
            raise ContractViolation("wait start requires valid clocks")
        token = self._waits.get(wait_id)
        if token is None:
            if len(self._waits) >= _MAX_ACTIVE_WAITS:
                raise RetryLedgerCapacityExceeded("active navigation wait ledger is full")
            token = WaitToken(wait_id, policy, movement_tick, monotonic_ns)
            self._waits[wait_id] = token
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

    def end_wait(self, wait_id: str) -> None:
        self._waits.pop(wait_id, None)
