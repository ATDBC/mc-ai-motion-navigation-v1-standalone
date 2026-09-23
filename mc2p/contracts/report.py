"""Structured failures and execution reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mc2p.contracts.common import (
    ContractViolation,
    parse_json_object,
    require_finite,
    require_identifier,
    require_nonnegative_int,
)


class FailureCodeV0(StrEnum):
    CONFIGURATION = "configuration"
    CONTRACT = "contract"
    BACKEND_START = "backend_start"
    BACKEND_IO = "backend_io"
    BACKEND_DISCONNECTED = "backend_disconnected"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    OBSERVATION_INVARIANT = "observation_invariant"
    TRACE_IO = "trace_io"
    CLEANUP = "cleanup"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class FailureV0:
    code: FailureCodeV0
    message: str
    retryable: bool
    source: str
    detail_json: str = "{}"
    schema_version: str = field(default="mc2p.failure.v0", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.code, FailureCodeV0):
            raise ContractViolation("failure code must be FailureCodeV0")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ContractViolation("failure message must be non-empty")
        if type(self.retryable) is not bool:
            raise ContractViolation("failure retryable must be bool")
        require_identifier(self.source, "failure source")
        parse_json_object(self.detail_json, "failure detail")


class ExecutionStatusV0(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class ExecutionReportV0:
    report_id: str
    task_id: str
    episode_id: str
    runtime_step: int
    status: ExecutionStatusV0
    phase: str
    progress: float
    selected_intent_ids: tuple[str, ...]
    action_sequence_id: int | None
    observation_sequence_id: int | None
    risk_events: tuple[str, ...] = ()
    recoveries: tuple[str, ...] = ()
    failure: FailureV0 | None = None
    schema_version: str = field(default="mc2p.execution-report.v0", init=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("report_id", self.report_id),
            ("task_id", self.task_id),
            ("episode_id", self.episode_id),
            ("phase", self.phase),
        ):
            require_identifier(value, name)
        require_nonnegative_int(self.runtime_step, "runtime_step")
        if not isinstance(self.status, ExecutionStatusV0):
            raise ContractViolation("report status must be ExecutionStatusV0")
        require_finite(self.progress, "progress")
        if not 0.0 <= float(self.progress) <= 1.0:
            raise ContractViolation("progress must be within [0, 1]")
        for name, value in (
            ("action_sequence_id", self.action_sequence_id),
            ("observation_sequence_id", self.observation_sequence_id),
        ):
            if value is not None:
                require_nonnegative_int(value, name)
        for intent_id in self.selected_intent_ids:
            require_identifier(intent_id, "selected intent id")
        terminal_failure = self.status in {
            ExecutionStatusV0.FAILED,
            ExecutionStatusV0.CANCELLED,
            ExecutionStatusV0.TIMED_OUT,
        }
        if terminal_failure and self.failure is None:
            raise ContractViolation(f"{self.status.value} report requires failure")
        if not terminal_failure and self.failure is not None:
            raise ContractViolation(
                f"{self.status.value} report cannot carry a failure"
            )
