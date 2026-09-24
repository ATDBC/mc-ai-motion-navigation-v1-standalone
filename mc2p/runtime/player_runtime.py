"""Single-writer Player Runtime V0 state machine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import threading
import time
from typing import Callable

from mc2p.contracts.action import ActionIntentV0, ActionSnapshotV0
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation_v2 import ObservationSnapshotV2
from mc2p.contracts.report import (
    ExecutionReportV0,
    ExecutionStatusV0,
    FailureCodeV0,
    FailureV0,
)
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.contracts.task import TaskIntentV0
from mc2p.runtime.arbiter import ActionArbiterV0, ArbitrationDecisionV0
from mc2p.runtime.backend import BackendStepResultV0, PlayerBackendV0
from mc2p.runtime.trace import TraceSinkV0


class RuntimeStateV0(StrEnum):
    NEW = "new"
    READY = "ready"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class RuntimeStepResultV0:
    observation: ObservationSnapshotV2 | None
    decision: ArbitrationDecisionV0 | None
    report: ExecutionReportV0
    terminated: bool
    truncated: bool


class PlayerRuntimeV0:
    def __init__(
        self,
        backend: PlayerBackendV0,
        trace_writer: TraceSinkV0,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._backend = backend
        self._trace = trace_writer
        self._clock_ns = clock_ns
        self._arbiter = ActionArbiterV0()
        self._state = RuntimeStateV0.NEW
        self._episode_id: str | None = None
        self._last_observation_sequence = -1
        self._action_sequence = 0
        self._runtime_step = 0
        self._cancel_reason: str | None = None
        self._cancel_lock = threading.Lock()
        self._cleanup_failures: list[FailureV0] = []

    @property
    def state(self) -> RuntimeStateV0:
        return self._state

    @property
    def cleanup_failure(self) -> FailureV0 | None:
        return self._cleanup_failures[0] if self._cleanup_failures else None

    @property
    def cleanup_failures(self) -> tuple[FailureV0, ...]:
        return tuple(self._cleanup_failures)

    def reset(self, request: ResetRequestV0) -> ResetResultV0:
        if self._state is RuntimeStateV0.CLOSED:
            raise ContractViolation("closed runtime cannot be reset")
        if not isinstance(request, ResetRequestV0):
            raise ContractViolation("reset requires ResetRequestV0")
        now = self._clock_ns()
        if now >= request.deadline_monotonic_ns:
            result = self._failed_reset(
                request,
                FailureCodeV0.DEADLINE_EXCEEDED,
                "reset deadline already expired",
                retryable=True,
            )
            self._state = RuntimeStateV0.FAILED
            return result
        self._arbiter.clear()
        with self._cancel_lock:
            self._cancel_reason = None
        try:
            result = self._backend.reset(request)
        except Exception as error:
            code = (
                FailureCodeV0.DEADLINE_EXCEEDED
                if isinstance(error, TimeoutError)
                else FailureCodeV0.BACKEND_START
            )
            result = self._failed_reset(
                request,
                code,
                str(error),
                retryable=True,
                error=error,
            )
        if result.succeeded:
            assert result.observation is not None
            try:
                self._validate_reset_result(request, result)
            except ContractViolation as error:
                result = self._failed_reset(
                    request,
                    FailureCodeV0.OBSERVATION_INVARIANT,
                    str(error),
                    retryable=False,
                    error=error,
                )
        try:
            self._trace.write("reset", {"request": request, "result": result})
        except Exception as error:
            result = self._failed_reset(
                request,
                FailureCodeV0.TRACE_IO,
                str(error),
                retryable=True,
                error=error,
            )
        if result.succeeded:
            assert result.observation is not None
            self._episode_id = result.actual_episode_id
            self._last_observation_sequence = result.observation.sequence_id
            self._action_sequence = 0
            self._runtime_step = 0
            self._state = RuntimeStateV0.READY
        else:
            self._state = RuntimeStateV0.FAILED
        return result

    def submit_intent(self, intent: ActionIntentV0) -> None:
        self._require_ready()
        self._arbiter.submit(intent)

    def cancel_source(self, source_id: str) -> tuple[str, ...]:
        self._require_ready()
        return self._arbiter.cancel_source(source_id)

    def cancel(self, reason: str) -> None:
        self._require_ready()
        if not isinstance(reason, str) or not reason.strip():
            raise ContractViolation("cancel reason must be non-empty")
        with self._cancel_lock:
            self._cancel_reason = reason.strip()

    def step(
        self,
        task: TaskIntentV0,
        profile: BehaviorProfileV0,
        deadline_monotonic_ns: int,
    ) -> RuntimeStepResultV0:
        self._require_ready()
        if not isinstance(task, TaskIntentV0):
            raise ContractViolation("step requires TaskIntentV0")
        if not isinstance(profile, BehaviorProfileV0):
            raise ContractViolation("step requires BehaviorProfileV0")
        require_nonnegative_int(deadline_monotonic_ns, "deadline_monotonic_ns")
        effective_deadline = min(
            deadline_monotonic_ns,
            task.deadline_monotonic_ns,
        )
        now = self._clock_ns()
        if now >= effective_deadline:
            failure = self._failure(
                FailureCodeV0.DEADLINE_EXCEEDED,
                "step deadline already expired",
                retryable=True,
            )
            self._state = RuntimeStateV0.FAILED
            report = self._make_report(
                task,
                ExecutionStatusV0.TIMED_OUT,
                "deadline",
                (),
                None,
                None,
                failure,
            )
            self._safe_trace("step", {"task": task, "profile": profile, "report": report})
            return RuntimeStepResultV0(None, None, report, False, False)

        with self._cancel_lock:
            cancel_reason = self._cancel_reason
        if cancel_reason is not None:
            decision = ArbitrationDecisionV0(
                action=ActionSnapshotV0.neutral(self._action_sequence),
                selected_intents=(),
                candidate_intent_ids=(),
                expired_intent_ids=(),
            )
            return self._perform_step(
                task,
                profile,
                effective_deadline,
                decision,
                cancel_reason=cancel_reason,
            )

        decision = self._arbiter.resolve(now, self._action_sequence)
        return self._perform_step(
            task,
            profile,
            effective_deadline,
            decision,
        )

    def _perform_step(
        self,
        task: TaskIntentV0,
        profile: BehaviorProfileV0,
        deadline: int,
        decision: ArbitrationDecisionV0,
        *,
        cancel_reason: str | None = None,
    ) -> RuntimeStepResultV0:
        action = decision.action
        self._action_sequence += 1
        self._runtime_step += 1
        try:
            backend_result = self._backend.step(action, deadline)
            self._validate_step_result(action, backend_result)
        except Exception as error:
            code = (
                FailureCodeV0.DEADLINE_EXCEEDED
                if isinstance(error, TimeoutError)
                else FailureCodeV0.BACKEND_IO
            )
            if isinstance(error, ContractViolation):
                code = FailureCodeV0.OBSERVATION_INVARIANT
            status = (
                ExecutionStatusV0.TIMED_OUT
                if code is FailureCodeV0.DEADLINE_EXCEEDED
                else ExecutionStatusV0.FAILED
            )
            failure = self._failure(code, str(error), retryable=True, error=error)
            report = self._make_report(
                task,
                status,
                "backend-step",
                self._selected_ids(decision),
                action.action_sequence_id,
                None,
                failure,
            )
            self._state = RuntimeStateV0.FAILED
            self._safe_trace(
                "step",
                {
                    "task": task,
                    "profile": profile,
                    "decision": decision,
                    "report": report,
                },
            )
            return RuntimeStepResultV0(None, decision, report, False, False)

        observation = backend_result.observation
        self._last_observation_sequence = observation.sequence_id
        if cancel_reason is None:
            status = ExecutionStatusV0.RUNNING
            phase = task.task_type
            failure = None
        else:
            status = ExecutionStatusV0.CANCELLED
            phase = "cancel"
            failure = self._failure(
                FailureCodeV0.CANCELLED,
                cancel_reason,
                retryable=True,
            )
        report = self._make_report(
            task,
            status,
            phase,
            self._selected_ids(decision),
            action.action_sequence_id,
            observation.sequence_id,
            failure,
        )
        payload = {
            "task": task,
            "profile": profile,
            "decision": decision,
            "backend_result": backend_result,
            "report": report,
        }
        try:
            self._trace.write("step", payload)
        except Exception as error:
            failure = self._failure(
                FailureCodeV0.TRACE_IO,
                str(error),
                retryable=True,
                error=error,
            )
            report = self._make_report(
                task,
                ExecutionStatusV0.FAILED,
                "trace-write",
                self._selected_ids(decision),
                action.action_sequence_id,
                observation.sequence_id,
                failure,
            )
            self._state = RuntimeStateV0.FAILED
            return RuntimeStepResultV0(
                observation,
                decision,
                report,
                backend_result.terminated,
                backend_result.truncated,
            )
        if cancel_reason is not None:
            self._arbiter.clear()
            self._state = RuntimeStateV0.CANCELLED
        return RuntimeStepResultV0(
            observation,
            decision,
            report,
            backend_result.terminated,
            backend_result.truncated,
        )

    def close(self) -> None:
        if self._state is RuntimeStateV0.CLOSED:
            return
        if self._state is RuntimeStateV0.READY:
            try:
                now = self._clock_ns()
                neutral = ActionSnapshotV0.neutral(self._action_sequence)
                self._backend.step(neutral, now + 5_000_000_000)
                self._action_sequence += 1
            except Exception as error:
                self._cleanup_failures.append(
                    self._failure(
                        FailureCodeV0.CLEANUP,
                        f"neutral release failed: {error}",
                        retryable=True,
                        error=error,
                    )
                )
        try:
            self._backend.close()
        except Exception as error:
            self._cleanup_failures.append(
                self._failure(
                    FailureCodeV0.CLEANUP,
                    str(error),
                    retryable=True,
                    error=error,
                )
            )
        finally:
            try:
                self._trace.close()
            except Exception as error:
                self._cleanup_failures.append(
                    self._failure(
                        FailureCodeV0.CLEANUP,
                        f"trace close failed: {error}",
                        retryable=True,
                        error=error,
                    )
                )
            self._state = RuntimeStateV0.CLOSED

    def __enter__(self) -> PlayerRuntimeV0:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _validate_reset_result(
        self,
        request: ResetRequestV0,
        result: ResetResultV0,
    ) -> None:
        if result.request_id != request.request_id:
            raise ContractViolation("reset result request id mismatch")
        if result.actual_episode_id != request.episode_id:
            raise ContractViolation("reset result episode id mismatch")
        assert result.observation is not None
        if type(result.observation) is not ObservationSnapshotV2:
            raise ContractViolation("legacy Runtime requires exact ObservationSnapshotV2")
        if result.observation.episode_id != result.actual_episode_id:
            raise ContractViolation("reset observation episode id mismatch")
        if result.observation.sequence_id != 0:
            raise ContractViolation("reset observation sequence must be zero")
        if result.observation.request_sequence_id is not None:
            raise ContractViolation("reset observation request sequence must be null")

    def _validate_step_result(
        self,
        action: ActionSnapshotV0,
        result: BackendStepResultV0,
    ) -> None:
        if not isinstance(result, BackendStepResultV0):
            raise ContractViolation("backend returned invalid step result")
        observation = result.observation
        if type(observation) is not ObservationSnapshotV2:
            raise ContractViolation("legacy Runtime requires exact ObservationSnapshotV2")
        if observation.episode_id != self._episode_id:
            raise ContractViolation("step observation episode id mismatch")
        if observation.request_sequence_id != action.action_sequence_id:
            raise ContractViolation("step observation request sequence mismatch")
        if observation.sequence_id <= self._last_observation_sequence:
            raise ContractViolation("step observation sequence did not advance")

    def _make_report(
        self,
        task: TaskIntentV0,
        status: ExecutionStatusV0,
        phase: str,
        selected_ids: tuple[str, ...],
        action_sequence_id: int | None,
        observation_sequence_id: int | None,
        failure: FailureV0 | None,
    ) -> ExecutionReportV0:
        return ExecutionReportV0(
            report_id=f"report-{self._runtime_step}",
            task_id=task.task_id,
            episode_id=self._episode_id or "unknown-episode",
            runtime_step=self._runtime_step,
            status=status,
            phase=phase,
            progress=0.0,
            selected_intent_ids=selected_ids,
            action_sequence_id=action_sequence_id,
            observation_sequence_id=observation_sequence_id,
            failure=failure,
        )

    @staticmethod
    def _selected_ids(decision: ArbitrationDecisionV0) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(intent_id for _, intent_id in decision.selected_intents)
        )

    @staticmethod
    def _failure(
        code: FailureCodeV0,
        message: str,
        *,
        retryable: bool,
        error: Exception | None = None,
    ) -> FailureV0:
        detail = {}
        if error is not None:
            detail["exception_type"] = type(error).__name__
        return FailureV0(
            code=code,
            message=message or code.value,
            retryable=retryable,
            source="runtime",
            detail_json=json.dumps(detail, sort_keys=True),
        )

    def _failed_reset(
        self,
        request: ResetRequestV0,
        code: FailureCodeV0,
        message: str,
        *,
        retryable: bool,
        error: Exception | None = None,
    ) -> ResetResultV0:
        return ResetResultV0(
            request_id=request.request_id,
            actual_episode_id=request.episode_id,
            succeeded=False,
            failure=self._failure(
                code,
                message,
                retryable=retryable,
                error=error,
            ),
        )

    def _safe_trace(self, record_type: str, payload: object) -> None:
        try:
            self._trace.write(record_type, payload)
        except Exception:
            return

    def _require_ready(self) -> None:
        if self._state is RuntimeStateV0.CLOSED:
            raise ContractViolation("runtime is closed")
        if self._state is not RuntimeStateV0.READY:
            raise ContractViolation("runtime must be ready")
