"""One-process Player Runtime worker for timing and capacity probes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable, Protocol, Sequence

import psutil

from mc2p.backends.craftground import CraftGroundBackendV0
from mc2p.backends.craftground_runtime import (
    CraftGroundClockModeV0,
    CraftGroundObservationModeV0,
    validate_sandbox_for_mode,
)
from mc2p.contracts.action import (
    ActionIntentV0,
    ActionPriorityV0,
    CameraActionV0,
    LocomotionActionV0,
)
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import FieldStatusV0, FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ObservationSnapshotV2
from mc2p.contracts.report import ExecutionStatusV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.task import (
    ComparisonOperatorV0,
    SuccessCriterionV0,
    TaskIntentV0,
)
from mc2p.runtime.player_runtime import PlayerRuntimeV0, RuntimeStateV0
from mc2p.runtime.trace import JsonlTraceWriterV0, trace_projection
from scripts.control_probe_core import write_json_atomic
from scripts.timing_parallel_probe_core import (
    CAPACITY_STEP_COUNT,
    CAPACITY_WARMUP_STEPS,
    TIMING_STEP_COUNT,
    ScheduledIntentV0,
    TimingRunResultV0,
    TimingSampleV0,
    build_capacity_schedule,
    build_timing_schedule,
)


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
REFERENCE_PERIOD_SECONDS = 0.05


class ProbeWorkerKindV0(StrEnum):
    TIMING = "timing"
    CAPACITY = "capacity"


@dataclass(frozen=True, slots=True)
class ProbeWorkerConfigV0:
    worker_id: str
    kind: ProbeWorkerKindV0
    run_dir: Path
    sandbox_path: Path
    clock_mode: CraftGroundClockModeV0
    seed: int
    port: int
    attempt: int = 1
    timeout_seconds: float = 300.0
    warmup_steps: int = CAPACITY_WARMUP_STEPS
    measured_steps: int = CAPACITY_STEP_COUNT
    timing_steps: int = TIMING_STEP_COUNT
    fault_pause_at_step: int | None = None
    schema_version: str = field(default="mc2p.probe-worker-config.v0", init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker_id, str)
            or not _SAFE_ID.fullmatch(self.worker_id)
            or self.worker_id in {".", ".."}
        ):
            raise ValueError("worker_id must be one safe path component")
        if not isinstance(self.kind, ProbeWorkerKindV0):
            raise ValueError("worker kind is invalid")
        if not Path(self.run_dir).is_absolute():
            raise ValueError("worker run_dir must be absolute")
        if not Path(self.sandbox_path).is_absolute():
            raise ValueError("worker sandbox_path must be absolute")
        if not isinstance(self.clock_mode, CraftGroundClockModeV0):
            raise ValueError("worker clock mode is invalid")
        validate_sandbox_for_mode(
            Path(self.sandbox_path),
            self.clock_mode,
            CraftGroundObservationModeV0.STRUCTURED_ONLY,
        )
        if self.kind is ProbeWorkerKindV0.CAPACITY and self.clock_mode is not CraftGroundClockModeV0.ACCELERATED:
            raise ValueError("capacity workers require accelerated clock mode")
        if type(self.seed) is not int:
            raise ValueError("worker seed must be an integer")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("worker port must be between 1 and 65535")
        if type(self.attempt) is not int or self.attempt not in {1, 2}:
            raise ValueError("worker attempt must be one or two")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("worker timeout must be finite and positive")
        if self.timing_steps != TIMING_STEP_COUNT:
            raise ValueError(f"timing workers require {TIMING_STEP_COUNT} steps")
        if self.warmup_steps != CAPACITY_WARMUP_STEPS:
            raise ValueError(
                f"capacity workers require {CAPACITY_WARMUP_STEPS} warmup steps"
            )
        if self.measured_steps != CAPACITY_STEP_COUNT:
            raise ValueError(
                f"capacity workers require {CAPACITY_STEP_COUNT} measured steps"
            )
        if self.fault_pause_at_step is not None:
            if self.kind is not ProbeWorkerKindV0.CAPACITY:
                raise ValueError("fault pause is only valid for capacity workers")
            if self.fault_pause_at_step != 64:
                raise ValueError("fault workers must pause after exactly 64 measured steps")


@dataclass(frozen=True, slots=True)
class WorkerEventV0:
    event_type: str
    worker_id: str
    monotonic_ns: int
    pid: int
    process_create_time: float
    port: int
    sequence: int | None = None
    detail: str | None = None
    schema_version: str = field(default="mc2p.worker-event.v0", init=False)


class _EventLike(Protocol):
    def put(self, value: WorkerEventV0) -> object: ...


def _emit(
    sink: Callable[[WorkerEventV0], object] | _EventLike,
    event: WorkerEventV0,
) -> None:
    put = getattr(sink, "put", None)
    if callable(put):
        put(event)
    else:
        sink(event)  # type: ignore[operator]


def intent_for_scheduled_action(
    action: ScheduledIntentV0,
    *,
    cycle: int,
    now_ns: int,
    deadline_ns: int,
    source_id: str,
) -> ActionIntentV0:
    if action.cycle != cycle:
        raise ValueError("scheduled action cycle does not match")
    return ActionIntentV0(
        intent_id=f"{source_id}-action",
        source_id=source_id,
        priority=ActionPriorityV0.TASK,
        submitted_at_monotonic_ns=now_ns,
        expires_at_monotonic_ns=deadline_ns,
        locomotion=LocomotionActionV0(
            forward=action.forward,
            back=action.back,
            left=action.left,
            right=action.right,
            jump=action.jump,
            sneak=action.sneak,
            sprint=action.sprint,
        ),
        camera=CameraActionV0(
            pitch_delta=action.camera_pitch_delta,
            yaw_delta=action.camera_yaw_delta,
        ),
    )


def _task(kind: ProbeWorkerKindV0, deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0(
        task_id=f"probe-{kind.value}",
        task_type=f"probe.{kind.value}",
        parameters_json="{}",
        success_criteria=(
            SuccessCriterionV0(
                metric="probe.complete",
                operator=ComparisonOperatorV0.EQUAL,
                target_value=1.0,
                unit="boolean",
            ),
        ),
        priority=200,
        deadline_monotonic_ns=deadline_ns,
        interruptible=True,
        max_risk=0.0,
    )


def _required(field: FieldValueV0[Any], name: str) -> Any:
    if field.status is not FieldStatusV0.VALID or field.value is None:
        raise ValueError(f"observation field {name} is not valid")
    return field.value


def _timing_sample(
    observation: ObservationSnapshotV2,
    *,
    action: ScheduledIntentV0,
    latency_seconds: float,
    terminated: bool,
    truncated: bool,
) -> TimingSampleV0:
    position = _required(observation.position, "position")
    if not isinstance(position, Vec3V0):
        raise ValueError("observation position is not Vec3V0")
    return TimingSampleV0(
        cycle=action.cycle,
        phase=action.phase,
        action_sequence_id=int(observation.request_sequence_id),
        observation_sequence_id=observation.sequence_id,
        request_sequence_id=observation.request_sequence_id,
        world_time_ticks=int(_required(observation.world_time_ticks, "world_time_ticks")),
        x=float(position.x),
        y=float(position.y),
        z=float(position.z),
        yaw_degrees=float(_required(observation.yaw_degrees, "yaw_degrees")),
        pitch_degrees=float(_required(observation.pitch_degrees, "pitch_degrees")),
        is_on_ground=bool(_required(observation.is_on_ground, "is_on_ground")),
        is_dead=bool(_required(observation.is_dead, "is_dead")),
        health_points=float(_required(observation.health_points, "health_points")),
        food_points=float(_required(observation.food_points, "food_points")),
        step_latency_seconds=latency_seconds,
        terminated=terminated,
        truncated=truncated,
    )


def _port_accepts_connections(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.1)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _default_backend(config: ProbeWorkerConfigV0) -> CraftGroundBackendV0:
    return CraftGroundBackendV0(
        port=config.port,
        runtime_env_path=config.sandbox_path,
        clock_mode=config.clock_mode,
        observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
    )


def _perform_action(
    runtime: PlayerRuntimeV0,
    action: ScheduledIntentV0,
    task: TaskIntentV0,
    profile: BehaviorProfileV0,
    deadline_ns: int,
    clock_ns: Callable[[], int],
    source_id: str,
) -> tuple[ObservationSnapshotV2, float, bool, bool]:
    runtime.submit_intent(
        intent_for_scheduled_action(
            action,
            cycle=action.cycle,
            now_ns=clock_ns(),
            deadline_ns=deadline_ns,
            source_id=source_id,
        )
    )
    started = clock_ns()
    result = runtime.step(task, profile, deadline_ns)
    elapsed = (clock_ns() - started) / 1_000_000_000
    if result.report.status is not ExecutionStatusV0.RUNNING:
        message = (
            result.report.failure.message
            if result.report.failure is not None
            else result.report.status.value
        )
        raise RuntimeError(f"runtime step failed: {message}")
    if result.observation is None:
        raise RuntimeError("runtime step returned no observation")
    return result.observation, elapsed, result.terminated, result.truncated


def _raise_if_episode_ended(*, terminated: bool, truncated: bool) -> None:
    if terminated:
        raise RuntimeError("backend episode terminated during probe")
    if truncated:
        raise RuntimeError("backend episode truncated during probe")


def _wait_for_start(
    start_event: Any,
    cancel_event: Any,
    deadline_ns: int,
    clock_ns: Callable[[], int],
) -> None:
    while not start_event.is_set():
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("capacity worker cancelled before measured start")
        remaining = (deadline_ns - clock_ns()) / 1_000_000_000
        if remaining <= 0:
            raise TimeoutError("capacity start barrier timed out")
        start_event.wait(timeout=min(0.1, remaining))


def run_worker(
    config: ProbeWorkerConfigV0,
    event_sink: Callable[[WorkerEventV0], object] | _EventLike,
    *,
    start_event: Any = None,
    cancel_event: Any = None,
    fault_continue_event: Any = None,
    backend_factory: Callable[[ProbeWorkerConfigV0], Any] = _default_backend,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    run_dir = Path(config.run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    process = psutil.Process(os.getpid())
    process_create_time = process.create_time()

    def emit(event_type: str, sequence: int | None = None, detail: str | None = None) -> None:
        _emit(
            event_sink,
            WorkerEventV0(
                event_type=event_type,
                worker_id=config.worker_id,
                monotonic_ns=clock_ns(),
                pid=os.getpid(),
                process_create_time=process_create_time,
                port=config.port,
                sequence=sequence,
                detail=detail,
            ),
        )

    emit("started")
    trace_path = run_dir / "trace.jsonl"
    backend: Any = None
    runtime: PlayerRuntimeV0 | None = None
    primary_failure: str | None = None
    samples: list[TimingSampleV0] = []
    step_latencies: list[float] = []
    warmup_completed = 0
    measured_completed = 0
    reset_seconds: float | None = None
    reset_world_time_ticks: int | None = None
    measured_elapsed_seconds: float | None = None
    started_ns = clock_ns()
    deadline_ns = started_ns + round(config.timeout_seconds * 1_000_000_000)
    task = _task(config.kind, deadline_ns)
    profile = BehaviorProfileV0()
    cleanup_failures: list[str] = []
    cleanup = {
        "actual_port": config.port,
        "process_stopped": True,
        "port_released": not _port_accepts_connections(config.port),
    }
    try:
        backend = backend_factory(config)
        runtime = PlayerRuntimeV0(
            backend,
            JsonlTraceWriterV0(trace_path),
            clock_ns=clock_ns,
        )
        reset_started = clock_ns()
        reset = runtime.reset(
            ResetRequestV0(
                request_id=f"reset-{config.worker_id}",
                episode_id=f"episode-{config.worker_id}",
                scenario_id="flat-safe",
                seed=config.seed,
                deadline_monotonic_ns=deadline_ns,
            )
        )
        reset_seconds = (clock_ns() - reset_started) / 1_000_000_000
        if not reset.succeeded:
            assert reset.failure is not None
            raise RuntimeError(f"runtime reset failed: {reset.failure.message}")
        if reset.observation is None:
            raise RuntimeError("runtime reset returned no observation")
        reset_world_time_ticks = int(
            _required(reset.observation.world_time_ticks, "world_time_ticks")
        )
        if int(getattr(backend, "actual_port")) != config.port:
            raise RuntimeError(
                f"worker requested port {config.port} but backend uses {backend.actual_port}"
            )
        emit("runtime_ready")

        if config.kind is ProbeWorkerKindV0.TIMING:
            schedule = build_timing_schedule()
            pacing_origin_ns = clock_ns()
            for action in schedule:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("timing worker cancelled")
                if config.clock_mode is CraftGroundClockModeV0.REFERENCE_20_TPS:
                    target_ns = pacing_origin_ns + round(
                        action.cycle * REFERENCE_PERIOD_SECONDS * 1_000_000_000
                    )
                    remaining_seconds = (target_ns - clock_ns()) / 1_000_000_000
                    if remaining_seconds > 0:
                        sleep(remaining_seconds)
                observation, latency, terminated, truncated = _perform_action(
                    runtime,
                    action,
                    task,
                    profile,
                    deadline_ns,
                    clock_ns,
                    config.worker_id,
                )
                samples.append(
                    _timing_sample(
                        observation,
                        action=action,
                        latency_seconds=latency,
                        terminated=terminated,
                        truncated=truncated,
                    )
                )
                _raise_if_episode_ended(
                    terminated=terminated,
                    truncated=truncated,
                )
                emit("progress", sequence=action.cycle + 1)
        else:
            schedule = build_capacity_schedule()
            for action in schedule[:CAPACITY_WARMUP_STEPS]:
                _observation, _latency, terminated, truncated = _perform_action(
                    runtime,
                    action,
                    task,
                    profile,
                    deadline_ns,
                    clock_ns,
                    config.worker_id,
                )
                _raise_if_episode_ended(
                    terminated=terminated,
                    truncated=truncated,
                )
                warmup_completed += 1
            emit("warmup_complete", sequence=warmup_completed)
            if start_event is None:
                raise ValueError("capacity worker requires a measured start event")
            _wait_for_start(start_event, cancel_event, deadline_ns, clock_ns)
            emit("measured_started")
            measured_started = clock_ns()
            for action in schedule:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("capacity worker cancelled")
                _observation, latency, terminated, truncated = _perform_action(
                    runtime,
                    action,
                    task,
                    profile,
                    deadline_ns,
                    clock_ns,
                    config.worker_id,
                )
                step_latencies.append(latency)
                measured_completed += 1
                _raise_if_episode_ended(
                    terminated=terminated,
                    truncated=truncated,
                )
                if measured_completed % 16 == 0:
                    emit("progress", sequence=measured_completed)
                if measured_completed == config.fault_pause_at_step:
                    if fault_continue_event is None:
                        raise ValueError(
                            "fault worker requires a parent-controlled continue event"
                        )
                    emit("fault_pause", sequence=measured_completed)
                    _wait_for_start(
                        fault_continue_event,
                        cancel_event,
                        deadline_ns,
                        clock_ns,
                    )
                    emit("fault_resumed", sequence=measured_completed)
            measured_elapsed_seconds = (clock_ns() - measured_started) / 1_000_000_000
        emit("finished", sequence=len(samples) or measured_completed)
    except BaseException as error:
        primary_failure = f"{type(error).__name__}: {error}"
        emit("failed", detail=primary_failure)
    finally:
        if runtime is not None and runtime.state is RuntimeStateV0.READY:
            try:
                runtime.cancel("probe worker complete")
                final = runtime.step(task, profile, deadline_ns)
                if final.report.status is not ExecutionStatusV0.CANCELLED:
                    cleanup_failures.append(
                        f"neutral cancellation returned {final.report.status.value}"
                    )
            except BaseException as error:
                cleanup_failures.append(
                    f"neutral cancellation failed: {type(error).__name__}: {error}"
                )
        if runtime is not None:
            runtime.close()
            cleanup_failures.extend(
                f"{failure.code.value}: {failure.message}"
                for failure in runtime.cleanup_failures
            )
        elif backend is not None:
            try:
                backend.close()
            except BaseException as error:
                cleanup_failures.append(
                    f"backend close failed: {type(error).__name__}: {error}"
                )
        status = getattr(backend, "cleanup_status", None)
        if status is not None:
            cleanup = {
                "actual_port": int(status.actual_port),
                "process_stopped": bool(status.process_stopped),
                "port_released": bool(status.port_released),
            }
        else:
            cleanup["port_released"] = not _port_accepts_connections(config.port)
        if not cleanup["process_stopped"]:
            cleanup_failures.append("captured CraftGround process did not stop")
        if not cleanup["port_released"]:
            cleanup_failures.append("actual CraftGround port did not release")

    timing_run: TimingRunResultV0 | None = None
    if config.kind is ProbeWorkerKindV0.TIMING:
        timing_run = TimingRunResultV0(
            worker_id=config.worker_id,
            clock_mode=config.clock_mode,
            seed=config.seed,
            attempt=config.attempt,
            wall_clock_paced=config.clock_mode
            is CraftGroundClockModeV0.REFERENCE_20_TPS,
            sandbox_path=str(config.sandbox_path),
            requested_port=config.port,
            actual_port=int(cleanup["actual_port"]),
            samples=tuple(samples),
            primary_failure=primary_failure,
            cleanup_failures=tuple(cleanup_failures),
            process_stopped=bool(cleanup["process_stopped"]),
            port_released=bool(cleanup["port_released"]),
            reset_world_time_ticks=reset_world_time_ticks,
        )
    passed = primary_failure is None and not cleanup_failures
    if config.kind is ProbeWorkerKindV0.TIMING:
        passed = passed and len(samples) == TIMING_STEP_COUNT
    else:
        passed = (
            passed
            and warmup_completed == CAPACITY_WARMUP_STEPS
            and measured_completed == CAPACITY_STEP_COUNT
        )
    report = {
        "schema_version": "mc2p.probe-worker-result.v0",
        "worker_id": config.worker_id,
        "kind": config.kind.value,
        "status": "passed" if passed else "failed",
        "seed": config.seed,
        "attempt": config.attempt,
        "clock_mode": config.clock_mode.value,
        "requested_port": config.port,
        "actual_port": int(cleanup["actual_port"]),
        "reset_seconds": reset_seconds,
        "warmup_steps": warmup_completed,
        "measured_steps": measured_completed,
        "measured_elapsed_seconds": measured_elapsed_seconds,
        "step_latencies_seconds": step_latencies,
        "timing_run": None if timing_run is None else trace_projection(timing_run),
        "primary_failure": primary_failure,
        "cleanup_failures": cleanup_failures,
        "cleanup": cleanup,
        "elapsed_seconds": (clock_ns() - started_ns) / 1_000_000_000,
        "trace_file": trace_path.name,
    }
    write_json_atomic(run_dir / "result.json", report)
    emit("cleanup_finished", detail="passed" if passed else "failed")
    return 0 if passed else 2
