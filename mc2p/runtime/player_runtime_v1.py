"""Single-writer formal Runtime. V0 key actions have no conversion or fallback here."""
from dataclasses import dataclass
from enum import StrEnum
import json
import threading
import time
from typing import Callable, TypeVar

from mc2p.contracts.action_v1 import ActionIntentV1, ActionSnapshotV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.intent_source import IntentSourceV1, OrderedIntentV1
from mc2p.contracts.observation_v2 import ObservationSnapshotV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.observation_request_v3 import (
    OBSERVATION_V3, ObservationRequestV3, validate_observation_schema, resolve_observation_request,
)
from mc2p.contracts.report import ExecutionReportV0, ExecutionStatusV0, FailureCodeV0, FailureV0
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.contracts.task import TaskIntentV0
from mc2p.runtime.arbiter_v1 import ActionArbiterV1, ArbitrationDecisionV1
from mc2p.runtime.backend_v1 import BackendStepResultV1, PlayerBackendV1
from mc2p.runtime.trace import TraceSinkV0

_OrderedResult = TypeVar('_OrderedResult')


class RuntimeStateV1(StrEnum):
    NEW = "new"
    READY = "ready"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ENDED = "ended"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class RuntimeStepResultV1:
    observation: ObservationSnapshotV2 | ObservationSnapshotV3 | None
    decision: ArbitrationDecisionV1 | None
    report: ExecutionReportV0
    backend_result: BackendStepResultV1 | None = None


