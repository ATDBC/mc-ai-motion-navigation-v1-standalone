"""CraftGround-free primitives for the Phase 0A control probe."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import threading
import time
from typing import Any, Mapping, Sequence


ActionValue = bool | float
ActionSnapshot = dict[str, ActionValue]

BOOL_ACTION_KEYS = (
    "attack",
    "back",
    "forward",
    "jump",
    "left",
    "right",
    "sneak",
    "sprint",
    "use",
    "drop",
    "inventory",
    "hotbar.1",
    "hotbar.2",
    "hotbar.3",
    "hotbar.4",
    "hotbar.5",
    "hotbar.6",
    "hotbar.7",
    "hotbar.8",
    "hotbar.9",
)
CAMERA_ACTION_KEYS = ("camera_pitch", "camera_yaw")
PRIVILEGED_CANDIDATE_FIELDS = (
    "height_info",
    "surrounding_blocks",
    "surrounding_entities",
)
CHECK_REQUIREMENTS = {
    "record_count": "57 records with indexes 0..56",
    "not_terminated": "no step is terminated or truncated",
    "alive": "no observation reports is_dead=true",
    "yaw_changed": "absolute cumulative yaw change > 30 degrees",
    "forward_moved": "forward net displacement > 0.5 block",
    "release_slowed": "last-three release median < forward step median",
    "pulses_released": (
        "attack/use/hotbar pulse each has following neutral steps"
    ),
    "watchdog_clear": "supervisor deadline did not expire",
    "process_stopped": "captured CraftGround process is not alive after close",
    "port_released": "actual control port released within 5 seconds",
}


class FailureKind(StrEnum):
    CONFIGURATION_ERROR = "configuration_error"
    BACKEND_START_FAILURE = "backend_start_failure"
    PROBE_TIMEOUT = "probe_timeout"
    CANCELLED = "cancelled"
    OBSERVATION_INVARIANT_FAILURE = "observation_invariant_failure"
    ACTION_INVARIANT_FAILURE = "action_invariant_failure"
    SEMANTIC_MISMATCH = "semantic_mismatch"
    CLEANUP_FAILURE = "cleanup_failure"
    UNEXPECTED_ERROR = "unexpected_error"


class ProbeError(RuntimeError):
    """A probe failure whose category is stable in serialized reports."""

    def __init__(self, kind: FailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ScheduledAction:
    phase: str
    action: ActionSnapshot


@dataclass(frozen=True)
class SupervisionResult:
    exit_code: int
    timed_out: bool
    cancelled: bool
    forced_termination: bool


def neutral_action() -> ActionSnapshot:
    """Return a complete V2 action snapshot that releases every input."""

    return {
        **{key: False for key in BOOL_ACTION_KEYS},
        "camera_pitch": 0.0,
        "camera_yaw": 0.0,
    }


def validate_action(action: Mapping[str, object]) -> None:
    """Reject incomplete, contradictory, or non-finite V2 snapshots."""

    expected = set(BOOL_ACTION_KEYS + CAMERA_ACTION_KEYS)
    actual = set(action)
    if actual != expected:
        raise ProbeError(
            FailureKind.ACTION_INVARIANT_FAILURE,
            f"action keys differ: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}",
        )
    for key in BOOL_ACTION_KEYS:
        if type(action[key]) is not bool:
            raise ProbeError(
                FailureKind.ACTION_INVARIANT_FAILURE,
                f"{key} must be bool",
            )
    for key in CAMERA_ACTION_KEYS:
        value = action[key]
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ProbeError(
                FailureKind.ACTION_INVARIANT_FAILURE,
                f"{key} must be finite",
            )
        if not -180.0 <= float(value) <= 180.0:
            raise ProbeError(
                FailureKind.ACTION_INVARIANT_FAILURE,
                f"{key} outside [-180, 180]",
            )
    if action["forward"] and action["back"]:
        raise ProbeError(
            FailureKind.ACTION_INVARIANT_FAILURE,
            "forward and back conflict",
        )
    if action["left"] and action["right"]:
        raise ProbeError(
            FailureKind.ACTION_INVARIANT_FAILURE,
            "left and right conflict",
        )
    if sum(bool(action[f"hotbar.{index}"]) for index in range(1, 10)) > 1:
        raise ProbeError(
            FailureKind.ACTION_INVARIANT_FAILURE,
            "multiple hotbar keys conflict",
        )


def make_action(**overrides: ActionValue) -> ActionSnapshot:
    """Build and validate a full snapshot from sparse overrides."""

    action = neutral_action()
    unknown = set(overrides) - set(action)
    if unknown:
        raise ProbeError(
            FailureKind.ACTION_INVARIANT_FAILURE,
            f"unknown action keys: {sorted(unknown)}",
        )
    action.update(overrides)
    validate_action(action)
    return action


def _repeat(
    phase: str,
    count: int,
    **overrides: ActionValue,
) -> list[ScheduledAction]:
    return [ScheduledAction(phase, make_action(**overrides)) for _ in range(count)]


def build_schedule() -> tuple[ScheduledAction, ...]:
    """Return the fixed 56-step experiment approved in the design."""

    items: list[ScheduledAction] = []
    items += _repeat("settle", 5)
    items += _repeat("yaw_probe", 6, camera_yaw=15.0)
    items += _repeat("post_yaw", 2)
    items += _repeat("forward_probe", 20, forward=True)
    items += _repeat("release_probe", 5)
    items += _repeat("interrupt_forward", 4, forward=True)
    items += _repeat("interrupt_neutral", 5)
    items += _repeat("attack_pulse", 1, attack=True)
    items += _repeat("attack_pulse", 2)
    items += _repeat("use_pulse", 1, use=True)
    items += _repeat("use_pulse", 2)
    items += _repeat("hotbar_2_pulse", 1, **{"hotbar.2": True})
    items += _repeat("hotbar_2_pulse", 2)
    return tuple(items)


def strict_json_dumps(value: object) -> str:
    """Serialize evidence without JavaScript-only NaN or Infinity values."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )


