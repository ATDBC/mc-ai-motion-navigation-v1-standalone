from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mc2p.contracts.action import (
    ActionIntentV0,
    ActionPriorityV0,
    CameraActionV0,
    LocomotionActionV0,
)
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.report import ExecutionStatusV0, FailureCodeV0
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.contracts.task import (
    ComparisonOperatorV0,
    SuccessCriterionV0,
    TaskIntentV0,
)
from mc2p.runtime.backend import BackendStepResultV0
from mc2p.runtime.player_runtime import PlayerRuntimeV0, RuntimeStateV0
from mc2p.runtime.trace import JsonlTraceWriterV0
from tests.observation_v2_fixtures import valid_snapshot_v2


class _FakeBackend:
    def __init__(self) -> None:
        self.actions = []
        self.close_calls = 0
        self.reset_error: Exception | None = None
        self.step_error: Exception | None = None
        self.close_error: Exception | None = None
        self.wrong_episode = False
        self.terminated = False
        self.truncated = False
        self._episode_id = ""
        self._observation_sequence = 0

    def reset(self, request: ResetRequestV0) -> ResetResultV0:
        if self.reset_error is not None:
            raise self.reset_error
        self._episode_id = request.episode_id
        self._observation_sequence = 0
        return ResetResultV0(
            request_id=request.request_id,
            actual_episode_id=request.episode_id,
            succeeded=True,
            observation=valid_snapshot_v2(
                episode_id=request.episode_id,
                sequence_id=0,
                request_sequence_id=None,
            ),
        )

    def step(self, action, deadline_monotonic_ns: int) -> BackendStepResultV0:
        self.actions.append(action)
        if self.step_error is not None:
            raise self.step_error
        self._observation_sequence += 1
        episode_id = "wrong-episode" if self.wrong_episode else self._episode_id
        return BackendStepResultV0(
            observation=valid_snapshot_v2(
                episode_id=episode_id,
                sequence_id=self._observation_sequence,
                request_sequence_id=action.action_sequence_id,
            ),
            reward=0.0,
            terminated=self.terminated,
            truncated=self.truncated,
        )

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _RecordingTrace:
    def __init__(self) -> None:
        self.records: list[tuple[str, object]] = []
        self.closed = False
        self.fail_writes = False

    def write(self, record_type: str, payload: object) -> None:
        if self.fail_writes:
            raise OSError("trace disk full")
        self.records.append((record_type, payload))

    def close(self) -> None:
        self.closed = True


def _reset_request(deadline: int = 1000) -> ResetRequestV0:
    return ResetRequestV0(
        request_id="reset-1",
        episode_id="episode-1",
        scenario_id="flat-safe",
        seed=7,
        deadline_monotonic_ns=deadline,
    )


def _task(deadline: int = 1000, interruptible: bool = True) -> TaskIntentV0:
    return TaskIntentV0(
        task_id="task-1",
        task_type="mc2p.runtime.control-probe",
        parameters_json="{}",
        success_criteria=(
            SuccessCriterionV0(
                metric="runtime-step",
                operator=ComparisonOperatorV0.GREATER_THAN_OR_EQUAL,
                target_value=1.0,
                unit="count",
            ),
        ),
        priority=100,
        deadline_monotonic_ns=deadline,
        interruptible=interruptible,
        max_risk=0.0,
    )


def _intent(group: str) -> ActionIntentV0:
    values = {
        "intent_id": f"{group}-1",
        "source_id": group,
        "priority": ActionPriorityV0.TASK,
        "submitted_at_monotonic_ns": 10,
        "expires_at_monotonic_ns": 900,
    }
    if group == "move":
        return ActionIntentV0(
            **values,
            locomotion=LocomotionActionV0(forward=True),
        )
    return ActionIntentV0(
        **values,
        camera=CameraActionV0(yaw_delta=15.0),
    )