class PlayerRuntimeV1:
    def __init__(self, backend: PlayerBackendV1, trace_writer: TraceSinkV0,
                 clock_ns: Callable[[], int] = time.perf_counter_ns) -> None:
        if getattr(backend, "action_schema_version", None) != "mc2p.action-snapshot.v1":
            raise ContractViolation("formal Runtime requires an explicit Action V1 backend")
        self._observation_schema = validate_observation_schema(getattr(backend, "observation_schema_version", None))
        self._observation_type = ObservationSnapshotV3 if self._observation_schema == OBSERVATION_V3 else ObservationSnapshotV2
        self._backend, self._trace, self._clock = backend, trace_writer, clock_ns
        self._arbiter = ActionArbiterV1()
        self._io_lock, self._cancel_lock = threading.RLock(), threading.Lock()
        self._state = RuntimeStateV1.NEW
        self._observation: ObservationSnapshotV2 | ObservationSnapshotV3 | None = None
        self._episodes: set[str] = set()
        self._request_sequence = self._step_number = 0
        self._cancel_reason: str | None = None
        self._backend_closed = False
        self._cleanup_failures: list[FailureV0] = []
        self._backend_elapsed_ns_total = 0
        self._backend_blocking_io_ns_total = 0

    @property
    def state(self) -> RuntimeStateV1:
        return self._state

    @property
    def observation(self) -> ObservationSnapshotV2 | ObservationSnapshotV3 | None:
        return self._observation

    @property
    def cleanup_failures(self) -> tuple[FailureV0, ...]:
        return tuple(self._cleanup_failures)

    @property
    def backend_elapsed_ns_total(self) -> int:
        """Wall time spent inside backend calls; used only for timing attribution."""
        return self._backend_elapsed_ns_total

    @property
    def backend_blocking_io_ns_total(self) -> int:
        """Socket-blocking time reported by the backend, when available."""
        return self._backend_blocking_io_ns_total

    def reset(self, request: ResetRequestV0) -> ResetResultV0:
        with self._io_lock:
            if self._state is RuntimeStateV1.CLOSED or self._backend_closed:
                raise ContractViolation("closed/sealed Runtime requires recreation")
            if type(request) is not ResetRequestV0:
                raise ContractViolation("reset requires ResetRequestV0")
            if request.episode_id in self._episodes or len(self._episodes) >= 4096:
                raise ContractViolation("episode identity reused or Runtime episode capacity exceeded")
            self._episodes.add(request.episode_id)
            self._arbiter.clear()
            self._observation = None
            with self._cancel_lock:
                self._cancel_reason = None
            code = FailureCodeV0.BACKEND_START
            try:
                self._check_deadline(request.deadline_monotonic_ns)
                result = self._backend.reset(request)
                self._check_deadline(request.deadline_monotonic_ns)
                if type(result) is not ResetResultV0:
                    raise ContractViolation("backend returned invalid reset result")
                if result.succeeded:
                    obs = result.observation
                    if (result.request_id != request.request_id or result.actual_episode_id != request.episode_id
                            or type(obs) is not self._observation_type or obs.episode_id != request.episode_id
                            or obs.sequence_id != 0 or obs.request_sequence_id is not None):
                        raise ContractViolation("reset observation/result identity mismatch")
                    if type(obs) is ObservationSnapshotV3 and obs.field_profile != "navigation_v1":
                        raise ContractViolation("reset requires navigation observation")
                code = FailureCodeV0.TRACE_IO
                self._trace.write("reset", {"request": request, "result": result})
            except Exception as error:
                self._seal()
                failure = self._exception_failure(error, code)
                result = ResetResultV0(request.request_id, request.episode_id, False, failure=failure)
                self._record_failure("reset_failure", {"request": request, "result": result})
            except BaseException:
                self._seal()
                raise
            if result.succeeded:
                self._observation = result.observation
                self._request_sequence = self._step_number = 0
                self._state = RuntimeStateV1.READY
            else:
                self._seal()
            return result

    def submit_intent(self, intent: ActionIntentV1) -> None:
        with self._io_lock:
            self._require_ready()
            self._validate_intent(intent)
            self._arbiter.submit(intent)
            try:
                self._trace.write("intent", {"intent": intent})
            except BaseException:
                self._seal()
                raise

    def _validate_intent(self, intent: ActionIntentV1) -> None:
        if type(intent) is not ActionIntentV1:
            raise ContractViolation('formal Runtime accepts ActionIntentV1 only')
        now = self._clock()
        if (intent.episode_id != self._observation.episode_id
                or intent.observation_sequence_id != self._observation.sequence_id
                or intent.submitted_at_monotonic_ns > now or intent.expires_at_monotonic_ns <= now):
            raise ContractViolation('intent has stale episode/observation or invalid time')

    @property
    def ordered_source_stats(self) -> dict[str, int]:
        with self._io_lock:
            return self._arbiter.ordered_source_stats

    def register_ordered_source(self, label: str) -> IntentSourceV1:
        with self._io_lock:
            self._require_ready()
            return self._ordered_change(
                lambda: self._arbiter.register_ordered_source(label, episode_id=self._observation.episode_id),
                'ordered_source_registered', lambda source: {'source': source, 'label': label})

    def submit_ordered_intent(self, envelope: OrderedIntentV1) -> None:
        with self._io_lock:
            self._require_ready()
            if type(envelope) is not OrderedIntentV1:
                raise ContractViolation('ordered Runtime accepts OrderedIntentV1 only')
            self._validate_intent(envelope.intent)
            self._ordered_change(lambda: self._arbiter.submit_ordered_intent(envelope),
                                 'ordered_intent', lambda _: {'envelope': envelope})

    def unregister_ordered_source(self, source: IntentSourceV1) -> tuple[str, ...]:
        with self._io_lock:
            self._require_ready()
            return self._ordered_change(lambda: self._arbiter.unregister_ordered_source(source),
                'ordered_source_unregistered', lambda removed: {'source': source, 'removed_intent_ids': removed})

    def _ordered_change(self, change: Callable[[], _OrderedResult], record_type: str,
                        payload: Callable[[_OrderedResult], dict]) -> _OrderedResult:
        # Expected rejection precedes mutation. Uncertain mutations or unlogged acceptance seal the Runtime.
        try:
            result = change()
        except ContractViolation:
            raise
        except BaseException:
            self._seal()
            raise
        try:
            self._trace.write(record_type, payload(result))
        except BaseException:
            self._seal()
            raise
        return result

    def cancel_source(self, source_id: str) -> tuple[str, ...]:
        with self._io_lock:
            self._require_ready()
            removed = self._arbiter.cancel_source(source_id)
            try:
                self._trace.write("source_cancel", {"source_id": source_id, "removed_intent_ids": removed})
            except BaseException:
                self._seal()
                raise
            return removed

    def cancel(self, reason: str) -> None:
        self._require_ready()
        if type(reason) is not str or not reason.strip():
            raise ContractViolation("cancel reason must be non-empty")
        # Cooperative cancellation: does not claim to interrupt an in-flight bounded backend call.
        with self._cancel_lock:
            self._cancel_reason = reason.strip()

    def record_task_event(self, record_type: str, payload: dict) -> None:
        """Internal task evidence, never an Action/Observation extension or writer bypass."""
        schemas = {'task_lease_renewed':'mc2p.task-lease-renewed.v1',
                   'playground_task':'mc2p.playground-task-step.v1',
                   'active_perception':'mc2p.active-perception-step.v1',
                   'playground_control_release':'mc2p.playground-control-release.v1',
                   'combat_assessment':'mc2p.combat-assessment.v1',
                   'combat_candidates':'mc2p.combat-candidates.v1',
                   'combat_selection':'mc2p.combat-selection.v1',
                   'combat_skill':'mc2p.combat-skill.v1',
                   'moving_engagement':'mc2p.moving-engagement.v1',
                   'moving_melee_decision':'mc2p.moving-melee-decision-event.v1',
                   'moving_goal_decision':'mc2p.moving-goal-decision-event.v1',
                   'external_motion_detection':'mc2p.external-motion-detection-event.v1',
                   'external_motion_recovery':'mc2p.external-motion-recovery-event.v1'}
        with self._io_lock:
            self._require_ready()
            if (record_type not in schemas or type(payload) is not dict
                    or payload.get('schema_version')!=schemas[record_type]
                    or payload.get('episode_id')!=self._observation.episode_id):
                raise ContractViolation('invalid internal task event')
            self._ordered_change(lambda: None,record_type,lambda _: payload)

    def fail_closed(self, reason: str) -> None:
        """Seal uncertain control without another action dispatch or reconnect."""
        if type(reason) is not str or not reason: raise ContractViolation('missing seal reason')
        with self._io_lock:
            if self._state is RuntimeStateV1.CLOSED: return
            try: self._record_failure('task_driver_failure',{'reason':reason})
            finally: self._seal()

    def step(self, task: TaskIntentV0, profile: BehaviorProfileV0,
             deadline_monotonic_ns: int, *, observation_request: ObservationRequestV3 | None = None) -> RuntimeStepResultV1:
        with self._io_lock:
            self._require_ready()
            request = resolve_observation_request(self._observation_schema, observation_request)
            if type(task) is not TaskIntentV0 or type(profile) is not BehaviorProfileV0:
                raise ContractViolation("step requires versioned task and behavior profile")
            require_nonnegative_int(deadline_monotonic_ns, "deadline_monotonic_ns")
            deadline = min(deadline_monotonic_ns, task.deadline_monotonic_ns)
            self._step_number += 1
            decision = backend_result = None
            phase_code = FailureCodeV0.CONTRACT
            try:
                self._check_deadline(deadline)
                with self._cancel_lock:
                    cancel_reason = self._cancel_reason
                obs = self._observation
                if cancel_reason is not None:
                    self._arbiter.clear()
                    decision = ArbitrationDecisionV1(ActionSnapshotV1(
                        obs.episode_id, self._request_sequence, obs.sequence_id, deadline))
                else:
                    gui = obs.gui.value
                    decision = self._arbiter.resolve(self._clock(), obs.episode_id, obs.sequence_id,
                        self._request_sequence, deadline, controls_blocked=gui is None or gui.open,
                        forbidden_actions=task.forbidden_actions)
                action = decision.action
                self._request_sequence += 1
                phase_code = FailureCodeV0.TRACE_IO
                self._trace.write("dispatch", {"decision": decision, "task": task, "profile": profile,
                                                "observation_request": request})
                phase_code = FailureCodeV0.BACKEND_IO
                backend_result = self._backend_step(action, action.deadline_monotonic_ns, request)
                self._check_deadline(action.deadline_monotonic_ns)
                self._validate_result(action, backend_result, request)
                self._observation = backend_result.observation
                receipt = backend_result.receipt
                failure = None
                status = ExecutionStatusV0.RUNNING
                phase = "constraint_filtered" if any(r.startswith("task_forbidden_")
                    for _, r in decision.suppressed_intents) else task.task_type
                if receipt.status in {"rejected", "timed_out"}:
                    status = ExecutionStatusV0.TIMED_OUT if receipt.status == "timed_out" or receipt.reason == "deadline_exceeded" else ExecutionStatusV0.FAILED
                    failure = FailureV0(FailureCodeV0.DEADLINE_EXCEEDED if status is ExecutionStatusV0.TIMED_OUT else FailureCodeV0.CONTRACT,
                        receipt.reason, True, "client_behavior")
                    phase = "action_rejected"
                    if cancel_reason is not None or status is ExecutionStatusV0.TIMED_OUT:
                        self._seal()
                elif cancel_reason is not None:
                    if receipt.status not in {"executed", "confirmed_local", "cancelled"}:
                        raise ContractViolation("neutral cancellation was not locally accepted")
                    status, phase = ExecutionStatusV0.CANCELLED, "cancel"
                    failure = FailureV0(FailureCodeV0.CANCELLED, cancel_reason, True, "runtime")
                    self._state = RuntimeStateV1.CANCELLED
                if backend_result.terminated or backend_result.truncated or self._observation.is_dead.value is True:
                    status, phase = ExecutionStatusV0.FAILED, "episode_ended"
                    failure = FailureV0(FailureCodeV0.BACKEND_DISCONNECTED, "episode ended; task completion not established", True, "runtime")
                    self._seal(RuntimeStateV1.ENDED)
                report = self._report(task, decision, status, phase, failure, self._observation)
                phase_code = FailureCodeV0.TRACE_IO
                self._trace.write("step", {"task": task, "profile": profile, "decision": decision,
                                          "backend_result": backend_result, "report": report})
                return RuntimeStepResultV1(self._observation, decision, report, backend_result)
            except Exception as error:
                failure = self._exception_failure(error, phase_code)
                self._seal()
                report = self._report(task, decision,
                    ExecutionStatusV0.TIMED_OUT if failure.code is FailureCodeV0.DEADLINE_EXCEEDED else ExecutionStatusV0.FAILED,
                    "runtime_failure", failure, None)
                self._record_failure("step_failure", {"decision": decision, "report": report})
                return RuntimeStepResultV1(None, decision, report)
            except BaseException:
                self._seal()
                raise

    def _backend_step(self, action, deadline, request):
        if self._backend.observation_schema_version != self._observation_schema:
            raise ContractViolation("backend observation schema changed during session")
        started_ns = time.perf_counter_ns()
        blocking_started = getattr(self._backend, "blocking_io_ns_total", None)
        try:
            if request is None:
                return self._backend.step(action, deadline)
            return self._backend.step(action, deadline, observation_request=request)
        finally:
            self._backend_elapsed_ns_total += time.perf_counter_ns() - started_ns
            blocking_finished = getattr(self._backend, "blocking_io_ns_total", None)
            if (type(blocking_started) is int and type(blocking_finished) is int
                    and blocking_finished >= blocking_started):
                self._backend_blocking_io_ns_total += (
                    blocking_finished - blocking_started
                )

    def _validate_result(self, action: ActionSnapshotV1, result: BackendStepResultV1,
                         request: ObservationRequestV3 | None = None) -> None:
        if type(result) is not BackendStepResultV1:
            raise ContractViolation("formal backend returned legacy/invalid result")
        obs, receipt = result.observation, result.receipt
        if type(obs) is not self._observation_type:
            raise ContractViolation("backend observation schema mismatch")
        if request is not None and obs.field_profile != request.field_profile:
            raise ContractViolation("observation_profile_mismatch")
        if (obs.episode_id != action.episode_id or obs.sequence_id != action.observation_sequence_id + 1
                or obs.request_sequence_id != action.request_sequence_id or receipt.episode_id != action.episode_id
                or receipt.request_sequence_id != action.request_sequence_id or receipt.generation_id != obs.sequence_id
                or receipt.world_tick != obs.world_time_ticks.value or not receipt.on_client_thread
                or receipt.action_keyboard_callbacks != 0 or receipt.action_mouse_callbacks != 0
                or receipt.status == "idle"):
            raise ContractViolation("formal receipt/observation does not match action or execution invariants")

    def close(self) -> None:
        with self._io_lock:
            if self._state is RuntimeStateV1.CLOSED:
                return
            try:
                if self._state is RuntimeStateV1.READY and not self._backend_closed:
                    deadline = self._clock() + 5_000_000_000
                    action = ActionSnapshotV1(self._observation.episode_id, self._request_sequence,
                                              self._observation.sequence_id, deadline)
                    self._request_sequence += 1
                    request = resolve_observation_request(self._observation_schema, None)
                    result = self._backend_step(action, deadline, request)
                    self._check_deadline(deadline)
                    self._validate_result(action, result, request)
                    if result.receipt.status not in {"executed", "confirmed_local"}:
                        raise ContractViolation("close neutral was not locally accepted")
                    self._trace.write("close_release", {"action": action, "backend_result": result})
            except Exception as error:
                self._cleanup_failures.append(self._exception_failure(error, FailureCodeV0.CLEANUP, classify=False))
            finally:
                self._arbiter.clear()
                try:
                    self._close_backend()
                finally:
                    try:
                        self._trace.close()
                    except Exception as error:
                        self._cleanup_failures.append(self._exception_failure(error, FailureCodeV0.CLEANUP, classify=False))
                    finally:
                        self._state = RuntimeStateV1.CLOSED

    def _close_backend(self) -> None:
        if self._backend_closed:
            return
        self._backend_closed = True
        try:
            self._backend.close()
        except Exception as error:
            self._cleanup_failures.append(self._exception_failure(error, FailureCodeV0.CLEANUP, classify=False))

    def _seal(self, state: RuntimeStateV1 = RuntimeStateV1.FAILED) -> None:
        self._state = state
        self._arbiter.clear()
        self._close_backend()

    def _check_deadline(self, deadline: int) -> None:
        if self._clock() >= deadline:
            raise TimeoutError("Runtime deadline expired")

    def _require_ready(self) -> None:
        if self._state is not RuntimeStateV1.READY:
            raise ContractViolation("formal Runtime must be ready")

    @staticmethod
    def _exception_failure(error: Exception, code: FailureCodeV0, *, classify: bool = True) -> FailureV0:
        if classify:
            if isinstance(error, TimeoutError): code = FailureCodeV0.DEADLINE_EXCEEDED
            elif isinstance(error, ContractViolation) and code is not FailureCodeV0.CONTRACT:
                code = FailureCodeV0.OBSERVATION_INVARIANT
        return FailureV0(code, str(error) or code.value, True, "runtime",
                         json.dumps({"exception_type": type(error).__name__}))

    def _record_failure(self, kind: str, payload: object) -> None:
        try:
            self._trace.write(kind, payload)
        except Exception as error:
            self._cleanup_failures.append(self._exception_failure(error, FailureCodeV0.TRACE_IO, classify=False))

    def _report(self, task: TaskIntentV0, decision: ArbitrationDecisionV1 | None,
                status: ExecutionStatusV0, phase: str, failure: FailureV0 | None,
                observation: ObservationSnapshotV2 | ObservationSnapshotV3 | None) -> ExecutionReportV0:
        return ExecutionReportV0(f"report-{self._step_number}", task.task_id,
            self._observation.episode_id if self._observation else "unknown-episode", self._step_number,
            status, phase, 0.0, tuple(dict.fromkeys(i for _, i in decision.selected_intents)) if decision else (),
            decision.action.request_sequence_id if decision else None,
            observation.sequence_id if observation else None, failure=failure)

    def __enter__(self) -> "PlayerRuntimeV1":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
