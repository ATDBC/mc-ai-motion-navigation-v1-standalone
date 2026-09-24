"""Run the stage-gated CraftGround timing and parallel-capacity probe."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
import importlib.metadata
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
from queue import Empty
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

import psutil


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from mc2p.backends.craftground_runtime import (
    MC121_RUNTIME_0_1_0_FINGERPRINTS,
    CraftGroundClockModeV0,
    CraftGroundObservationModeV0,
    PreparedCraftGroundRuntimeV0,
    capture_runtime_source_fingerprints,
    prepare_runtime_sandbox,
    resolve_mc121_runtime_path,
    validate_sandbox_for_mode,
)
from mc2p.runtime.trace import trace_projection
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.timing_parallel_probe_core import (
    CAPACITY_STEP_COUNT,
    TIMING_SEEDS,
    CapacityBatchResultV0,
    CheckResultV0,
    ProcessIdentityError,
    ProcessIdentityV0,
    ProcessTreeCleanupV0,
    ResourceSampleV0,
    TimingPairReportV0,
    TimingRunResultV0,
    TimingSampleV0,
    TimingVerdictV0,
    aggregate_timing_verdict,
    capture_registered_tree,
    compare_timing_pair,
    evaluate_resource_guard,
    evaluate_lockstep_run,
    parse_lockstep_trace_lines,
    query_host_resources,
    summarize_capacity,
    terminate_registered_tree,
    validate_timing_run,
)
from scripts.timing_parallel_worker import (
    ProbeWorkerConfigV0,
    ProbeWorkerKindV0,
    WorkerEventV0,
    run_worker,
)


DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "craftground-timing-parallel"
DEFAULT_BASE_PORT = 8130
DEFAULT_WORKER_TIMEOUT_SECONDS = 300.0
DEFAULT_BUILD_TIMEOUT_SECONDS = 300.0
CAPACITY_BATCH_COUNT = 3
MIN_FREE_BYTES = 10 * 1024**3
RESOURCE_SAMPLE_PERIOD_SECONDS = 0.5
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7897
_SAFE_COMPONENT = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True, slots=True)
class ProbeConfigV0:
    artifact_root: Path
    base_port: int = DEFAULT_BASE_PORT
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS
    build_timeout_seconds: float = DEFAULT_BUILD_TIMEOUT_SECONDS
    capacity_batches: int = CAPACITY_BATCH_COUNT
    reference_smoke: bool = False
    lockstep_probe: bool = False
    seeds: tuple[int, ...] = TIMING_SEEDS

    def __post_init__(self) -> None:
        root = Path(self.artifact_root)
        if not root.is_absolute():
            raise ValueError("artifact root must be absolute")
        if type(self.base_port) is not int or not 1 <= self.base_port <= 65528:
            raise ValueError("base port must leave room for eight candidate ports")
        if not math.isfinite(self.worker_timeout_seconds) or self.worker_timeout_seconds <= 0:
            raise ValueError("worker timeout must be finite and positive")
        if not math.isfinite(self.build_timeout_seconds) or self.build_timeout_seconds <= 0:
            raise ValueError("build timeout must be finite and positive")
        if self.capacity_batches != CAPACITY_BATCH_COUNT:
            raise ValueError("capacity batch count is fixed at three")
        if not self.seeds or any(seed not in TIMING_SEEDS for seed in self.seeds):
            raise ValueError(f"seeds must be a non-empty subset of {TIMING_SEEDS}")

    @classmethod
    def from_namespace(cls, value: argparse.Namespace) -> "ProbeConfigV0":
        return cls(
            artifact_root=Path(value.artifact_root).resolve(),
            base_port=value.base_port,
            worker_timeout_seconds=value.worker_timeout_seconds,
            build_timeout_seconds=value.build_timeout_seconds,
            capacity_batches=value.capacity_batches,
            reference_smoke=value.reference_smoke,
            lockstep_probe=value.lockstep_probe,
            seeds=tuple(value.seeds),
        )

    @property
    def candidate_ports(self) -> tuple[int, ...]:
        return tuple(range(self.base_port, self.base_port + 8))


@dataclass(frozen=True, slots=True)
class PreflightReportV0:
    free_bytes: int
    candidate_ports: tuple[int, ...]
    failures: tuple[str, ...]
    schema_version: str = field(default="mc2p.probe-preflight.v0", init=False)

    @property
    def passed(self) -> bool:
        return not self.failures


class TimingAttemptDispositionV0(StrEnum):
    ACCEPT = "accept"
    RETRY = "retry"
    STOP = "stop"


class _StopProbe(Exception):
    """Internal control flow after an expected stage-gate stop."""


@dataclass(frozen=True, slots=True)
class CapacityStageOutcomeV0:
    passed: bool
    guard_reasons: tuple[str, ...] = ()
    scale_block_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CapacityStageGateReportV0:
    events: tuple[str, ...]
    stop_reason: str | None
    completed: bool
    schema_version: str = field(default="mc2p.capacity-stage-gate.v0", init=False)


def make_run_id(
    *,
    now: datetime | None = None,
    unique: str | None = None,
) -> str:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    timestamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    component = _SAFE_COMPONENT.sub("-", (unique or uuid.uuid4().hex[:8]).casefold()).strip("-")
    if not component:
        raise ValueError("run id unique component is empty after sanitization")
    return f"{timestamp}-{component}"


def allocate_run_directory(root: Path) -> Path:
    artifact_root = Path(root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        candidate = artifact_root / make_run_id()
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not allocate a unique probe artifact directory")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage-gated CraftGround timing equivalence and parallel-capacity probe."
    )
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=DEFAULT_WORKER_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--build-timeout-seconds",
        type=float,
        default=DEFAULT_BUILD_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--capacity-batches",
        type=int,
        choices=(CAPACITY_BATCH_COUNT,),
        default=CAPACITY_BATCH_COUNT,
        help="Fixed repeated batches per capacity (locked to 3).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--reference-smoke",
        action="store_true",
        help="Run one reference 20 TPS timing worker and stop.",
    )
    mode.add_argument(
        "--lockstep-probe",
        action="store_true",
        help="Run the strict accelerated lockstep probe and stop.",
    )
    parser.add_argument(
        "--seeds",
        nargs="+",
        type=int,
        choices=TIMING_SEEDS,
        default=TIMING_SEEDS,
        help="Fixed timing seeds to run (default: all three).",
    )
    return parser


def evaluate_preflight(
    *,
    free_bytes: int,
    candidate_ports: Sequence[int],
    port_is_free: Callable[[int], bool],
) -> PreflightReportV0:
    failures: list[str] = []
    if type(free_bytes) is not int or free_bytes < MIN_FREE_BYTES:
        failures.append("disk-free-below-10-gib")
    ports = tuple(candidate_ports)
    if len(ports) != 8 or len(set(ports)) != 8:
        failures.append("candidate-port-set-invalid")
    for port in ports:
        if type(port) is not int or not 1 <= port <= 65535 or not port_is_free(port):
            failures.append(f"port-{port}-unavailable")
    return PreflightReportV0(free_bytes, ports, tuple(failures))


def evaluate_postflight(
    *,
    candidate_ports: Sequence[int],
    port_is_free: Callable[[int], bool],
    actual_source_fingerprints: Mapping[str, str],
    expected_source_fingerprints: Mapping[str, str],
) -> tuple[str, ...]:
    failures = [
        f"port-{port}-not-released"
        for port in candidate_ports
        if not port_is_free(port)
    ]
    if dict(actual_source_fingerprints) != dict(expected_source_fingerprints):
        failures.append("runtime-source-fingerprint-changed")
    return tuple(failures)


def timing_worker_run_directory(
    run_dir: Path,
    mode: CraftGroundClockModeV0,
    *,
    seed: int,
    attempt: int,
) -> Path:
    return (
        Path(run_dir)
        / "timing"
        / mode.value
        / str(seed)
        / f"attempt-{attempt}"
    ).resolve()


def _capture_lockstep_trace_segment(
    sandbox: Path,
    start_offset: int,
    destination: Path,
) -> None:
    """Copy only the JVM trace bytes appended by one completed worker."""

    if type(start_offset) is not int or start_offset < 0:
        raise ValueError("lockstep trace offset must be a non-negative integer")
    source = Path(sandbox) / "run" / "mc2p-lockstep.jsonl"
    with source.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        end_offset = stream.tell()
        if end_offset < start_offset:
            raise ValueError("lockstep trace shrank while worker was running")
        stream.seek(start_offset)
        payload = stream.read()
    payload.decode("utf-8", errors="strict")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, destination)


def timing_attempt_disposition(
    *, attempt: int, clean: bool, equivalent: bool
) -> TimingAttemptDispositionV0:
    if attempt not in {1, 2}:
        raise ValueError("timing attempt must be one or two")
    if clean and equivalent:
        return TimingAttemptDispositionV0.ACCEPT
    if attempt == 1:
        return TimingAttemptDispositionV0.RETRY
    return TimingAttemptDispositionV0.STOP


def run_capacity_stage_gate(
    timing_verdict: TimingVerdictV0,
    *,
    gpu_monitor_available: bool,
    run_capacity: Callable[[int], CapacityStageOutcomeV0],
    run_fault: Callable[[], CapacityStageOutcomeV0],
) -> CapacityStageGateReportV0:
    if timing_verdict is not TimingVerdictV0.EQUIVALENT:
        return CapacityStageGateReportV0(
            (f"timing_{timing_verdict.value}",), timing_verdict.value, False
        )
    events: list[str] = ["timing_equivalent"]
    events.append("n1_started")
    n1 = run_capacity(1)
    events.append("n1_passed" if n1.passed else "n1_stopped")
    if not n1.passed:
        reason = n1.guard_reasons[0] if n1.guard_reasons else "n1-failed"
        return CapacityStageGateReportV0(tuple(events), reason, False)
    scale_block_reasons = tuple(
        dict.fromkeys(
            (
                *(("gpu-monitor-unavailable",) if not gpu_monitor_available else ()),
                *n1.scale_block_reasons,
            )
        )
    )
    if scale_block_reasons:
        return CapacityStageGateReportV0(
            tuple(events), scale_block_reasons[0], False
        )
    events.append("n2_started")
    n2 = run_capacity(2)
    events.append("n2_passed" if n2.passed else "n2_stopped")
    if not n2.passed:
        reason = n2.guard_reasons[0] if n2.guard_reasons else "n2-failed"
        return CapacityStageGateReportV0(tuple(events), reason, False)
    events.append("fault_started")
    fault = run_fault()
    events.append("fault_passed" if fault.passed else "fault_stopped")
    if not fault.passed:
        reason = fault.guard_reasons[0] if fault.guard_reasons else "fault-isolation-failed"
        return CapacityStageGateReportV0(tuple(events), reason, False)
    events.append("n4_started")
    n4 = run_capacity(4)
    events.append("n4_passed" if n4.passed else "n4_stopped")
    if not n4.passed:
        reason = n4.guard_reasons[0] if n4.guard_reasons else "n4-failed"
        return CapacityStageGateReportV0(tuple(events), reason, False)
    return CapacityStageGateReportV0(tuple(events), None, True)


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _port_accepts_connections(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _proxy_is_listening() -> bool:
    return _port_accepts_connections(PROXY_PORT)


def _worker_process_entry(
    config: ProbeWorkerConfigV0,
    event_queue: Any,
    start_event: Any,
    cancel_event: Any,
    fault_continue_event: Any,
) -> None:
    exit_code = run_worker(
        config,
        event_queue,
        start_event=start_event,
        cancel_event=cancel_event,
        fault_continue_event=fault_continue_event,
    )
    raise SystemExit(exit_code)


@dataclass(slots=True)
class _WorkerHandle:
    config: ProbeWorkerConfigV0
    process: Any
    root: ProcessIdentityV0
    registered: dict[int, ProcessIdentityV0]
    events: list[WorkerEventV0] = field(default_factory=list)


def _process_identity(pid: int) -> ProcessIdentityV0:
    process = psutil.Process(pid)
    return ProcessIdentityV0(pid, process.create_time())


def merge_registered_identities(
    root: ProcessIdentityV0,
    registered: dict[int, ProcessIdentityV0],
    current: Sequence[ProcessIdentityV0],
) -> None:
    """Merge a verified current tree while permitting descendant PID reuse."""

    by_pid = {identity.pid: identity for identity in current}
    if len(by_pid) != len(current):
        raise ProcessIdentityError("current process tree contains duplicate pids")
    if by_pid.get(root.pid) != root:
        raise ProcessIdentityError("registered worker root identity changed")
    for identity in current:
        registered[identity.pid] = identity


def _refresh_registered(handle: _WorkerHandle) -> None:
    try:
        snapshot = capture_registered_tree(handle.root)
    except ProcessIdentityError:
        return
    merge_registered_identities(
        handle.root,
        handle.registered,
        snapshot.identities,
    )


def _drain_events(event_queue: Any, handles: Mapping[str, _WorkerHandle]) -> None:
    while True:
        try:
            event = event_queue.get_nowait()
        except Empty:
            return
        if not isinstance(event, WorkerEventV0):
            raise TypeError("worker event queue contained an invalid value")
        handle = handles.get(event.worker_id)
        if handle is None:
            raise ValueError(f"event from unregistered worker {event.worker_id}")
        if event.pid != handle.root.pid or abs(event.process_create_time - handle.root.create_time) > 0.01:
            raise ProcessIdentityError(
                f"worker event identity mismatch for {event.worker_id}"
            )
        handle.events.append(event)


def _release_continue_events(
    events: Mapping[str, Any],
    *,
    excluded_workers: Sequence[str] = (),
) -> None:
    excluded = set(excluded_workers)
    for worker_id, event in events.items():
        if worker_id not in excluded:
            event.set()


def _cleanup_handle(handle: _WorkerHandle) -> dict[str, object]:
    try:
        cleanup = terminate_registered_tree(
            handle.root,
            tuple(handle.registered.values()),
            grace_seconds=5.0,
        )
        return trace_projection(cleanup)
    except ProcessIdentityError as error:
        return {"errors": [str(error)], "surviving": [], "stopped": []}


def terminate_fault_victim(
    root: ProcessIdentityV0,
    registered: Sequence[ProcessIdentityV0],
    *,
    reported_progress: int,
    grace_seconds: float = 5.0,
) -> ProcessTreeCleanupV0:
    """Terminate one exact registered tree only at the approved fault boundary."""

    if reported_progress != 64:
        raise ValueError("fault injection is only legal at measured progress 64")
    known = {identity.pid: identity for identity in registered}
    snapshot = capture_registered_tree(root)
    merge_registered_identities(root, known, snapshot.identities)
    return terminate_registered_tree(
        root,
        tuple(known.values()),
        grace_seconds=grace_seconds,
    )


def prepare_gradle_environment(
    base_environment: Mapping[str, str],
    *,
    gradle_user_home: Path,
    properties_template: Path,
) -> dict[str, str]:
    """Create a project-scoped Gradle proxy config without `.bat` metacharacters."""

    home = Path(gradle_user_home).resolve()
    template = Path(properties_template).resolve()
    if not template.is_file():
        raise FileNotFoundError(f"Gradle properties template is missing: {template}")
    expected = template.read_bytes()
    home.mkdir(parents=True, exist_ok=True)
    target = home / "gradle.properties"
    if target.exists():
        if not target.is_file() or target.read_bytes() != expected:
            raise RuntimeError(
                f"project Gradle properties conflict with locked template: {target}"
            )
    else:
        shutil.copyfile(template, target)
    environment = dict(base_environment)
    environment.update(
        {
            "GRADLE_USER_HOME": str(home),
            "HTTP_PROXY": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "ALL_PROXY": "http://127.0.0.1:7897",
            "NO_PROXY": "localhost,127.0.0.1,::1,repo.huaweicloud.com",
        }
    )
    return environment


def build_gradle_command(sandbox: Path) -> list[str]:
    root = Path(sandbox).resolve()
    wrapper = (root / "gradlew.bat").resolve()
    if not wrapper.is_file():
        raise FileNotFoundError(f"Gradle wrapper is missing: {wrapper}")
    init_script = (PROJECT_ROOT / "config" / "gradle.init.mirrors.gradle").resolve()
    return [
        str(wrapper),
        "build",
        "--no-daemon",
        "--init-script",
        str(init_script),
    ]


@dataclass(frozen=True, slots=True)
class BoundedProcessResultV0:
    return_code: int | None
    primary_failure: str | None
    cleanup_failures: tuple[str, ...]
    process_stopped: bool
    registered_processes: tuple[ProcessIdentityV0, ...]
    schema_version: str = field(default="mc2p.bounded-process-result.v0", init=False)


def run_bounded_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    log_path: Path,
    timeout_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> BoundedProcessResultV0:
    """Run one process tree with exact identity tracking and unconditional cleanup."""

    if not command:
        raise ValueError("bounded process command must not be empty")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("bounded process timeout must be finite and positive")
    target_log = Path(log_path).resolve()
    target_log.parent.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen[str] | None = None
    root: ProcessIdentityV0 | None = None
    registered: dict[int, ProcessIdentityV0] = {}
    primary_failure: str | None = None
    cleanup_failures: list[str] = []
    return_code: int | None = None
    with target_log.open("x", encoding="utf-8", newline="\n") as log_stream:
        try:
            process = subprocess.Popen(
                list(command),
                cwd=Path(cwd).resolve(),
                env=dict(environment),
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                text=True,
            )
            root = _process_identity(process.pid)
            registered[root.pid] = root
            deadline = monotonic() + timeout_seconds
            while process.poll() is None:
                try:
                    snapshot = capture_registered_tree(root)
                except ProcessIdentityError:
                    snapshot = None
                if snapshot is not None:
                    merge_registered_identities(root, registered, snapshot.identities)
                if monotonic() >= deadline:
                    primary_failure = (
                        f"process exceeded {timeout_seconds} seconds"
                    )
                    break
                sleep(0.1)
            return_code = process.poll()
        except BaseException as error:
            primary_failure = f"{type(error).__name__}: {error}"
        finally:
            if process is not None and root is not None:
                try:
                    snapshot = capture_registered_tree(root)
                except ProcessIdentityError:
                    snapshot = None
                if snapshot is not None:
                    try:
                        merge_registered_identities(
                            root,
                            registered,
                            snapshot.identities,
                        )
                    except ProcessIdentityError as error:
                        cleanup_failures.append(str(error))
                try:
                    cleanup = terminate_registered_tree(
                        root,
                        tuple(registered.values()),
                        grace_seconds=5.0,
                    )
                except ProcessIdentityError as error:
                    cleanup_failures.append(str(error))
                else:
                    cleanup_failures.extend(cleanup.errors)
                    if cleanup.surviving:
                        cleanup_failures.append(
                            "registered build processes survived cleanup"
                        )
                if process.poll() is None:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        cleanup_failures.append("build root did not exit after exact cleanup")
                return_code = process.poll()
            elif process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=10)
                except BaseException as error:
                    cleanup_failures.append(
                        f"unregistered build root cleanup: {type(error).__name__}: {error}"
                    )
                return_code = process.poll()
    return BoundedProcessResultV0(
        return_code=return_code,
        primary_failure=primary_failure,
        cleanup_failures=tuple(dict.fromkeys(cleanup_failures)),
        process_stopped=process is None or process.poll() is not None,
        registered_processes=tuple(
            sorted(registered.values(), key=lambda item: item.pid)
        ),
    )


def _build_runtime(sandbox: Path, run_dir: Path, timeout_seconds: float) -> None:
    command = build_gradle_command(sandbox)
    environment = prepare_gradle_environment(
        os.environ,
        gradle_user_home=PROJECT_ROOT / ".gradle",
        properties_template=PROJECT_ROOT / "config" / "gradle.properties.mihomo",
    )
    log_path = run_dir / "build-logs" / f"{sandbox.name}.log"
    result = run_bounded_process(
        command,
        cwd=sandbox,
        environment=environment,
        log_path=log_path,
        timeout_seconds=timeout_seconds,
    )
    write_json_atomic(
        log_path.with_suffix(".result.json"),
        trace_projection(result),
    )
    if result.primary_failure is not None:
        raise RuntimeError(
            f"Gradle build failed for {sandbox.name}: {result.primary_failure}"
        )
    if result.cleanup_failures or not result.process_stopped:
        raise RuntimeError(
            f"Gradle build cleanup failed for {sandbox.name}: "
            f"{', '.join(result.cleanup_failures) or 'root still alive'}"
        )
    if result.return_code != 0:
        raise RuntimeError(
            f"Gradle build failed for {sandbox.name} with exit code {result.return_code}"
        )


def _prepare_and_build(
    *,
    source_root: Path,
    sandbox_parent: Path,
    sandbox_id: str,
    mode: CraftGroundClockModeV0,
    run_dir: Path,
    timeout_seconds: float,
) -> PreparedCraftGroundRuntimeV0:
    prepared = prepare_runtime_sandbox(
        source_root=source_root,
        sandbox_parent=sandbox_parent,
        sandbox_id=sandbox_id,
        clock_mode=mode,
        observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
    )
    _build_runtime(prepared.path, run_dir, timeout_seconds)
    validate_sandbox_for_mode(
        prepared.path,
        mode,
        CraftGroundObservationModeV0.STRUCTURED_ONLY,
    )
    return prepared


@dataclass(slots=True)
class _ExecutionResult:
    handles: list[_WorkerHandle]
    samples: list[ResourceSampleV0]
    guard_reasons: tuple[str, ...]
    final_cleanups: dict[str, dict[str, object]]
    injected_cleanup: dict[str, object] | None = None
    injected_victim: str | None = None


def _execution_cleanup_failures(
    execution: _ExecutionResult,
    *,
    port_is_free: Callable[[int], bool] = _port_is_free,
) -> tuple[str, ...]:
    failures: list[str] = []
    for handle in execution.handles:
        worker_id = handle.config.worker_id
        cleanup = execution.final_cleanups.get(worker_id)
        if cleanup is None:
            failures.append(f"{worker_id}: parent cleanup result missing")
        else:
            failures.extend(
                f"{worker_id}: parent cleanup: {item}"
                for item in cleanup.get("errors", [])
            )
            if cleanup.get("surviving"):
                failures.append(
                    f"{worker_id}: registered processes survived parent cleanup"
                )
        if not port_is_free(handle.config.port):
            failures.append(
                f"{worker_id}: port {handle.config.port} not released"
            )
    return tuple(dict.fromkeys(failures))


def _execute_worker_group(
    configs: Sequence[ProbeWorkerConfigV0],
    *,
    resource_path: Path,
    fault_injection: bool = False,
    worker_entry: Callable[..., None] = _worker_process_entry,
    resource_sampler: Callable[[Mapping[str, ProcessIdentityV0]], ResourceSampleV0] = query_host_resources,
) -> _ExecutionResult:
    if not configs:
        raise ValueError("worker group must not be empty")
    context = multiprocessing.get_context("spawn")
    event_queue = context.Queue()
    start_event = context.Event() if configs[0].kind is ProbeWorkerKindV0.CAPACITY else None
    cancel_event = context.Event()
    continue_events = {
        config.worker_id: context.Event() for config in configs
    } if fault_injection else {}
    handles: dict[str, _WorkerHandle] = {}
    samples: list[ResourceSampleV0] = []
    guard_reasons: tuple[str, ...] = ()
    injected_cleanup: dict[str, object] | None = None
    injected_victim: str | None = None
    final_cleanups: dict[str, dict[str, object]] = {}
    deadline = time.monotonic() + max(config.timeout_seconds for config in configs)
    next_sample = 0.0
    try:
        for config in configs:
            process = context.Process(
                target=worker_entry,
                args=(
                    config,
                    event_queue,
                    start_event,
                    cancel_event,
                    continue_events.get(config.worker_id),
                ),
                name=f"mc2p-{config.worker_id}",
            )
            process.start()
            if process.pid is None:
                raise RuntimeError("spawned worker has no pid")
            root = _process_identity(process.pid)
            handles[config.worker_id] = _WorkerHandle(
                config, process, root, {root.pid: root}
            )

        barrier_released = start_event is None
        fault_done = False
        while True:
            _drain_events(event_queue, handles)
            for handle in handles.values():
                _refresh_registered(handle)

            if start_event is not None and not barrier_released:
                ready = all(
                    any(event.event_type == "warmup_complete" for event in handle.events)
                    for handle in handles.values()
                )
                if ready:
                    start_event.set()
                    barrier_released = True
                elif any(
                    not handle.process.is_alive() for handle in handles.values()
                ):
                    guard_reasons = tuple(
                        dict.fromkeys((*guard_reasons, "worker-crash-before-barrier"))
                    )
                    cancel_event.set()
                    start_event.set()
                    barrier_released = True

            if fault_injection and barrier_released and not fault_done:
                paused = [
                    handle
                    for handle in handles.values()
                    if any(event.event_type == "fault_pause" for event in handle.events)
                ]
                if len(paused) == len(handles):
                    victim = sorted(paused, key=lambda item: item.config.worker_id)[0]
                    _refresh_registered(victim)
                    injected_victim = victim.config.worker_id
                    progress = next(
                        event.sequence
                        for event in victim.events
                        if event.event_type == "fault_pause"
                    )
                    cleanup = terminate_fault_victim(
                        victim.root,
                        tuple(victim.registered.values()),
                        reported_progress=-1 if progress is None else progress,
                    )
                    injected_cleanup = trace_projection(cleanup)
                    survivor = next(
                        item for item in handles.values() if item is not victim
                    )
                    continue_events[survivor.config.worker_id].set()
                    fault_done = True
                elif any(
                    not handle.process.is_alive() for handle in handles.values()
                ):
                    guard_reasons = tuple(
                        dict.fromkeys((*guard_reasons, "worker-crash-before-fault"))
                    )
                    cancel_event.set()
                    _release_continue_events(
                        continue_events,
                        excluded_workers=tuple(
                            name
                            for name, handle in handles.items()
                            if not handle.process.is_alive()
                        ),
                    )

            now = time.monotonic()
            if now >= next_sample:
                roots = {
                    name: handle.root
                    for name, handle in handles.items()
                    if handle.process.is_alive()
                }
                sample = resource_sampler(roots)
                samples.append(sample)
                append_jsonl(resource_path, trace_projection(sample))
                decision = evaluate_resource_guard(sample)
                reasons = tuple(
                    reason
                    for reason in decision.reasons
                    if not (
                        reason == "gpu-monitor-unavailable"
                        and len(configs) == 1
                    )
                )
                if reasons:
                    guard_reasons = reasons
                    cancel_event.set()
                    _release_continue_events(
                        continue_events,
                        excluded_workers=(injected_victim,)
                        if injected_victim is not None
                        else (),
                    )
                next_sample = now + RESOURCE_SAMPLE_PERIOD_SECONDS

            alive = [handle for handle in handles.values() if handle.process.is_alive()]
            if not alive:
                break
            if now >= deadline:
                guard_reasons = tuple(dict.fromkeys((*guard_reasons, "hang")))
                cancel_event.set()
                _release_continue_events(
                    continue_events,
                    excluded_workers=(injected_victim,)
                    if injected_victim is not None
                    else (),
                )
                break
            time.sleep(0.05)

        for handle in handles.values():
            handle.process.join(timeout=10)
        _drain_events(event_queue, handles)
    finally:
        cancel_event.set()
        _release_continue_events(
            continue_events,
            excluded_workers=(injected_victim,)
            if injected_victim is not None
            else (),
        )
        for handle in handles.values():
            refresh_error: str | None = None
            try:
                _refresh_registered(handle)
            except ProcessIdentityError as error:
                refresh_error = str(error)
            final_cleanup = _cleanup_handle(handle)
            if refresh_error is not None:
                final_cleanup.setdefault("errors", []).append(refresh_error)
            final_cleanups[handle.config.worker_id] = final_cleanup
            if handle.process.is_alive():
                handle.process.join(timeout=10)
        try:
            _drain_events(event_queue, handles)
        finally:
            event_queue.close()
            event_queue.join_thread()
    return _ExecutionResult(
        list(handles.values()),
        samples,
        guard_reasons,
        final_cleanups,
        injected_cleanup,
        injected_victim,
    )


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def validate_worker_trace(
    path: Path,
    *,
    expected_active_steps: int,
) -> tuple[str, ...]:
    """Independently audit Runtime sequence continuity and the cancel boundary."""

    if type(expected_active_steps) is not int or expected_active_steps < 0:
        raise ValueError("expected active steps must be a nonnegative integer")
    failures: list[str] = []
    records: list[dict[str, Any]] = []
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                value = json.loads(line)
                if not isinstance(value, dict):
                    failures.append(f"trace line {line_number} is not an object")
                    continue
                records.append(value)
    except (OSError, json.JSONDecodeError) as error:
        return (f"trace cannot be read: {type(error).__name__}: {error}",)
    expected_count = expected_active_steps + 2
    if len(records) != expected_count:
        failures.append(
            f"trace record count is {len(records)}, expected {expected_count}"
        )
    if not records or records[0].get("record_type") != "reset":
        failures.append("trace does not begin with exactly one reset")
    step_records = [record for record in records if record.get("record_type") == "step"]
    if len(step_records) != expected_active_steps + 1:
        failures.append(
            f"trace step count is {len(step_records)}, expected {expected_active_steps + 1}"
        )
    if sum(record.get("record_type") == "reset" for record in records) != 1:
        failures.append("trace reset count is not one")
    for sequence, record in enumerate(step_records):
        try:
            payload = record["payload"]
            action_sequence = payload["decision"]["action"]["action_sequence_id"]
            backend_result = payload["backend_result"]
            observation = backend_result["observation"]
            observation_sequence = observation["sequence_id"]
            request_sequence = observation["request_sequence_id"]
            terminated = backend_result["terminated"]
            truncated = backend_result["truncated"]
        except (KeyError, TypeError):
            failures.append(f"step {sequence} is missing sequence evidence")
            continue
        if action_sequence != sequence:
            failures.append(f"step {sequence} action sequence is not continuous")
        if observation_sequence != sequence + 1:
            failures.append(f"step {sequence} observation sequence is not continuous")
        if request_sequence != sequence:
            failures.append(f"step {sequence} request sequence is not continuous")
        if terminated:
            failures.append(f"step {sequence} backend terminated")
        if truncated:
            failures.append(f"step {sequence} backend truncated")
    if step_records:
        try:
            final_payload = step_records[-1]["payload"]
            final_action = final_payload["decision"]["action"]
            final_status = final_payload["report"]["status"]
            neutral = (
                not any(final_action["locomotion"].values())
                and final_action["camera"] == {"pitch_delta": 0.0, "yaw_delta": 0.0}
                and not any(final_action["interaction"].values())
                and final_action["hotbar"] == {"selected_slot": None}
                and not any(final_action["gui"].values())
            )
        except (KeyError, TypeError, AttributeError):
            neutral = False
            final_status = None
        if final_status != "cancelled":
            failures.append("final report is not structured cancelled")
        if not neutral:
            failures.append("final action is not completely neutral")
    return tuple(dict.fromkeys(failures))


def _parse_timing_run(path: Path) -> TimingRunResultV0:
    report = _load_json(path)
    raw = report.get("timing_run")
    if not isinstance(raw, dict):
        raise ValueError(f"worker result has no timing run: {path}")
    samples = tuple(
        TimingSampleV0(
            **{key: value for key, value in item.items() if key != "schema_version"}
        )
        for item in raw["samples"]
    )
    return TimingRunResultV0(
        worker_id=raw["worker_id"],
        clock_mode=CraftGroundClockModeV0(raw["clock_mode"]),
        seed=int(raw["seed"]),
        attempt=int(raw["attempt"]),
        wall_clock_paced=bool(raw["wall_clock_paced"]),
        sandbox_path=str(raw["sandbox_path"]),
        requested_port=int(raw["requested_port"]),
        actual_port=int(raw["actual_port"]),
        samples=samples,
        primary_failure=raw["primary_failure"],
        cleanup_failures=tuple(raw["cleanup_failures"]),
        process_stopped=bool(raw["process_stopped"]),
        port_released=bool(raw["port_released"]),
        reset_world_time_ticks=(
            None
            if raw.get("reset_world_time_ticks") is None
            else int(raw["reset_world_time_ticks"])
        ),
    )


def _timing_run_is_clean(run: TimingRunResultV0) -> bool:
    return (
        run.primary_failure is None
        and not run.cleanup_failures
        and run.process_stopped
        and run.port_released
        and run.actual_port == run.requested_port
    )


def _failed_timing_run(
    config: ProbeWorkerConfigV0,
    *,
    primary_failure: str,
    cleanup_failures: Sequence[str] = (),
    process_stopped: bool = False,
    port_released: bool,
) -> TimingRunResultV0:
    return TimingRunResultV0(
        worker_id=config.worker_id,
        clock_mode=config.clock_mode,
        seed=config.seed,
        attempt=config.attempt,
        wall_clock_paced=config.clock_mode
        is CraftGroundClockModeV0.REFERENCE_20_TPS,
        sandbox_path=str(config.sandbox_path),
        requested_port=config.port,
        actual_port=config.port,
        samples=(),
        primary_failure=primary_failure,
        cleanup_failures=tuple(cleanup_failures),
        process_stopped=process_stopped,
        port_released=port_released,
    )


def _persist_timing_attempt(
    config: ProbeWorkerConfigV0,
    run: TimingRunResultV0,
    *,
    guard_reasons: Sequence[str],
) -> None:
    write_json_atomic(
        config.run_dir / "supervisor-attempt.json",
        {
            "schema_version": "mc2p.timing-supervisor-attempt.v0",
            "worker_id": config.worker_id,
            "seed": config.seed,
            "attempt": config.attempt,
            "clock_mode": config.clock_mode.value,
            "guard_reasons": list(guard_reasons),
            "run": trace_projection(run),
        },
    )


def _run_one_timing_worker(
    *,
    run_dir: Path,
    sandbox: Path,
    mode: CraftGroundClockModeV0,
    seed: int,
    attempt: int,
    port: int,
    timeout_seconds: float,
    resource_path: Path,
) -> TimingRunResultV0:
    worker_id = f"timing-{seed}-{mode.value}-a{attempt}"
    lockstep_trace_source = Path(sandbox) / "run" / "mc2p-lockstep.jsonl"
    lockstep_trace_offset = (
        lockstep_trace_source.stat().st_size
        if mode is CraftGroundClockModeV0.LOCKSTEP_ACCELERATED
        and lockstep_trace_source.is_file()
        else 0
    )
    config = ProbeWorkerConfigV0(
        worker_id=worker_id,
        kind=ProbeWorkerKindV0.TIMING,
        run_dir=timing_worker_run_directory(
            run_dir,
            mode,
            seed=seed,
            attempt=attempt,
        ),
        sandbox_path=sandbox.resolve(),
        clock_mode=mode,
        seed=seed,
        port=port,
        attempt=attempt,
        timeout_seconds=timeout_seconds,
    )
    try:
        execution = _execute_worker_group((config,), resource_path=resource_path)
    except Exception as error:
        port_released = _port_is_free(port)
        run = _failed_timing_run(
            config,
            primary_failure=f"supervisor execution failed: {type(error).__name__}: {error}",
            cleanup_failures=("supervisor execution outcome unavailable",),
            process_stopped=False,
            port_released=port_released,
        )
        _persist_timing_attempt(config, run, guard_reasons=())
        return run

    result_path = config.run_dir / "result.json"
    trace_path = config.run_dir / "trace.jsonl"
    load_failure: str | None = None
    try:
        run = _parse_timing_run(result_path)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        load_failure = f"worker result unavailable: {type(error).__name__}: {error}"
        run = _failed_timing_run(
            config,
            primary_failure=load_failure,
            port_released=_port_is_free(port),
        )
    trace_failures = validate_worker_trace(
        trace_path,
        expected_active_steps=100,
    )
    parent_cleanup = execution.final_cleanups.get(worker_id, {})
    parent_failures = tuple(str(item) for item in parent_cleanup.get("errors", []))
    surviving = parent_cleanup.get("surviving", [])
    if surviving:
        parent_failures += ("parent supervisor found surviving registered processes",)
    port_released = _port_is_free(port)
    guard_failure = (
        "timing worker resource guard: " + ", ".join(execution.guard_reasons)
        if execution.guard_reasons
        else None
    )
    if trace_failures or parent_failures or not port_released or guard_failure:
        primary_parts = [run.primary_failure or load_failure]
        primary_parts.append(guard_failure)
        if trace_failures:
            primary_parts.append("parent trace audit: " + "; ".join(trace_failures))
        if not port_released:
            primary_parts.append(f"parent port audit: port {port} not released")
        primary = "; ".join(
            dict.fromkeys(item for item in primary_parts if item is not None)
        )
        run = replace(
            run,
            primary_failure=primary,
            cleanup_failures=tuple((*run.cleanup_failures, *parent_failures)),
            process_stopped=run.process_stopped and not bool(surviving),
            port_released=port_released,
        )
    if mode is CraftGroundClockModeV0.LOCKSTEP_ACCELERATED:
        try:
            _capture_lockstep_trace_segment(
                sandbox,
                lockstep_trace_offset,
                config.run_dir / "lockstep-trace.jsonl",
            )
        except (OSError, UnicodeError, ValueError) as error:
            trace_capture_failure = (
                f"lockstep trace capture failed: {type(error).__name__}: {error}"
            )
            run = replace(
                run,
                primary_failure="; ".join(
                    item
                    for item in (run.primary_failure, trace_capture_failure)
                    if item is not None
                ),
            )
    _persist_timing_attempt(
        config,
        run,
        guard_reasons=execution.guard_reasons,
    )
    return run


def _run_timing_stage(
    *,
    config: ProbeConfigV0,
    run_dir: Path,
    reference: Path,
    accelerated: Path,
    resource_path: Path,
) -> tuple[TimingVerdictV0, list[TimingPairReportV0]]:
    pairs: list[TimingPairReportV0] = []
    paths = {
        CraftGroundClockModeV0.REFERENCE_20_TPS: reference,
        CraftGroundClockModeV0.ACCELERATED: accelerated,
    }
    ports = {
        CraftGroundClockModeV0.REFERENCE_20_TPS: config.base_port,
        CraftGroundClockModeV0.ACCELERATED: config.base_port + 1,
    }
    for seed_index, seed in enumerate(TIMING_SEEDS):
        order = (
            (CraftGroundClockModeV0.REFERENCE_20_TPS, CraftGroundClockModeV0.ACCELERATED)
            if seed_index != 1
            else (CraftGroundClockModeV0.ACCELERATED, CraftGroundClockModeV0.REFERENCE_20_TPS)
        )
        runs: dict[CraftGroundClockModeV0, TimingRunResultV0] = {}
        for mode in order:
            runs[mode] = _run_one_timing_worker(
                run_dir=run_dir,
                sandbox=paths[mode],
                mode=mode,
                seed=seed,
                attempt=1,
                port=ports[mode],
                timeout_seconds=config.worker_timeout_seconds,
                resource_path=resource_path,
            )
        first = compare_timing_pair(
            runs[CraftGroundClockModeV0.REFERENCE_20_TPS],
            runs[CraftGroundClockModeV0.ACCELERATED],
        )
        pairs.append(first)
        disposition = timing_attempt_disposition(
            attempt=1, clean=first.clean, equivalent=first.equivalent
        )
        if disposition is TimingAttemptDispositionV0.RETRY:
            retry_modes = order if first.clean else tuple(
                mode for mode in order if not _timing_run_is_clean(runs[mode])
            )
            for mode in retry_modes:
                runs[mode] = _run_one_timing_worker(
                    run_dir=run_dir,
                    sandbox=paths[mode],
                    mode=mode,
                    seed=seed,
                    attempt=2,
                    port=ports[mode],
                    timeout_seconds=config.worker_timeout_seconds,
                    resource_path=resource_path,
                )
            pairs.append(
                compare_timing_pair(
                    runs[CraftGroundClockModeV0.REFERENCE_20_TPS],
                    runs[CraftGroundClockModeV0.ACCELERATED],
                )
            )
    verdict = aggregate_timing_verdict(pairs)
    report = {
        "schema_version": "mc2p.timing-report.v0",
        "seeds": list(TIMING_SEEDS),
        "mode_order": ["R/A", "A/R", "R/A"],
        "verdict": verdict.value,
        "pairs": trace_projection(pairs),
    }
    write_json_atomic(run_dir / "timing" / "report.json", report)
    return verdict, pairs


def _run_lockstep_stage(
    *,
    config: ProbeConfigV0,
    run_dir: Path,
    sandbox: Path,
    resource_path: Path,
) -> bool:
    run_reports: list[dict[str, object]] = []
    stage_passed = True
    for seed in config.seeds:
        run = _run_one_timing_worker(
            run_dir=run_dir,
            sandbox=sandbox,
            mode=CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
            seed=seed,
            attempt=1,
            port=config.base_port,
            timeout_seconds=config.worker_timeout_seconds,
            resource_path=resource_path,
        )
        worker_dir = timing_worker_run_directory(
            run_dir,
            CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
            seed=seed,
            attempt=1,
        )
        trace_path = worker_dir / "lockstep-trace.jsonl"
        events = ()
        try:
            events = parse_lockstep_trace_lines(
                trace_path.read_text(encoding="utf-8").splitlines()
            )
            if run.reset_world_time_ticks is None:
                raise ValueError("timing run has no reset world time")
            checks = evaluate_lockstep_run(
                run,
                events,
                reset_world_time_ticks=run.reset_world_time_ticks,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            checks = (
                CheckResultV0(
                    name="lockstep.trace-parse",
                    passed=False,
                    actual=f"{type(error).__name__}: {error}",
                    expected="valid per-worker lockstep trace and reset world time",
                ),
            )
        passed = _timing_run_is_clean(run) and all(check.passed for check in checks)
        stage_passed = stage_passed and passed
        run_reports.append(
            {
                "seed": seed,
                "status": "passed" if passed else "failed",
                "trace_path": str(trace_path.resolve()),
                "trace_event_count": len(events),
                "run": trace_projection(run),
                "checks": trace_projection(checks),
            }
        )
    write_json_atomic(
        run_dir / "timing" / "lockstep-report.json",
        {
            "schema_version": "mc2p.lockstep-report.v0",
            "status": "passed" if stage_passed else "failed",
            "seeds": list(config.seeds),
            "runs": run_reports,
        },
    )
    return stage_passed


def _capacity_result_from_execution(
    execution: _ExecutionResult,
    *,
    parallelism: int,
    batch_index: int,
) -> CapacityBatchResultV0:
    worker_reports: list[dict[str, Any]] = []
    semantic: list[str] = []
    cleanup: list[str] = []
    measured_started: list[int] = []
    finished: list[int] = []
    for handle in execution.handles:
        result_path = handle.config.run_dir / "result.json"
        if not result_path.is_file():
            semantic.append(f"{handle.config.worker_id}: result missing")
            continue
        report = _load_json(result_path)
        worker_reports.append(report)
        if report.get("status") != "passed" or handle.process.exitcode != 0:
            semantic.append(f"{handle.config.worker_id}: worker failed")
        semantic.extend(
            f"{handle.config.worker_id}: {item}"
            for item in validate_worker_trace(
                handle.config.run_dir / "trace.jsonl",
                expected_active_steps=16 + CAPACITY_STEP_COUNT,
            )
        )
        cleanup.extend(
            f"{handle.config.worker_id}: {item}"
            for item in report.get("cleanup_failures", [])
        )
        parent_cleanup = execution.final_cleanups.get(handle.config.worker_id, {})
        cleanup.extend(
            f"{handle.config.worker_id}: parent cleanup: {item}"
            for item in parent_cleanup.get("errors", [])
        )
        if parent_cleanup.get("surviving"):
            cleanup.append(
                f"{handle.config.worker_id}: registered processes survived parent cleanup"
            )
        if not _port_is_free(handle.config.port):
            cleanup.append(f"{handle.config.worker_id}: port not released")
        measured_started.extend(
            event.monotonic_ns
            for event in handle.events
            if event.event_type == "measured_started"
        )
        finished.extend(
            event.monotonic_ns
            for event in handle.events
            if event.event_type == "finished"
        )
    elapsed = (
        (max(finished) - min(measured_started)) / 1_000_000_000
        if len(measured_started) == parallelism and len(finished) == parallelism
        else 0.0
    )
    reset_seconds: list[float] = []
    measured_seconds: list[float] = []
    worker_throughput: list[float] = []
    for report in worker_reports:
        try:
            reset_value = float(report["reset_seconds"])
            measured_value = float(report["measured_elapsed_seconds"])
        except (KeyError, TypeError, ValueError):
            semantic.append(
                f"{report.get('worker_id', 'worker')}: timing metrics missing"
            )
            continue
        if (
            not math.isfinite(reset_value)
            or reset_value < 0
            or not math.isfinite(measured_value)
            or measured_value <= 0
        ):
            semantic.append(
                f"{report.get('worker_id', 'worker')}: timing metrics invalid"
            )
            continue
        reset_seconds.append(reset_value)
        measured_seconds.append(measured_value)
        worker_throughput.append(
            int(report.get("measured_steps", 0)) / measured_value
        )
    resource_summary: dict[str, object] = {
        "sample_count": len(execution.samples),
    }
    if execution.samples:
        tail = execution.samples[-1]
        resource_summary.update(
            {
                "ram_peak_percent": max(
                    sample.ram_used_percent for sample in execution.samples
                ),
                "ram_tail_percent": tail.ram_used_percent,
                "cpu_peak_percent": max(
                    sample.cpu_used_percent for sample in execution.samples
                ),
                "cpu_tail_percent": tail.cpu_used_percent,
                "gpu_monitor_available_all": all(
                    sample.gpu_monitor_available for sample in execution.samples
                ),
                "vram_used_peak_mib": max(
                    (
                        sample.vram_used_mib
                        for sample in execution.samples
                        if sample.vram_used_mib is not None
                    ),
                    default=None,
                ),
                "vram_used_tail_mib": tail.vram_used_mib,
                "gpu_peak_percent": max(
                    (
                        sample.gpu_used_percent
                        for sample in execution.samples
                        if sample.gpu_used_percent is not None
                    ),
                    default=None,
                ),
                "gpu_tail_percent": tail.gpu_used_percent,
                "worker_tree_rss_peak_bytes": max(
                    (
                        sum(value for _name, value in sample.worker_tree_rss_bytes)
                        for sample in execution.samples
                    ),
                    default=0,
                ),
            }
        )
    else:
        semantic.append("capacity batch has no resource samples")
    return CapacityBatchResultV0(
        parallelism=parallelism,
        batch_index=batch_index,
        worker_measured_steps=tuple(
            int(report.get("measured_steps", -1)) for report in worker_reports
        ),
        batch_elapsed_seconds=elapsed,
        step_latencies_seconds=tuple(
            float(value)
            for report in worker_reports
            for value in report.get("step_latencies_seconds", [])
        ),
        worker_reset_seconds=tuple(reset_seconds),
        worker_measured_elapsed_seconds=tuple(measured_seconds),
        worker_steps_per_second=tuple(worker_throughput),
        resource_summary=resource_summary,
        semantic_failures=tuple(semantic),
        cleanup_failures=tuple(cleanup),
        guard_reasons=execution.guard_reasons,
    )


def _run_capacity_batches(
    *,
    config: ProbeConfigV0,
    run_dir: Path,
    sandboxes: Sequence[Path],
    parallelism: int,
    resource_path: Path,
) -> tuple[CapacityStageOutcomeV0, list[CapacityBatchResultV0]]:
    batches: list[CapacityBatchResultV0] = []
    scale_block_reasons: list[str] = []
    for batch_index in range(CAPACITY_BATCH_COUNT):
        worker_configs = tuple(
            ProbeWorkerConfigV0(
                worker_id=f"capacity-n{parallelism}-b{batch_index}-w{worker}",
                kind=ProbeWorkerKindV0.CAPACITY,
                run_dir=(run_dir / "capacity" / f"n{parallelism}" / f"batch-{batch_index}" / f"worker-{worker}").resolve(),
                sandbox_path=sandboxes[worker].resolve(),
                clock_mode=CraftGroundClockModeV0.ACCELERATED,
                seed=22000 + parallelism * 100 + batch_index * 10 + worker,
                port=config.base_port + 2 + worker,
                timeout_seconds=config.worker_timeout_seconds,
            )
            for worker in range(parallelism)
        )
        execution = _execute_worker_group(worker_configs, resource_path=resource_path)
        if any(not sample.gpu_monitor_available for sample in execution.samples):
            scale_block_reasons.append("gpu-monitor-unavailable")
        result = _capacity_result_from_execution(
            execution, parallelism=parallelism, batch_index=batch_index
        )
        batches.append(result)
        write_json_atomic(
            run_dir / "capacity" / f"n{parallelism}" / f"batch-{batch_index}" / "report.json",
            trace_projection(result),
        )
        if not result.passed:
            reasons = result.guard_reasons or result.cleanup_failures or result.semantic_failures
            return CapacityStageOutcomeV0(
                False,
                tuple(reasons),
                tuple(dict.fromkeys(scale_block_reasons)),
            ), batches
    return CapacityStageOutcomeV0(
        True,
        scale_block_reasons=tuple(dict.fromkeys(scale_block_reasons)),
    ), batches


def _count_trace_records(path: Path, record_type: str) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            if value.get("record_type") == record_type:
                count += 1
    return count


def _run_fault_isolation(
    *,
    config: ProbeConfigV0,
    run_dir: Path,
    sandboxes: Sequence[Path],
    resource_path: Path,
) -> tuple[CapacityStageOutcomeV0, dict[str, object]]:
    worker_configs = tuple(
        ProbeWorkerConfigV0(
            worker_id=f"fault-{'a' if worker == 0 else 'b'}",
            kind=ProbeWorkerKindV0.CAPACITY,
            run_dir=(run_dir / "fault-isolation" / f"worker-{worker}").resolve(),
            sandbox_path=sandboxes[worker].resolve(),
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            seed=23001 + worker,
            port=config.base_port + 6 + worker,
            timeout_seconds=config.worker_timeout_seconds,
            fault_pause_at_step=64,
        )
        for worker in range(2)
    )
    execution = _execute_worker_group(
        worker_configs,
        resource_path=resource_path,
        fault_injection=True,
    )
    victim = execution.injected_victim
    survivor_handles = [
        handle for handle in execution.handles if handle.config.worker_id != victim
    ]
    failures: list[str] = list(execution.guard_reasons)
    failures.extend(_execution_cleanup_failures(execution))
    injection_ok = victim is not None and execution.injected_cleanup is not None
    if victim is None or execution.injected_cleanup is None:
        failures.append("fault was not injected")
    else:
        if execution.injected_cleanup.get("surviving"):
            failures.append("victim process tree survived")
            injection_ok = False
        if execution.injected_cleanup.get("errors"):
            failures.append("victim cleanup reported errors")
            injection_ok = False
        final_cleanup = execution.final_cleanups.get(victim)
        if final_cleanup is None or final_cleanup.get("errors") or final_cleanup.get("surviving"):
            injection_ok = False
        victim_handle = next(
            (item for item in execution.handles if item.config.worker_id == victim),
            None,
        )
        if victim_handle is None or not _port_is_free(victim_handle.config.port):
            injection_ok = False
    survivor_report: dict[str, Any] | None = None
    progress_before = 64
    progress_after = 0
    reset_count = 0
    if len(survivor_handles) != 1:
        failures.append("fault survivor selection failed")
    else:
        survivor = survivor_handles[0]
        result_path = survivor.config.run_dir / "result.json"
        if not result_path.is_file():
            failures.append("survivor result missing")
        else:
            survivor_report = _load_json(result_path)
            progress_after = int(survivor_report.get("measured_steps", 0))
            reset_count = _count_trace_records(survivor.config.run_dir / "trace.jsonl", "reset")
            failures.extend(
                f"survivor: {item}"
                for item in validate_worker_trace(
                    survivor.config.run_dir / "trace.jsonl",
                    expected_active_steps=16 + CAPACITY_STEP_COUNT,
                )
            )
            if survivor_report.get("status") != "passed" or progress_after != CAPACITY_STEP_COUNT:
                failures.append("survivor did not complete 256 measured steps")
            if progress_after - progress_before < 64:
                failures.append("survivor made fewer than 64 additional steps")
            if reset_count != 1:
                failures.append("survivor reset or reconnected during injection")
            if not _port_is_free(survivor.config.port):
                failures.append("survivor port not released")
    victim_config = next(
        (item for item in worker_configs if item.worker_id == victim), None
    )
    if victim_config is not None and not _port_is_free(victim_config.port):
        failures.append("victim port not released")
    report: dict[str, object] = {
        "schema_version": "mc2p.fault-isolation-report.v0",
        "passed": not failures,
        "victim": victim,
        "victim_status": (
            "expected_injected_failure"
            if injection_ok
            else "injection_failed"
        ),
        "victim_cleanup": execution.injected_cleanup,
        "survivor": None if not survivor_handles else survivor_handles[0].config.worker_id,
        "survivor_progress_before": progress_before,
        "survivor_progress_after": progress_after,
        "survivor_reset_count": reset_count,
        "survivor_result": survivor_report,
        "failures": failures,
    }
    write_json_atomic(run_dir / "fault-isolation" / "report.json", report)
    return CapacityStageOutcomeV0(not failures, tuple(failures)), report


def _manifest(
    *, config: ProbeConfigV0, run_dir: Path, source_root: Path, initial_sample: ResourceSampleV0
) -> dict[str, object]:
    packages: dict[str, str | None] = {}
    for name in ("craftground", "craftground-runtime-mc121", "torch", "psutil"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "schema_version": "mc2p.timing-parallel-manifest.v0",
        "run_id": run_dir.name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "artifact_root": str(config.artifact_root),
        "python": sys.version,
        "platform": platform.platform(),
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "ram_total_bytes": psutil.virtual_memory().total,
        "packages": packages,
        "source_root": str(source_root),
        "source_fingerprints": capture_runtime_source_fingerprints(source_root),
        "expected_source_fingerprints": dict(MC121_RUNTIME_0_1_0_FINGERPRINTS),
        "base_port": config.base_port,
        "candidate_ports": list(config.candidate_ports),
        "worker_timeout_seconds": config.worker_timeout_seconds,
        "build_timeout_seconds": config.build_timeout_seconds,
        "capacity_batches": CAPACITY_BATCH_COUNT,
        "timing_seeds": list(TIMING_SEEDS),
        "selected_seeds": list(config.seeds),
        "lockstep_probe": config.lockstep_probe,
        "initial_resource_sample": trace_projection(initial_sample),
    }


def _run_reference_smoke(
    *, config: ProbeConfigV0, run_dir: Path, source_root: Path, resource_path: Path
) -> int:
    sandbox = _prepare_and_build(
        source_root=source_root,
        sandbox_parent=run_dir / "sandboxes",
        sandbox_id="timing-reference",
        mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
        run_dir=run_dir,
        timeout_seconds=config.build_timeout_seconds,
    )
    result = _run_one_timing_worker(
        run_dir=run_dir,
        sandbox=sandbox.path,
        mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
        seed=TIMING_SEEDS[0],
        attempt=1,
        port=config.base_port,
        timeout_seconds=config.worker_timeout_seconds,
        resource_path=resource_path,
    )
    checks = validate_timing_run(result)
    passed = _timing_run_is_clean(result) and all(item.passed for item in checks)
    write_json_atomic(
        run_dir / "timing" / "reference-smoke.json",
        {
            "schema_version": "mc2p.reference-smoke.v0",
            "status": "passed" if passed else "failed",
            "run": trace_projection(result),
            "checks": trace_projection(checks),
        },
    )
    return 0 if passed else 2


def run_probe(config: ProbeConfigV0) -> int:
    run_dir = allocate_run_directory(config.artifact_root)
    resource_path = run_dir / "resource-samples.jsonl"
    supervisor: dict[str, object] = {
        "schema_version": "mc2p.timing-parallel-supervisor.v0",
        "status": "running",
        "primary_failure": None,
        "cleanup_failures": [],
    }
    exit_code = 2
    marker = "CRAFTGROUND_TIMING_PARALLEL_FAILED"
    source_root: Path | None = None
    launch_preflight_passed = False
    try:
        drive_root = Path(config.artifact_root.anchor or str(config.artifact_root))
        free_bytes = shutil.disk_usage(drive_root).free
        preflight = evaluate_preflight(
            free_bytes=free_bytes,
            candidate_ports=config.candidate_ports,
            port_is_free=_port_is_free,
        )
        failures = list(preflight.failures)
        if not _proxy_is_listening():
            failures.append("mihomo-127.0.0.1-7897-unavailable")
        source_root = resolve_mc121_runtime_path()
        source_fingerprints = capture_runtime_source_fingerprints(source_root)
        if source_fingerprints != dict(MC121_RUNTIME_0_1_0_FINGERPRINTS):
            failures.append("runtime-source-fingerprint-mismatch")
        initial_sample = query_host_resources({})
        write_json_atomic(
            run_dir / "preflight.json",
            {
                **trace_projection(preflight),
                "failures": failures,
                "proxy_listening": _proxy_is_listening(),
                "initial_resource_sample": trace_projection(initial_sample),
            },
        )
        if failures:
            raise RuntimeError("preflight failed: " + ", ".join(failures))
        launch_preflight_passed = True
        write_json_atomic(
            run_dir / "manifest.json",
            _manifest(
                config=config,
                run_dir=run_dir,
                source_root=source_root,
                initial_sample=initial_sample,
            ),
        )
        if config.reference_smoke:
            exit_code = _run_reference_smoke(
                config=config,
                run_dir=run_dir,
                source_root=source_root,
                resource_path=resource_path,
            )
            supervisor["status"] = "passed" if exit_code == 0 else "failed"
            marker = "CRAFTGROUND_REFERENCE_SMOKE_OK" if exit_code == 0 else "CRAFTGROUND_REFERENCE_SMOKE_FAILED"
            if exit_code != 0:
                supervisor["primary_failure"] = "reference-smoke-failed"
            raise _StopProbe

        if config.lockstep_probe:
            lockstep = _prepare_and_build(
                source_root=source_root,
                sandbox_parent=run_dir / "sandboxes",
                sandbox_id="timing-lockstep",
                mode=CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
                run_dir=run_dir,
                timeout_seconds=config.build_timeout_seconds,
            )
            lockstep_passed = _run_lockstep_stage(
                config=config,
                run_dir=run_dir,
                sandbox=lockstep.path,
                resource_path=resource_path,
            )
            exit_code = 0 if lockstep_passed else 2
            supervisor["status"] = "passed" if lockstep_passed else "failed"
            marker = (
                "CRAFTGROUND_LOCKSTEP_OK"
                if lockstep_passed
                else "CRAFTGROUND_LOCKSTEP_FAILED"
            )
            if not lockstep_passed:
                supervisor["primary_failure"] = "lockstep-probe-failed"
            raise _StopProbe

        reference = _prepare_and_build(
            source_root=source_root,
            sandbox_parent=run_dir / "sandboxes",
            sandbox_id="timing-reference",
            mode=CraftGroundClockModeV0.REFERENCE_20_TPS,
            run_dir=run_dir,
            timeout_seconds=config.build_timeout_seconds,
        )
        accelerated = _prepare_and_build(
            source_root=source_root,
            sandbox_parent=run_dir / "sandboxes",
            sandbox_id="timing-accelerated",
            mode=CraftGroundClockModeV0.ACCELERATED,
            run_dir=run_dir,
            timeout_seconds=config.build_timeout_seconds,
        )
        verdict, _pairs = _run_timing_stage(
            config=config,
            run_dir=run_dir,
            reference=reference.path,
            accelerated=accelerated.path,
            resource_path=resource_path,
        )
        supervisor["timing_verdict"] = verdict.value
        if verdict is not TimingVerdictV0.EQUIVALENT:
            supervisor["status"] = verdict.value
            exit_code = 3 if verdict is TimingVerdictV0.NON_EQUIVALENT else 4
            marker = (
                "CRAFTGROUND_TIMING_NON_EQUIVALENT"
                if verdict is TimingVerdictV0.NON_EQUIVALENT
                else "CRAFTGROUND_TIMING_INCONCLUSIVE"
            )
            raise _StopProbe

        capacity_sandboxes = [
            _prepare_and_build(
                source_root=source_root,
                sandbox_parent=run_dir / "sandboxes",
                sandbox_id=f"capacity-{index}",
                mode=CraftGroundClockModeV0.ACCELERATED,
                run_dir=run_dir,
                timeout_seconds=config.build_timeout_seconds,
            ).path
            for index in range(4)
        ]
        fault_sandboxes = [
            _prepare_and_build(
                source_root=source_root,
                sandbox_parent=run_dir / "sandboxes",
                sandbox_id=f"fault-{'a' if index == 0 else 'b'}",
                mode=CraftGroundClockModeV0.ACCELERATED,
                run_dir=run_dir,
                timeout_seconds=config.build_timeout_seconds,
            ).path
            for index in range(2)
        ]
        all_batches: list[CapacityBatchResultV0] = []
        fault_report: dict[str, object] | None = None

        def run_capacity(parallelism: int) -> CapacityStageOutcomeV0:
            outcome, batches = _run_capacity_batches(
                config=config,
                run_dir=run_dir,
                sandboxes=capacity_sandboxes,
                parallelism=parallelism,
                resource_path=resource_path,
            )
            all_batches.extend(batches)
            return outcome

        def run_fault() -> CapacityStageOutcomeV0:
            nonlocal fault_report
            outcome, fault_report = _run_fault_isolation(
                config=config,
                run_dir=run_dir,
                sandboxes=fault_sandboxes,
                resource_path=resource_path,
            )
            return outcome

        stage_gate = run_capacity_stage_gate(
            verdict,
            gpu_monitor_available=initial_sample.gpu_monitor_available,
            run_capacity=run_capacity,
            run_fault=run_fault,
        )
        fault_passed = bool(fault_report and fault_report.get("passed"))
        summary = summarize_capacity(
            all_batches,
            fault_isolation_passed=fault_passed,
        )
        write_json_atomic(
            run_dir / "capacity" / "report.json",
            {
                "schema_version": "mc2p.capacity-report.v0",
                "stage_gate": trace_projection(stage_gate),
                "batches": trace_projection(all_batches),
                "fault_isolation": fault_report,
                "summary": trace_projection(summary),
            },
        )
        supervisor["capacity_stage_gate"] = trace_projection(stage_gate)
        supervisor["capacity_summary"] = trace_projection(summary)
        if stage_gate.completed:
            supervisor["status"] = "passed"
            exit_code = 0
            marker = "CRAFTGROUND_TIMING_PARALLEL_OK"
        else:
            supervisor["status"] = "stopped"
            supervisor["primary_failure"] = stage_gate.stop_reason
            exit_code = 2
    except _StopProbe:
        pass
    except BaseException as error:
        supervisor["status"] = "failed"
        supervisor["primary_failure"] = f"{type(error).__name__}: {error}"
        exit_code = 2
    finally:
        cleanup_failures = list(supervisor["cleanup_failures"])
        if launch_preflight_passed and source_root is not None:
            try:
                actual_source_fingerprints = capture_runtime_source_fingerprints(
                    source_root
                )
            except BaseException as error:
                cleanup_failures.append(
                    f"runtime-source-postflight: {type(error).__name__}: {error}"
                )
                actual_source_fingerprints = {}
            cleanup_failures.extend(
                evaluate_postflight(
                    candidate_ports=config.candidate_ports,
                    port_is_free=_port_is_free,
                    actual_source_fingerprints=actual_source_fingerprints,
                    expected_source_fingerprints=MC121_RUNTIME_0_1_0_FINGERPRINTS,
                )
            )
        supervisor["cleanup_failures"] = list(dict.fromkeys(cleanup_failures))
        if cleanup_failures:
            supervisor["status"] = "failed"
            exit_code = 2
            marker = "CRAFTGROUND_TIMING_PARALLEL_FAILED"
        supervisor["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        supervisor["exit_code"] = exit_code
        supervisor["marker"] = marker
        write_json_atomic(run_dir / "supervisor.json", supervisor)
        print(marker)
        print(f"artifacts={run_dir}")
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = ProbeConfigV0.from_namespace(arguments)
    except ValueError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    return run_probe(config)


if __name__ == "__main__":
    raise SystemExit(main())