class PlayerRuntimeTests(unittest.TestCase):
    def _ready_runtime(
        self,
        backend: _FakeBackend | None = None,
        trace: _RecordingTrace | None = None,
    ) -> tuple[PlayerRuntimeV0, _FakeBackend, _RecordingTrace]:
        backend = backend or _FakeBackend()
        trace = trace or _RecordingTrace()
        runtime = PlayerRuntimeV0(backend, trace, clock_ns=lambda: 100)
        result = runtime.reset(_reset_request())
        self.assertTrue(result.succeeded)
        return runtime, backend, trace

    def test_successful_reset_and_step_merge_action_groups(self) -> None:
        runtime, backend, trace = self._ready_runtime()
        runtime.submit_intent(_intent("move"))
        runtime.submit_intent(_intent("look"))

        result = runtime.step(_task(), BehaviorProfileV0(), 800)

        self.assertEqual(result.report.status, ExecutionStatusV0.RUNNING)
        self.assertTrue(result.observation is not None)
        self.assertEqual(result.observation.request_sequence_id, 0)
        self.assertTrue(backend.actions[0].locomotion.forward)
        self.assertEqual(backend.actions[0].camera.yaw_delta, 15.0)
        self.assertEqual([kind for kind, _ in trace.records], ["reset", "step"])

    def test_step_before_reset_and_after_close_is_rejected(self) -> None:
        runtime = PlayerRuntimeV0(_FakeBackend(), _RecordingTrace(), lambda: 100)
        with self.assertRaisesRegex(ContractViolation, "ready"):
            runtime.step(_task(), BehaviorProfileV0(), 800)
        runtime.close()
        with self.assertRaisesRegex(ContractViolation, "closed"):
            runtime.reset(_reset_request())

    def test_expired_deadline_does_not_call_backend(self) -> None:
        runtime, backend, _ = self._ready_runtime()

        result = runtime.step(_task(deadline=100), BehaviorProfileV0(), 100)

        self.assertEqual(result.report.status, ExecutionStatusV0.TIMED_OUT)
        self.assertEqual(result.report.failure.code, FailureCodeV0.DEADLINE_EXCEEDED)
        self.assertEqual(backend.actions, [])
        self.assertEqual(runtime.state, RuntimeStateV0.FAILED)

    def test_backend_reset_timeout_keeps_deadline_failure_code(self) -> None:
        backend = _FakeBackend()
        backend.reset_error = TimeoutError("reset deadline crossed")
        runtime = PlayerRuntimeV0(backend, _RecordingTrace(), lambda: 100)

        result = runtime.reset(_reset_request())

        self.assertFalse(result.succeeded)
        self.assertEqual(result.failure.code, FailureCodeV0.DEADLINE_EXCEEDED)
        self.assertEqual(runtime.state, RuntimeStateV0.FAILED)

    def test_backend_exception_becomes_structured_failure(self) -> None:
        backend = _FakeBackend()
        backend.step_error = ConnectionError("socket closed")
        runtime, _, _ = self._ready_runtime(backend=backend)

        result = runtime.step(_task(), BehaviorProfileV0(), 800)

        self.assertEqual(result.report.status, ExecutionStatusV0.FAILED)
        self.assertEqual(result.report.failure.code, FailureCodeV0.BACKEND_IO)
        self.assertEqual(runtime.state, RuntimeStateV0.FAILED)

    def test_step_surfaces_backend_episode_status(self) -> None:
        backend = _FakeBackend()
        backend.terminated = True
        backend.truncated = True
        runtime, _, _ = self._ready_runtime(backend=backend)

        result = runtime.step(_task(), BehaviorProfileV0(), 800)

        self.assertTrue(result.terminated)
        self.assertTrue(result.truncated)

    def test_wrong_episode_is_observation_invariant_failure(self) -> None:
        backend = _FakeBackend()
        backend.wrong_episode = True
        runtime, _, _ = self._ready_runtime(backend=backend)

        result = runtime.step(_task(), BehaviorProfileV0(), 800)

        self.assertEqual(
            result.report.failure.code,
            FailureCodeV0.OBSERVATION_INVARIANT,
        )
        self.assertEqual(runtime.state, RuntimeStateV0.FAILED)

    def test_cancel_writes_neutral_at_next_step_boundary(self) -> None:
        runtime, backend, _ = self._ready_runtime()
        runtime.submit_intent(_intent("move"))
        runtime.cancel("player requested stop")

        result = runtime.step(_task(), BehaviorProfileV0(), 800)

        self.assertEqual(result.report.status, ExecutionStatusV0.CANCELLED)
        self.assertEqual(result.report.failure.code, FailureCodeV0.CANCELLED)
        self.assertFalse(backend.actions[-1].locomotion.forward)
        self.assertEqual(backend.actions[-1].camera.yaw_delta, 0.0)
        self.assertEqual(runtime.state, RuntimeStateV0.CANCELLED)
        with self.assertRaisesRegex(ContractViolation, "ready"):
            runtime.step(_task(), BehaviorProfileV0(), 800)

    def test_trace_failure_stops_runtime_after_backend_action(self) -> None:
        trace = _RecordingTrace()
        runtime, backend, _ = self._ready_runtime(trace=trace)
        trace.fail_writes = True

        result = runtime.step(_task(), BehaviorProfileV0(), 800)

        self.assertEqual(result.report.failure.code, FailureCodeV0.TRACE_IO)
        self.assertEqual(len(backend.actions), 1)
        self.assertEqual(runtime.state, RuntimeStateV0.FAILED)

    def test_close_is_idempotent_and_preserves_cleanup_failure(self) -> None:
        backend = _FakeBackend()
        backend.close_error = OSError("cannot close")
        runtime, _, trace = self._ready_runtime(backend=backend)

        runtime.close()
        runtime.close()

        self.assertEqual(backend.close_calls, 1)
        self.assertTrue(trace.closed)
        self.assertEqual(runtime.state, RuntimeStateV0.CLOSED)
        self.assertEqual(runtime.cleanup_failure.code, FailureCodeV0.CLEANUP)

    def test_close_preserves_neutral_and_backend_cleanup_failures(self) -> None:
        backend = _FakeBackend()
        runtime, _, _ = self._ready_runtime(backend=backend)
        backend.step_error = TimeoutError("neutral timed out")
        backend.close_error = OSError("cannot close")

        runtime.close()

        self.assertEqual(len(runtime.cleanup_failures), 2)
        self.assertIn("neutral release", runtime.cleanup_failures[0].message)
        self.assertEqual(runtime.cleanup_failures[1].message, "cannot close")


class TraceWriterTests(unittest.TestCase):
    def test_formal_observation_trace_contains_no_image_field(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            writer = JsonlTraceWriterV0(path)
            writer.write("observation", valid_snapshot_v2())
            content_before_close = path.read_text(encoding="utf-8")
            writer.close()

        record = json.loads(content_before_close)
        self.assertEqual(record["payload"]["schema_version"], "mc2p.observation.v2")
        self.assertNotIn('"pov"', content_before_close)
        self.assertNotIn('"rgb"', content_before_close)


if __name__ == "__main__":
    unittest.main()
