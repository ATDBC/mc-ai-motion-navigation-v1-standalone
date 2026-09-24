"""Deterministic rejection categories and bounded local recovery accounting."""
from __future__ import annotations

from dataclasses import dataclass

from mc2p.contracts.common import ContractViolation, require_nonnegative_int


ATTEMPT_BUDGET_NS = 3_000_000_000
ATTEMPT_CHECKS = 16
MAX_RECOVERIES = 12
RETRY_WAIT_NS = 250_000_000
TOTAL_BUDGETS_NS = frozenset((30_000_000_000, 90_000_000_000))
MAX_RETAINED_ATTEMPTS = MAX_RECOVERIES


_REASON_CATEGORY = {
    # Canonical NB3 categories.
    "control_unavailable": "control_unavailable",
    "unsupported_motion": "unsupported_motion",
    "missing_field": "missing_field",
    "known_obstacle": "known_obstacle",
    "contradiction": "contradiction",
    "missing_support": "missing_support",
    "uncertain_history": "uncertain_history",
    # Existing navigation/motion guard reasons used by the adapter boundary.
    "navigation_observation_unavailable": "control_unavailable",
    "navigation_observation_mismatch": "control_unavailable",
    "stale_navigation_request": "control_unavailable",
    "stale_motion_state": "control_unavailable",
    "support_time_invalid": "control_unavailable",
    "unsupported_motion_state": "unsupported_motion",
    "unsupported_velocity_or_height": "unsupported_motion",
    "unsupported_support_semantics": "unsupported_motion",
    "unsupported_block_collision": "missing_field",
    "observed_body_or_head_obstacle": "known_obstacle",
    "observed_entity_contact": "known_obstacle",
    "current_collision": "known_obstacle",
    "unknown_reachable_floor": "missing_support",
    "unknown_landing_support": "missing_support",
    "unknown_footprint_support": "missing_support",
    "support_not_observed": "missing_support",
    "support_expired": "uncertain_history",
}

_PRIORITY = (
    "control_unavailable",
    "unsupported_motion",
    "missing_field",
    "known_obstacle",
    "contradiction",
    "missing_support",
    "uncertain_history",
)


def classify_rejection(reasons: tuple[str, ...]) -> str:
    """Return the highest-priority category after inspecting every reason."""
    if type(reasons) is not tuple or not reasons:
        raise ContractViolation("rejection reasons must be a nonempty tuple")
    categories: set[str] = set()
    for reason in reasons:
        if type(reason) is not str or reason not in _REASON_CATEGORY:
            raise ContractViolation("unknown navigation rejection reason")
        categories.add(_REASON_CATEGORY[reason])
    return next(category for category in _PRIORITY if category in categories)


@dataclass(frozen=True, slots=True)
class RecoveryAttemptState:
    key: str
    evidence_revision: str
    started_at_ns: int
    checks: int
    last_failure_ns: int | None


@dataclass(frozen=True, slots=True)
class RecoveryLedgerState:
    started_at_ns: int | None
    recoveries: int
    attempts: tuple[RecoveryAttemptState, ...]


@dataclass(slots=True)
class _Attempt:
    started_at_ns: int
    checks: int = 0
    failed_checks: int = 0
    last_failure_ns: int | None = None


def _identity(value: str, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ContractViolation(name + " must be a nonempty string")


class RecoveryLedger:
    """A trial-local ledger that cannot be renewed by key/revision churn."""

    def __init__(self, total_budget_ns: int = 90_000_000_000) -> None:
        require_nonnegative_int(total_budget_ns, "recovery total budget")
        if total_budget_ns not in TOTAL_BUDGETS_NS:
            raise ContractViolation("recovery total budget must be 30 or 90 seconds")
        self.total_budget_ns = total_budget_ns
        self._started_at_ns: int | None = None
        self._last_now_ns: int | None = None
        self._recoveries = 0
        self._attempts: dict[tuple[str, str], _Attempt] = {}

    def _time(self, now_ns: int) -> None:
        require_nonnegative_int(now_ns, "recovery time")
        if self._last_now_ns is not None and now_ns < self._last_now_ns:
            raise ContractViolation("recovery time regression")
        if self._started_at_ns is None:
            self._started_at_ns = now_ns
        self._last_now_ns = now_ns

    @staticmethod
    def _pair(key: str, evidence_revision: str) -> tuple[str, str]:
        _identity(key, "recovery key")
        _identity(evidence_revision, "evidence revision")
        return key, evidence_revision

    def allow(self, key: str, evidence_revision: str, now_ns: int) -> bool:
        pair = self._pair(key, evidence_revision)
        self._time(now_ns)
        assert self._started_at_ns is not None
        if now_ns - self._started_at_ns >= self.total_budget_ns:
            return False
        if self._recoveries >= MAX_RECOVERIES:
            return False

        attempt = self._attempts.get(pair)
        if attempt is None:
            if len(self._attempts) >= MAX_RETAINED_ATTEMPTS:
                return False
            attempt = _Attempt(now_ns)
            self._attempts[pair] = attempt
        if now_ns - attempt.started_at_ns >= ATTEMPT_BUDGET_NS:
            return False
        if attempt.checks >= ATTEMPT_CHECKS:
            return False
        if (
            attempt.last_failure_ns is not None
            and now_ns - attempt.last_failure_ns < RETRY_WAIT_NS
        ):
            return False
        attempt.checks += 1
        return True

    def fail(self, key: str, evidence_revision: str, now_ns: int) -> None:
        pair = self._pair(key, evidence_revision)
        self._time(now_ns)
        attempt = self._attempts.get(pair)
        if attempt is None or attempt.checks <= attempt.failed_checks:
            raise ContractViolation("recovery failure requires an allowed check")
        if self._recoveries >= MAX_RECOVERIES:
            raise ContractViolation("recovery budget exhausted")
        attempt.failed_checks = attempt.checks
        attempt.last_failure_ns = now_ns
        self._recoveries += 1

    def snapshot(self) -> RecoveryLedgerState:
        attempts = tuple(
            RecoveryAttemptState(
                key,
                revision,
                attempt.started_at_ns,
                attempt.checks,
                attempt.last_failure_ns,
            )
            for (key, revision), attempt in sorted(self._attempts.items())
        )
        return RecoveryLedgerState(self._started_at_ns, self._recoveries, attempts)

    def clear(self) -> None:
        self._started_at_ns = None
        self._last_now_ns = None
        self._recoveries = 0
        self._attempts.clear()