def _finite_float(name: str, value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ProbeError(
            FailureKind.OBSERVATION_INVARIANT_FAILURE,
            f"{name} is not numeric",
        ) from error
    if not math.isfinite(result):
        raise ProbeError(
            FailureKind.OBSERVATION_INVARIANT_FAILURE,
            f"{name} is not finite",
        )
    return result


def extract_observation_summary(
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract deployable basics while only flagging privileged field presence."""

    missing = {"full"} - set(observation)
    if missing:
        raise ProbeError(
            FailureKind.OBSERVATION_INVARIANT_FAILURE,
            f"observation missing keys: {sorted(missing)}",
        )
    full = observation["full"]
    try:
        present_fields = sorted(
            descriptor.name for descriptor, _ in full.ListFields()
        )
        summary = {
            "position_blocks": [
                _finite_float("x", full.x),
                _finite_float("y", full.y),
                _finite_float("z", full.z),
            ],
            "yaw_degrees": _finite_float("yaw", full.yaw),
            "pitch_degrees": _finite_float("pitch", full.pitch),
            "velocity_craftground_raw": [
                _finite_float("velocity_x", full.velocity_x),
                _finite_float("velocity_y", full.velocity_y),
                _finite_float("velocity_z", full.velocity_z),
            ],
            "world_time_ticks": int(full.world_time),
            "is_on_ground": bool(full.is_on_ground),
            "is_dead": bool(full.is_dead),
            "health": _finite_float("health", full.health),
            "food_level": _finite_float("food_level", full.food_level),
            "present_fields": present_fields,
            "privileged_fields_present": sorted(
                set(present_fields).intersection(PRIVILEGED_CANDIDATE_FIELDS)
            ),
        }
    except AttributeError as error:
        raise ProbeError(
            FailureKind.OBSERVATION_INVARIANT_FAILURE,
            f"observation shape is invalid: {error}",
        ) from error
    strict_json_dumps(summary)
    return summary


def signed_angle_delta_degrees(previous: float, current: float) -> float:
    """Return the shortest signed change from previous to current yaw."""

    return ((current - previous + 180.0) % 360.0) - 180.0


def _horizontal_distance(
    first: Sequence[float],
    second: Sequence[float],
) -> float:
    return math.hypot(second[0] - first[0], second[2] - first[2])


def make_step_record(
    *,
    run_id: str,
    episode_id: str,
    step_index: int,
    phase: str,
    action: Mapping[str, object],
    send_started_monotonic_ns: int,
    observation_received_monotonic_ns: int,
    observation: Mapping[str, Any],
    previous_observation: Mapping[str, Any],
    reward: float,
    terminated: bool,
    truncated: bool,
) -> dict[str, Any]:
    """Create one strict transition record from consecutive observations."""

    validate_action(action)
    record = {
        "schema_version": "craftground-control-probe.step.v1",
        "run_id": run_id,
        "episode_id": episode_id,
        "step_index": step_index,
        "phase": phase,
        "action": dict(action),
        "timing": {
            "send_started_monotonic_ns": send_started_monotonic_ns,
            "observation_received_monotonic_ns": (
                observation_received_monotonic_ns
            ),
            "round_trip_ns": (
                observation_received_monotonic_ns
                - send_started_monotonic_ns
            ),
        },
        "observation": dict(observation),
        "transition": {
            "horizontal_displacement_blocks": _horizontal_distance(
                previous_observation["position_blocks"],
                observation["position_blocks"],
            ),
            "yaw_delta_degrees": signed_angle_delta_degrees(
                float(previous_observation["yaw_degrees"]),
                float(observation["yaw_degrees"]),
            ),
            "world_time_delta": (
                int(observation["world_time_ticks"])
                - int(previous_observation["world_time_ticks"])
            ),
        },
        "outcome": {
            "reward": _finite_float("reward", reward),
            "terminated": bool(terminated),
            "truncated": bool(truncated),
        },
    }
    strict_json_dumps(record)
    return record


def make_reset_record(
    *,
    run_id: str,
    episode_id: str,
    reset_started_monotonic_ns: int,
    observation_received_monotonic_ns: int,
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Create the index-zero observation record for an episode."""

    record = {
        "schema_version": "craftground-control-probe.step.v1",
        "run_id": run_id,
        "episode_id": episode_id,
        "step_index": 0,
        "phase": "reset",
        "action": None,
        "timing": {
            "send_started_monotonic_ns": reset_started_monotonic_ns,
            "observation_received_monotonic_ns": (
                observation_received_monotonic_ns
            ),
            "round_trip_ns": (
                observation_received_monotonic_ns
                - reset_started_monotonic_ns
            ),
        },
        "observation": dict(observation),
        "transition": None,
        "outcome": {
            "reward": 0.0,
            "terminated": False,
            "truncated": False,
        },
    }
    strict_json_dumps(record)
    return record


def failure_dict(
    kind: FailureKind,
    error: BaseException,
) -> dict[str, str]:
    """Serialize a failure without discarding its stable category."""

    return {
        "kind": kind.value,
        "exception_type": type(error).__name__,
        "message": str(error),
    }


def _records_for_phase(
    records: Sequence[Mapping[str, Any]],
    phase: str,
) -> list[Mapping[str, Any]]:
    return [record for record in records if record["phase"] == phase]


def _phase_net_distance(
    records: Sequence[Mapping[str, Any]],
    phase: str,
) -> float:
    indexes = [
        index for index, record in enumerate(records) if record["phase"] == phase
    ]
    if not indexes or indexes[0] == 0:
        raise ProbeError(
            FailureKind.SEMANTIC_MISMATCH,
            f"phase {phase} is incomplete",
        )
    start = records[indexes[0] - 1]["observation"]["position_blocks"]
    end = records[indexes[-1]]["observation"]["position_blocks"]
    return _horizontal_distance(start, end)


def _is_neutral(action: Mapping[str, object]) -> bool:
    return (
        all(action[key] is False for key in BOOL_ACTION_KEYS)
        and float(action["camera_pitch"]) == 0.0
        and float(action["camera_yaw"]) == 0.0
    )


def _pulse_released(
    records: Sequence[Mapping[str, Any]],
    phase: str,
    active_key: str,
) -> bool:
    phase_records = _records_for_phase(records, phase)
    return (
        len(phase_records) == 3
        and phase_records[0]["action"][active_key] is True
        and all(_is_neutral(record["action"]) for record in phase_records[1:])
    )


def compute_metrics(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute empirical metrics from one complete fixed schedule."""

    yaw_records = _records_for_phase(records, "yaw_probe")
    post_yaw_records = _records_for_phase(records, "post_yaw")
    forward_records = _records_for_phase(records, "forward_probe")
    release_records = _records_for_phase(records, "release_probe")
    yaw_deltas = [
        float(record["transition"]["yaw_delta_degrees"])
        for record in yaw_records
    ]
    forward_steps = [
        float(record["transition"]["horizontal_displacement_blocks"])
        for record in forward_records
    ]
    release_steps = [
        float(record["transition"]["horizontal_displacement_blocks"])
        for record in release_records
    ]
    step_round_trips = [
        int(record["timing"]["round_trip_ns"]) for record in records[1:]
    ]
    if (
        not yaw_deltas
        or not post_yaw_records
        or not forward_steps
        or len(release_steps) < 3
        or not step_round_trips
    ):
        raise ProbeError(
            FailureKind.SEMANTIC_MISMATCH,
            "required schedule phases are incomplete",
        )
    onset_lag = next(
        (
            index
            for index, value in enumerate(yaw_deltas)
            if not math.isclose(value, 0.0, abs_tol=1e-6)
        ),
        None,
    )
    yaw_phase_degrees = sum(yaw_deltas)
    yaw_tail_degrees = float(
        post_yaw_records[0]["transition"]["yaw_delta_degrees"]
    )
    return {
        "record_count": len(records),
        "yaw_probe_cumulative_signed_degrees": yaw_phase_degrees,
        "yaw_probe_cumulative_abs_degrees": sum(
            abs(value) for value in yaw_deltas
        ),
        "yaw_probe_onset_lag_steps": onset_lag,
        "yaw_probe_phase_degrees": yaw_phase_degrees,
        "yaw_probe_tail_degrees": yaw_tail_degrees,
        "yaw_probe_total_response_degrees": (
            yaw_phase_degrees + yaw_tail_degrees
        ),
        "forward_probe_net_displacement_blocks": _phase_net_distance(
            records,
            "forward_probe",
        ),
        "forward_probe_step_median_blocks": statistics.median(forward_steps),
        "release_probe_last_three_median_blocks": statistics.median(
            release_steps[-3:]
        ),
        "step_round_trip_median_ms": (
            statistics.median(step_round_trips) / 1_000_000
        ),
        "step_round_trip_max_ms": max(step_round_trips) / 1_000_000,
        "world_time_deltas": [
            int(record["transition"]["world_time_delta"])
            for record in records[1:]
        ],
    }


def _named_check(
    name: str,
    passed: bool,
    observed: object,
) -> dict[str, object]:
    return {
        "name": name,
        "passed": bool(passed),
        "observed": observed,
        "requirement": CHECK_REQUIREMENTS[name],
    }


def evaluate_checks(
    records: Sequence[Mapping[str, Any]],
    *,
    watchdog_triggered: bool,
    process_alive_after_close: bool,
    port_released: bool,
) -> list[dict[str, object]]:
    """Evaluate every named acceptance condition without hiding cleanup."""

    expected_indexes = list(range(57))
    actual_indexes = [int(record["step_index"]) for record in records]
    complete = len(records) == 57 and actual_indexes == expected_indexes
    metrics = compute_metrics(records) if complete else None
    not_terminated = all(
        not record["outcome"]["terminated"]
        and not record["outcome"]["truncated"]
        for record in records
    )
    alive = all(not record["observation"]["is_dead"] for record in records)
    pulses_released = all(
        (
            _pulse_released(records, "attack_pulse", "attack"),
            _pulse_released(records, "use_pulse", "use"),
            _pulse_released(records, "hotbar_2_pulse", "hotbar.2"),
        )
    )
    yaw_change = (
        None
        if metrics is None
        else metrics["yaw_probe_cumulative_abs_degrees"]
    )
    forward_net = (
        None
        if metrics is None
        else metrics["forward_probe_net_displacement_blocks"]
    )
    forward_median = (
        None
        if metrics is None
        else metrics["forward_probe_step_median_blocks"]
    )
    release_median = (
        None
        if metrics is None
        else metrics["release_probe_last_three_median_blocks"]
    )
    return [
        _named_check(
            "record_count",
            complete,
            {"count": len(records), "indexes": actual_indexes},
        ),
        _named_check("not_terminated", not_terminated, not_terminated),
        _named_check("alive", alive, alive),
        _named_check(
            "yaw_changed",
            yaw_change is not None and yaw_change > 30.0,
            yaw_change,
        ),
        _named_check(
            "forward_moved",
            forward_net is not None and forward_net > 0.5,
            forward_net,
        ),
        _named_check(
            "release_slowed",
            release_median is not None
            and forward_median is not None
            and release_median < forward_median,
            {
                "release_last_three_median": release_median,
                "forward_median": forward_median,
            },
        ),
        _named_check(
            "pulses_released",
            pulses_released,
            pulses_released,
        ),
        _named_check(
            "watchdog_clear",
            not watchdog_triggered,
            watchdog_triggered,
        ),
        _named_check(
            "process_stopped",
            not process_alive_after_close,
            process_alive_after_close,
        ),
        _named_check("port_released", port_released, port_released),
    ]


def _interrupt_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(process.pid, signal.SIGINT)


def _force_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        if process.poll() is None:
            process.kill()
    else:
        os.killpg(process.pid, signal.SIGKILL)


def supervise_process(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    grace_seconds: float,
    cancel_event: threading.Event | None = None,
) -> SupervisionResult:
    """Run one exact worker with graceful cancellation and a hard deadline."""

    if not command:
        raise ProbeError(
            FailureKind.CONFIGURATION_ERROR,
            "worker command is empty",
        )
    if timeout_seconds <= 0:
        raise ProbeError(
            FailureKind.CONFIGURATION_ERROR,
            "timeout_seconds must be positive",
        )
    if grace_seconds < 0:
        raise ProbeError(
            FailureKind.CONFIGURATION_ERROR,
            "grace_seconds must be non-negative",
        )

    popen_options: dict[str, object] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(list(command), **popen_options)
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    cancelled = False
    forced = False

    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        cancelled = True

    if process.poll() is None and (timed_out or cancelled):
        try:
            _interrupt_process(process)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            forced = True
            _force_process_tree(process)
            process.wait(timeout=5.0)

    exit_code = process.wait()
    return SupervisionResult(
        exit_code=exit_code,
        timed_out=timed_out,
        cancelled=cancelled,
        forced_termination=forced,
    )


def append_jsonl(path: Path, value: object) -> None:
    """Append and durably flush one strict JSON line."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(strict_json_dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_json_atomic(path: Path, value: object, *, replace_retry_seconds: float = 0) -> None:
    """Replace a JSON document only after its complete temporary is durable."""
    if not 0<=replace_retry_seconds<=.1: raise ValueError('atomic replace retry must be 0..100ms')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(strict_json_dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    deadline=time.perf_counter()+replace_retry_seconds
    while True:
        try:
            temporary.replace(path)
            return
        except PermissionError as error:
            remaining=deadline-time.perf_counter()
            if remaining<=0 or getattr(error,'winerror',None) not in (5,32,33): raise
            time.sleep(min(.005,remaining))
