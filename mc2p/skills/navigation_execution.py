"""Bounded execution facts shared by goal-directed navigation and its Driver.

This state never authorizes an action. Driver must associate an event with the
actual action, source and post-observation before reporting progress. Queries
are pure; deadline handling and resolution are explicit state transitions.
"""
from __future__ import annotations

import math

from mc2p.contracts.common import ContractViolation, require_nonnegative_int


EXECUTION_RECOVERY_REVISION = 1
TASK_TIMEOUT_NS = 90_000_000_000
PROBLEM_TIMEOUT_NS = 12_000_000_000
NO_PROGRESS_TIMEOUT_NS = 3_000_000_000
MAX_ATTEMPTS = 12
PHASES = frozenset((
    "tracking", "adjusting", "observing", "retreating", "replanning",
    "waiting", "blocked", "suspended",
))
_RECOVERY_PHASES = PHASES - {"tracking", "blocked", "suspended"}


def _text(value: str, name: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > 256:
        raise ContractViolation(name + " must be a nonempty string of at most 256 characters")


class NavigationExecution:
    """One task/source binding, one active problem and O(1) event history.

    ``event_id`` and explicit ``attempt_id`` are monotonic nonnegative integers
    within a binding. A stable attempt ID groups consecutive actual actions of
    the same recovery attempt. Older attempt IDs never create a new allowance.
    Omitting ``binding`` declares that the caller already checked the binding;
    callers with an untrusted or delayed event must pass its original binding.
    """

    def __init__(
        self, *, task_timeout_ns: int = TASK_TIMEOUT_NS,
        problem_timeout_ns: int = PROBLEM_TIMEOUT_NS,
        no_progress_timeout_ns: int = NO_PROGRESS_TIMEOUT_NS,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        for name, value in (
            ("task timeout", task_timeout_ns), ("problem timeout", problem_timeout_ns),
            ("no progress timeout", no_progress_timeout_ns), ("maximum attempts", max_attempts),
        ):
            require_nonnegative_int(value, name)
            if value == 0:
                raise ContractViolation(name + " must be positive")
        self.task_timeout_ns = task_timeout_ns
        self.problem_timeout_ns = problem_timeout_ns
        self.no_progress_timeout_ns = no_progress_timeout_ns
        self.max_attempts = max_attempts
        self.phase = "suspended"
        self.started_ns: int | None = None
        self.problem_started_ns: int | None = None
        self.last_progress_ns: int | None = None
        self.attempts = 0
        self.revision = 0
        self._binding: object = None
        self._last_now_ns: int | None = None
        self._feedback_high_water = -1
        self._attempt_high_water = -1
        self._reason: str | None = None
        self._problem_key: str | None = None
        self._last_position: tuple[float, float, float] | None = None
        self._last_executed: bool | None = None

    def _check_time(self, now_ns: int) -> None:
        require_nonnegative_int(now_ns, "execution time")
        if self._last_now_ns is not None and now_ns < self._last_now_ns:
            raise ContractViolation("execution time regression")

    def _active(self) -> None:
        if self.started_ns is None:
            raise ContractViolation("execution binding has not begun")

    def begin(self, now_ns: int, binding: object) -> None:
        """Begin a new binding; repeating the current binding cannot reset it."""
        self._check_time(now_ns)
        if binding is None:
            raise ContractViolation("execution binding is required")
        try:
            hash(binding)
        except TypeError as exc:
            raise ContractViolation("execution binding must be immutable") from exc
        if self.started_ns is not None and binding == self._binding:
            return
        self._binding = binding
        self.started_ns = self.last_progress_ns = self._last_now_ns = now_ns
        self.problem_started_ns = None
        self.phase = "tracking"
        self.attempts = 0
        self._feedback_high_water = self._attempt_high_water = -1
        self._reason = self._problem_key = None
        self._last_position = self._last_executed = None
        self.revision += 1

    def problem(self, now_ns: int, reason: str, key: str) -> None:
        """Record a problem without renewing its first trigger or progress."""
        self._active()
        self._check_time(now_ns)
        _text(reason, "execution reason")
        _text(key, "problem key")
        changed = (self.problem_started_ns is None or self._reason != reason
                   or self._problem_key != key or self.phase == "tracking")
        if self.problem_started_ns is None:
            self.problem_started_ns = now_ns
        self._reason, self._problem_key = reason, key
        if self.phase == "tracking":
            self.phase = "adjusting"
        self._last_now_ns = now_ns
        if changed:
            self.revision += 1

    def set_phase(self, phase: str, now_ns: int, reason: str | None = None) -> None:
        """Select a named mode; this never resolves a problem or renews time."""
        self._active()
        self._check_time(now_ns)
        if type(phase) is not str or phase not in PHASES:
            raise ContractViolation("unknown navigation execution phase")
        if reason is not None:
            _text(reason, "execution reason")
        if self.phase == "blocked" and phase != "suspended":
            return
        changed = self.phase != phase or (reason is not None and self._reason != reason)
        self.phase = phase
        if reason is not None:
            self._reason = reason
        self._last_now_ns = now_ns
        if changed:
            self.revision += 1

    def resolve(self, now_ns: int) -> None:
        """Caller confirms a useful connection/motion, ending this local problem.

        Intermediate turning or retreat progress is insufficient. This method
        preserves task start, cumulative attempt counts and event high waters.
        """
        self._active()
        self._check_time(now_ns)
        if self.phase in {"blocked", "suspended"}:
            return
        self.problem_started_ns = None
        self._reason = self._problem_key = None
        self.phase = "tracking"
        self._last_now_ns = now_ns
        self.revision += 1

    def observe_feedback(
        self, event_id: int, now_ns: int, executed: bool,
        position: tuple[float, float, float] | None, progress: bool,
        reason: str | None, *, binding: object = None, attempt_id: int | None = None,
    ) -> bool:
        """Accept a matching new fact once; return False for a stale/mismatched fact.

        Attempts count only actions actually executed while recovering. The
        initial failure while tracking starts a problem, not a recovery attempt.
        An accepted unexecuted event records disposition but never progress.
        """
        self._active()
        require_nonnegative_int(event_id, "execution event ID")
        require_nonnegative_int(now_ns, "execution time")
        if attempt_id is not None:
            require_nonnegative_int(attempt_id, "execution attempt ID")
        if type(executed) is not bool or type(progress) is not bool:
            raise ContractViolation("executed and progress must be booleans")
        if reason is not None:
            _text(reason, "execution reason")
        if position is not None:
            if (type(position) not in (tuple, list) or len(position) != 3
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in position)):
                raise ContractViolation("execution position must contain three finite coordinates")
            position = tuple(float(v) for v in position)
        if ((binding is not None and binding != self._binding)
                or event_id <= self._feedback_high_water
                or now_ns < self._last_now_ns):
            return False
        self._feedback_high_water = event_id
        self._last_now_ns = now_ns
        self._last_executed = executed
        if executed:
            self._last_position = position
            actual_attempt = event_id if attempt_id is None else attempt_id
            if self.phase in _RECOVERY_PHASES and actual_attempt > self._attempt_high_water:
                self.attempts += 1
                self._attempt_high_water = actual_attempt
            if progress:
                self.last_progress_ns = now_ns
            elif reason is not None and self.problem_started_ns is None:
                self.problem(now_ns, reason, "feedback")
            if reason is not None:
                self._reason = reason
        self.revision += 1
        return True

    def deadline_reason(self, now_ns: int) -> str | None:
        """Pure deadline query, ordered by task, total problem, attempts, progress."""
        self._check_time(now_ns)
        if self.started_ns is None:
            return None
        if now_ns - self.started_ns >= self.task_timeout_ns:
            return "task_deadline"
        if self.problem_started_ns is None:
            return None
        if now_ns - self.problem_started_ns >= self.problem_timeout_ns:
            return "problem_deadline"
        if self.attempts >= self.max_attempts:
            return "attempts_exhausted"
        anchor = max(self.problem_started_ns, self.last_progress_ns)
        if now_ns - anchor >= self.no_progress_timeout_ns:
            return "no_progress"
        return None

    def can_attempt(self, now_ns: int) -> bool:
        """Pure local allowance; never substitutes for the action safety guard."""
        return (self.deadline_reason(now_ns) is None and self.started_ns is not None
                and self.phase not in {"blocked", "suspended"})

    def enforce_deadline(self, now_ns: int) -> str | None:
        """Explicitly finish exhausted processing; caller still releases Runtime."""
        reason = self.deadline_reason(now_ns)
        if reason is not None:
            self.set_phase("blocked", now_ns, reason)
        return reason

    def diagnostic(self, now_ns: int) -> dict[str, object]:
        """Compact fresh JSON-friendly snapshot with no state mutation."""
        deadline = self.deadline_reason(now_ns)
        problem_elapsed = None if self.problem_started_ns is None else now_ns - self.problem_started_ns
        progress_anchor = None if self.problem_started_ns is None else max(self.problem_started_ns, self.last_progress_ns)
        return {
            "execution_recovery_revision": EXECUTION_RECOVERY_REVISION,
            "phase": self.phase, "revision": self.revision,
            "started_ns": self.started_ns, "problem_started_ns": self.problem_started_ns,
            "last_progress_ns": self.last_progress_ns, "attempts": self.attempts,
            "feedback_high_water": self._feedback_high_water,
            "attempt_high_water": self._attempt_high_water,
            "reason": self._reason, "problem_key": self._problem_key,
            "deadline_reason": deadline, "problem_elapsed_ns": problem_elapsed,
            "no_progress_elapsed_ns": None if progress_anchor is None else now_ns - progress_anchor,
            "task_elapsed_ns": None if self.started_ns is None else now_ns - self.started_ns,
            "last_position": None if self._last_position is None else list(self._last_position),
            "last_executed": self._last_executed,
            "task_timeout_ns": self.task_timeout_ns,
            "problem_timeout_ns": self.problem_timeout_ns,
            "no_progress_timeout_ns": self.no_progress_timeout_ns,
            "max_attempts": self.max_attempts,
        }
