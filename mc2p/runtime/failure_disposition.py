"""Typed lifecycle decisions derived from formal Runtime failures."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from mc2p.contracts.common import ContractViolation, require_identifier
from mc2p.contracts.report import FailureCodeV0, FailureV0


class FailureDisposition(StrEnum):
    CONTINUE_WITH_INCOMPLETE_EVIDENCE = "continue_with_incomplete_evidence"
    RETRY_TASK_BOUNDED = "retry_task_bounded"
    CANCEL_TASK = "cancel_task"
    RECREATE_RUNTIME = "recreate_runtime"
    END_EPISODE = "end_episode"


@dataclass(frozen=True, slots=True)
class FailureDispositionDecision:
    disposition: FailureDisposition
    reason_code: str
    evidence_complete: bool
    retry_requested: bool
    retry_number: int = 0

    def __post_init__(self) -> None:
        if type(self.disposition) is not FailureDisposition:
            raise ContractViolation("failure disposition must be typed")
        require_identifier(self.reason_code, "failure disposition reason")
        if type(self.evidence_complete) is not bool \
                or type(self.retry_requested) is not bool:
            raise ContractViolation("failure disposition flags must be bool")
        if type(self.retry_number) is not int or self.retry_number < 0:
            raise ContractViolation("failure retry number must be nonnegative")
        if self.retry_requested != (
            self.disposition is FailureDisposition.RETRY_TASK_BOUNDED
        ):
            raise ContractViolation("retry flag and disposition disagree")


def classify_failure(failure: FailureV0) -> FailureDispositionDecision:
    """Map failure identity to an action; messages never grant permission."""
    if type(failure) is not FailureV0:
        raise ContractViolation("failure disposition requires FailureV0")
    code = failure.code
    if code is FailureCodeV0.TRACE_IO and failure.source == "async_trace":
        return FailureDispositionDecision(
            FailureDisposition.CONTINUE_WITH_INCOMPLETE_EVIDENCE,
            "async_trace_evidence_incomplete", False, False,
        )
    if code is FailureCodeV0.CANCELLED:
        return FailureDispositionDecision(
            FailureDisposition.CANCEL_TASK, "explicit_cancellation", True, False,
        )
    if code is FailureCodeV0.DEADLINE_EXCEEDED:
        if failure.retryable:
            return FailureDispositionDecision(
                FailureDisposition.RETRY_TASK_BOUNDED,
                "deadline_retryable", True, True,
            )
        return FailureDispositionDecision(
            FailureDisposition.CANCEL_TASK, "deadline_not_retryable", True, False,
        )
    if code in {
        FailureCodeV0.BACKEND_START,
        FailureCodeV0.BACKEND_IO,
        FailureCodeV0.BACKEND_DISCONNECTED,
    } and not failure.retryable:
        return FailureDispositionDecision(
            FailureDisposition.END_EPISODE,
            "backend_failure_not_recoverable", True, False,
        )
    return FailureDispositionDecision(
        FailureDisposition.RECREATE_RUNTIME,
        f"{code.value}_requires_runtime_recreation", True, False,
    )


class FailureDispositionPolicy:
    """Bound retries for one typed cause within one task lifecycle."""

    def __init__(self, *, max_same_cause_retries: int = 2) -> None:
        if type(max_same_cause_retries) is not int or max_same_cause_retries < 0:
            raise ContractViolation("same-cause retry limit must be nonnegative")
        self._limit = max_same_cause_retries
        self._counts: dict[tuple[FailureCodeV0, str], int] = {}

    def decide(self, failure: FailureV0) -> FailureDispositionDecision:
        decision = classify_failure(failure)
        if not decision.retry_requested:
            return decision
        key = (failure.code, failure.source)
        retry_number = self._counts.get(key, 0) + 1
        self._counts[key] = retry_number
        if retry_number > self._limit:
            return FailureDispositionDecision(
                FailureDisposition.CANCEL_TASK,
                "same_cause_retry_exhausted", True, False, retry_number,
            )
        return replace(decision, retry_number=retry_number)

    def clear(self) -> None:
        self._counts.clear()
