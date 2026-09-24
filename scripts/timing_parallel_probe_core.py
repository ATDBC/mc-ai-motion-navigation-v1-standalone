"""Pure schedules and verdicts for the CraftGround timing/parallel probe."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import math
import statistics
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

import psutil

from mc2p.backends.craftground_runtime import CraftGroundClockModeV0


TIMING_STEP_COUNT = 100
CAPACITY_STEP_COUNT = 256
CAPACITY_WARMUP_STEPS = 16
TIMING_SEEDS = (21001, 21002, 21003)
EXPECTED_OBSERVATION_LAG_STEPS = 1
YAW_TOLERANCE_DEGREES = 1.0
POSITION_ABSOLUTE_TOLERANCE_BLOCKS = 0.15
POSITION_RELATIVE_TOLERANCE = 0.05
JUMP_HEIGHT_TOLERANCE_BLOCKS = 0.15
AIRBORNE_TOLERANCE_TICKS = 2
WORLD_TIME_ABSOLUTE_TOLERANCE_TICKS = 2
WORLD_TIME_RELATIVE_TOLERANCE = 0.05
MIN_FORWARD_DISTANCE_BLOCKS = 1.0
MIN_JUMP_HEIGHT_BLOCKS = 0.1
MAX_RELEASE_TAIL_DRIFT_BLOCKS = 0.02
MAX_FINAL_NEUTRAL_TAIL_DRIFT_BLOCKS = 0.02
RAM_GUARD_PERCENT = 85.0
VRAM_GUARD_PERCENT = 90.0
RECOMMENDED_EFFICIENCY = 0.60
GPU_QUERY_TIMEOUT_SECONDS = 0.25
_FLOAT_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class ScheduledIntentV0:
    cycle: int
    phase: str
    forward: bool = False
    back: bool = False
    left: bool = False
    right: bool = False
    jump: bool = False
    sneak: bool = False
    sprint: bool = False
    camera_pitch_delta: float = 0.0
    camera_yaw_delta: float = 0.0

    @property
    def is_neutral(self) -> bool:
        return not any(
            (
                self.forward,
                self.back,
                self.left,
                self.right,
                self.jump,
                self.sneak,
                self.sprint,
            )
        ) and self.camera_pitch_delta == 0.0 and self.camera_yaw_delta == 0.0


def _scheduled_range(
    start: int,
    stop: int,
    phase: str,
    **values: bool | float,
) -> list[ScheduledIntentV0]:
    return [ScheduledIntentV0(cycle=cycle, phase=phase, **values) for cycle in range(start, stop)]


def build_timing_schedule() -> tuple[ScheduledIntentV0, ...]:
    schedule: list[ScheduledIntentV0] = []
    schedule.extend(_scheduled_range(0, 12, "neutral-settle"))
    schedule.extend(
        _scheduled_range(12, 13, "yaw-pulse", camera_yaw_delta=90.0)
    )
    schedule.extend(_scheduled_range(13, 17, "yaw-release"))
    schedule.extend(_scheduled_range(17, 37, "forward", forward=True))
    schedule.extend(
        _scheduled_range(37, 57, "sprint", forward=True, sprint=True)
    )
    schedule.extend(_scheduled_range(57, 65, "locomotion-release"))
    schedule.extend(
        _scheduled_range(65, 66, "jump-pulse", forward=True, jump=True)
    )
    schedule.extend(_scheduled_range(66, 90, "jump-forward", forward=True))
    schedule.extend(_scheduled_range(90, 100, "final-neutral"))
    return tuple(schedule)


def _capacity_cycle() -> tuple[ScheduledIntentV0, ...]:
    cycle: list[ScheduledIntentV0] = []
    cycle.extend(_scheduled_range(0, 4, "capacity-neutral"))
    cycle.extend(_scheduled_range(4, 12, "capacity-forward", forward=True))
    cycle.extend(
        _scheduled_range(
            12,
            20,
            "capacity-sprint",
            forward=True,
            sprint=True,
        )
    )
    cycle.extend(
        _scheduled_range(20, 21, "capacity-yaw", camera_yaw_delta=15.0)
    )
    cycle.extend(_scheduled_range(21, 24, "capacity-release"))
    cycle.extend(
        _scheduled_range(24, 25, "capacity-jump", forward=True, jump=True)
    )
    cycle.extend(_scheduled_range(25, 32, "capacity-final-neutral"))
    return tuple(cycle)


def build_capacity_schedule() -> tuple[ScheduledIntentV0, ...]:
    template = _capacity_cycle()
    return tuple(
        ScheduledIntentV0(
            cycle=block * len(template) + item.cycle,
            phase=item.phase,
            forward=item.forward,
            back=item.back,
            left=item.left,
            right=item.right,
            jump=item.jump,
            sneak=item.sneak,
            sprint=item.sprint,
            camera_pitch_delta=item.camera_pitch_delta,
            camera_yaw_delta=item.camera_yaw_delta,
        )
        for block in range(8)
        for item in template
    )


@dataclass(frozen=True, slots=True)
class TimingSampleV0:
    cycle: int
    phase: str
    action_sequence_id: int
    observation_sequence_id: int
    request_sequence_id: int | None
    world_time_ticks: int | None
    x: float | None
    y: float | None
    z: float | None
    yaw_degrees: float | None
    pitch_degrees: float | None
    is_on_ground: bool | None
    is_dead: bool | None
    health_points: float | None
    food_points: float | None
    step_latency_seconds: float
    terminated: bool
    truncated: bool
    schema_version: str = field(default="mc2p.timing-sample.v0", init=False)


@dataclass(frozen=True, slots=True)
class TimingRunResultV0:
    worker_id: str
    clock_mode: CraftGroundClockModeV0
    seed: int
    attempt: int
    wall_clock_paced: bool
    sandbox_path: str
    requested_port: int
    actual_port: int
    samples: tuple[TimingSampleV0, ...]
    primary_failure: str | None
    cleanup_failures: tuple[str, ...]
    process_stopped: bool
    port_released: bool
    reset_world_time_ticks: int | None = None
    schema_version: str = field(default="mc2p.timing-run.v0", init=False)


@dataclass(frozen=True, slots=True)
class CheckResultV0:
    name: str
    passed: bool
    actual: object
    expected: object
    tolerance: float | None = None


@dataclass(frozen=True, slots=True)
class LockstepTraceEventV0:
    session_id: str
    event: str
    generation: int
    server_world_time: int | None
    client_world_time: int | None
    schema_version: str = field(default="mc2p.lockstep-trace.v0", init=False)


def parse_lockstep_trace_lines(
    lines: Iterable[str],
) -> tuple[LockstepTraceEventV0, ...]:
    """Parse the exact append-only trace schema emitted by the Kotlin runtime."""

    expected_keys = {
        "schema_version",
        "session_id",
        "event",
        "generation",
        "server_world_time",
        "client_world_time",
    }
    allowed_events = {
        "server_arm_complete",
        "server_tick_complete",
        "client_observation",
    }
    events: list[LockstepTraceEventV0] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise ValueError(f"lockstep trace line {line_number} has invalid keys")
        if raw["schema_version"] != "mc2p.lockstep-trace.v0":
            raise ValueError(f"lockstep trace line {line_number} has invalid schema")
        session_id = raw["session_id"]
        event = raw["event"]
        generation = raw["generation"]
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"lockstep trace line {line_number} has invalid session")
        if event not in allowed_events:
            raise ValueError(f"lockstep trace line {line_number} has invalid event")
        if type(generation) is not int or generation < 0:
            raise ValueError(f"lockstep trace line {line_number} has invalid generation")
        world_times: dict[str, int | None] = {}
        for key in ("server_world_time", "client_world_time"):
            value = raw[key]
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(
                    f"lockstep trace line {line_number} has invalid {key}"
                )
            world_times[key] = value
        events.append(
            LockstepTraceEventV0(
                session_id=session_id,
                event=event,
                generation=generation,
                server_world_time=world_times["server_world_time"],
                client_world_time=world_times["client_world_time"],
            )
        )
    return tuple(events)


@dataclass(frozen=True, slots=True)
class TimingPairReportV0:
    seed: int
    attempt: int
    clean: bool
    equivalent: bool
    checks: tuple[CheckResultV0, ...]
    infrastructure_failures: tuple[str, ...]
    schema_version: str = field(default="mc2p.timing-pair.v0", init=False)

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)


class TimingVerdictV0(StrEnum):
    EQUIVALENT = "equivalent"
    NON_EQUIVALENT = "non_equivalent"
    INCONCLUSIVE = "inconclusive"


def _check(
    name: str,
    passed: bool,
    actual: object,
    expected: object,
    tolerance: float | None = None,
) -> CheckResultV0:
    return CheckResultV0(name, bool(passed), actual, expected, tolerance)


def _finite_optional(value: object) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(float(value))


def _signed_angle_delta(previous: float, current: float) -> float:
    return (current - previous + 180.0) % 360.0 - 180.0


def _horizontal_distance(start: TimingSampleV0, end: TimingSampleV0) -> float:
    assert start.x is not None and start.z is not None
    assert end.x is not None and end.z is not None
    return math.hypot(end.x - start.x, end.z - start.z)


def _world_delta(start: TimingSampleV0, end: TimingSampleV0) -> int:
    assert start.world_time_ticks is not None and end.world_time_ticks is not None
    return end.world_time_ticks - start.world_time_ticks


def _camera_lag(samples: Sequence[TimingSampleV0]) -> int | None:
    baseline = samples[11].yaw_degrees
    if baseline is None:
        return None
    for sample in samples[12:17]:
        if sample.yaw_degrees is None:
            return None
        if abs(_signed_angle_delta(baseline, sample.yaw_degrees)) > _FLOAT_EPSILON:
            return sample.cycle - 12
    return None


def _airborne_duration(samples: Sequence[TimingSampleV0]) -> int | None:
    airborne = [
        sample
        for sample in samples[65:100]
        if sample.is_on_ground is False and sample.world_time_ticks is not None
    ]
    if not airborne:
        return None
    assert airborne[0].world_time_ticks is not None
    assert airborne[-1].world_time_ticks is not None
    return airborne[-1].world_time_ticks - airborne[0].world_time_ticks + 1


def _optional_horizontal_distance(
    samples: Sequence[TimingSampleV0],
    start: int,
    end: int,
) -> float | None:
    endpoints = (samples[start], samples[end])
    if not all(
        _finite_optional(value)
        for sample in endpoints
        for value in (sample.x, sample.z)
    ):
        return None
    return _horizontal_distance(endpoints[0], endpoints[1])


def _lockstep_behavior_checks(
    samples: Sequence[TimingSampleV0],
) -> list[CheckResultV0]:
    if len(samples) != TIMING_STEP_COUNT:
        return []
    forward_distance = _optional_horizontal_distance(samples, 17, 37)
    sprint_distance = _optional_horizontal_distance(samples, 37, 57)
    release_tail_drift = _optional_horizontal_distance(samples, 59, 64)
    final_neutral_tail_drift = _optional_horizontal_distance(samples, 92, 99)
    jump_values = [sample.y for sample in samples[65:91]]
    jump_base = samples[64].y
    if _finite_optional(jump_base) and all(
        _finite_optional(value) for value in jump_values
    ):
        jump_height = max(float(value) for value in jump_values) - float(jump_base)
    else:
        jump_height = None
    airborne_samples = sum(sample.is_on_ground is False for sample in samples[65:91])
    return [
        _check(
            "lockstep.forward-distance",
            forward_distance is not None
            and forward_distance >= MIN_FORWARD_DISTANCE_BLOCKS,
            forward_distance,
            f">= {MIN_FORWARD_DISTANCE_BLOCKS}",
        ),
        _check(
            "lockstep.sprint-faster-than-forward",
            sprint_distance is not None
            and forward_distance is not None
            and sprint_distance > forward_distance,
            sprint_distance,
            f"> {forward_distance}",
        ),
        _check(
            "lockstep.jump-response",
            jump_height is not None
            and jump_height >= MIN_JUMP_HEIGHT_BLOCKS
            and airborne_samples > 0,
            {"height": jump_height, "airborne_samples": airborne_samples},
            {
                "height": f">= {MIN_JUMP_HEIGHT_BLOCKS}",
                "airborne_samples": "> 0",
            },
        ),
        _check(
            "lockstep.release-tail-drift",
            release_tail_drift is not None
            and release_tail_drift <= MAX_RELEASE_TAIL_DRIFT_BLOCKS,
            release_tail_drift,
            f"<= {MAX_RELEASE_TAIL_DRIFT_BLOCKS}",
        ),
        _check(
            "lockstep.final-neutral-tail-drift",
            final_neutral_tail_drift is not None
            and final_neutral_tail_drift <= MAX_FINAL_NEUTRAL_TAIL_DRIFT_BLOCKS,
            final_neutral_tail_drift,
            f"<= {MAX_FINAL_NEUTRAL_TAIL_DRIFT_BLOCKS}",
        ),
    ]


def _run_structure_checks(
    run: TimingRunResultV0,
    expected_mode: CraftGroundClockModeV0,
) -> list[CheckResultV0]:
    checks = [
        _check(f"{expected_mode.value}.mode", run.clock_mode is expected_mode, run.clock_mode.value, expected_mode.value),
        _check(f"{expected_mode.value}.sample-count", len(run.samples) == TIMING_STEP_COUNT, len(run.samples), TIMING_STEP_COUNT),
        _check(f"{expected_mode.value}.actual-port", run.actual_port == run.requested_port, run.actual_port, run.requested_port),
        _check(f"{expected_mode.value}.process-stopped", run.process_stopped, run.process_stopped, True),
        _check(f"{expected_mode.value}.port-released", run.port_released, run.port_released, True),
    ]
    expected_pacing = expected_mode is CraftGroundClockModeV0.REFERENCE_20_TPS
    checks.append(
        _check(
            f"{expected_mode.value}.wall-clock-pacing",
            run.wall_clock_paced is expected_pacing,
            run.wall_clock_paced,
            expected_pacing,
        )
    )
    if len(run.samples) != TIMING_STEP_COUNT:
        return checks
    schedule = build_timing_schedule()
    for cycle, (sample, intended) in enumerate(zip(run.samples, schedule, strict=True)):
        checks.extend(
            (
                _check(f"{expected_mode.value}.cycle-{cycle}.cycle", sample.cycle == cycle, sample.cycle, cycle),
                _check(f"{expected_mode.value}.cycle-{cycle}.phase", sample.phase == intended.phase, sample.phase, intended.phase),
                _check(f"{expected_mode.value}.cycle-{cycle}.action-sequence", sample.action_sequence_id == cycle, sample.action_sequence_id, cycle),
                _check(f"{expected_mode.value}.cycle-{cycle}.observation-sequence", sample.observation_sequence_id == cycle + 1, sample.observation_sequence_id, cycle + 1),
                _check(f"{expected_mode.value}.cycle-{cycle}.request-sequence", sample.request_sequence_id == cycle, sample.request_sequence_id, cycle),
                _check(
                    f"{expected_mode.value}.cycle-{cycle}.required-fields",
                    all(
                        _finite_optional(value)
                        for value in (
                            sample.world_time_ticks,
                            sample.x,
                            sample.y,
                            sample.z,
                            sample.yaw_degrees,
                            sample.pitch_degrees,
                            sample.health_points,
                            sample.food_points,
                            sample.step_latency_seconds,
                        )
                    )
                    and isinstance(sample.is_on_ground, bool)
                    and isinstance(sample.is_dead, bool)
                    and sample.step_latency_seconds >= 0,
                    "valid" if sample.x is not None else "missing",
                    "all required fields valid",
                ),
                _check(f"{expected_mode.value}.cycle-{cycle}.alive", sample.is_dead is False, sample.is_dead, False),
                _check(f"{expected_mode.value}.cycle-{cycle}.terminated", not sample.terminated, sample.terminated, False),
                _check(f"{expected_mode.value}.cycle-{cycle}.truncated", not sample.truncated, sample.truncated, False),
            )
        )
        if cycle > 0:
            previous_world_time = run.samples[cycle - 1].world_time_ticks
            checks.append(
                _check(
                    f"{expected_mode.value}.cycle-{cycle}.world-time-monotonic",
                    previous_world_time is not None
                    and sample.world_time_ticks is not None
                    and sample.world_time_ticks >= previous_world_time,
                    sample.world_time_ticks,
                    f">= {previous_world_time}",
                )
            )
    checks.append(
        _check(
            f"{expected_mode.value}.final-on-ground",
            run.samples[-1].is_on_ground is True,
            run.samples[-1].is_on_ground,
            True,
        )
    )
    lag = _camera_lag(run.samples)
    checks.append(
        _check(
            f"{expected_mode.value}.camera-lag",
            lag == EXPECTED_OBSERVATION_LAG_STEPS,
            lag,
            EXPECTED_OBSERVATION_LAG_STEPS,
        )
    )
    return checks


def _within(actual: float, expected: float, tolerance: float) -> bool:
    return abs(actual - expected) <= tolerance + _FLOAT_EPSILON


def compare_timing_pair(
    reference: TimingRunResultV0,
    accelerated: TimingRunResultV0,
) -> TimingPairReportV0:
    if reference.seed != accelerated.seed:
        raise ValueError("timing pair seeds differ")
    attempt = max(reference.attempt, accelerated.attempt)
    infrastructure_failures: list[str] = []
    for label, run in (("reference", reference), ("accelerated", accelerated)):
        if run.primary_failure is not None:
            infrastructure_failures.append(f"{label}: {run.primary_failure}")
        infrastructure_failures.extend(
            f"{label} cleanup: {failure}" for failure in run.cleanup_failures
        )
        if not run.process_stopped:
            infrastructure_failures.append(f"{label}: process not stopped")
        if not run.port_released:
            infrastructure_failures.append(f"{label}: port not released")
        if run.actual_port != run.requested_port:
            infrastructure_failures.append(f"{label}: actual port mismatch")
    clean = not infrastructure_failures
    checks = _run_structure_checks(
        reference, CraftGroundClockModeV0.REFERENCE_20_TPS
    )
    checks.extend(
        _run_structure_checks(accelerated, CraftGroundClockModeV0.ACCELERATED)
    )
    if len(reference.samples) == TIMING_STEP_COUNT and len(accelerated.samples) == TIMING_STEP_COUNT:
        ref_lag = _camera_lag(reference.samples)
        acc_lag = _camera_lag(accelerated.samples)
        checks.append(_check("paired.camera-lag", ref_lag == acc_lag == 1, acc_lag, ref_lag))

        if all(
            sample.yaw_degrees is not None
            for sample in (reference.samples[11], reference.samples[13], accelerated.samples[11], accelerated.samples[13])
        ):
            ref_yaw = _signed_angle_delta(
                float(reference.samples[11].yaw_degrees),
                float(reference.samples[13].yaw_degrees),
            )
            acc_yaw = _signed_angle_delta(
                float(accelerated.samples[11].yaw_degrees),
                float(accelerated.samples[13].yaw_degrees),
            )
            checks.append(
                _check(
                    "paired.yaw-delta",
                    _within(acc_yaw, ref_yaw, YAW_TOLERANCE_DEGREES),
                    acc_yaw,
                    ref_yaw,
                    YAW_TOLERANCE_DEGREES,
                )
            )

        for name, start, end in (("forward", 17, 37), ("sprint", 37, 57)):
            relevant = (
                reference.samples[start],
                reference.samples[end],
                accelerated.samples[start],
                accelerated.samples[end],
            )
            if all(
                sample.x is not None and sample.z is not None for sample in relevant
            ):
                ref_distance = _horizontal_distance(relevant[0], relevant[1])
                acc_distance = _horizontal_distance(relevant[2], relevant[3])
                tolerance = max(
                    POSITION_ABSOLUTE_TOLERANCE_BLOCKS,
                    abs(ref_distance) * POSITION_RELATIVE_TOLERANCE,
                )
                checks.append(
                    _check(
                        f"paired.{name}-distance",
                        _within(acc_distance, ref_distance, tolerance),
                        acc_distance,
                        ref_distance,
                        tolerance,
                    )
                )

        for label, run in (("reference", reference), ("accelerated", accelerated)):
            if all(
                sample.x is not None and sample.z is not None
                for sample in (run.samples[17], run.samples[37], run.samples[57])
            ):
                forward_distance = _horizontal_distance(run.samples[17], run.samples[37])
                sprint_distance = _horizontal_distance(run.samples[37], run.samples[57])
                checks.append(
                    _check(
                        f"{label}.sprint-faster-than-forward",
                        sprint_distance > forward_distance,
                        sprint_distance,
                        f"> {forward_distance}",
                    )
                )

        if all(sample.y is not None for sample in reference.samples[65:91]) and all(
            sample.y is not None for sample in accelerated.samples[65:91]
        ):
            ref_base = float(reference.samples[65].y)
            acc_base = float(accelerated.samples[65].y)
            ref_peak = max(float(sample.y) for sample in reference.samples[65:91]) - ref_base
            acc_peak = max(float(sample.y) for sample in accelerated.samples[65:91]) - acc_base
            checks.append(
                _check(
                    "paired.jump-peak",
                    _within(acc_peak, ref_peak, JUMP_HEIGHT_TOLERANCE_BLOCKS),
                    acc_peak,
                    ref_peak,
                    JUMP_HEIGHT_TOLERANCE_BLOCKS,
                )
            )

        ref_air = _airborne_duration(reference.samples)
        acc_air = _airborne_duration(accelerated.samples)
        if ref_air is not None and acc_air is not None:
            checks.append(
                _check(
                    "paired.airborne-world-ticks",
                    abs(acc_air - ref_air) <= AIRBORNE_TOLERANCE_TICKS,
                    acc_air,
                    ref_air,
                    float(AIRBORNE_TOLERANCE_TICKS),
                )
            )
        else:
            checks.append(_check("paired.airborne-world-ticks", False, acc_air, ref_air))

        phase_bounds = (
            ("neutral-settle", 0, 12),
            ("camera", 11, 16),
            ("forward", 17, 37),
            ("sprint", 37, 57),
            ("release", 57, 65),
            ("jump", 65, 90),
            ("final", 90, 99),
        )
        for name, start, end in phase_bounds:
            relevant = (
                reference.samples[start],
                reference.samples[end],
                accelerated.samples[start],
                accelerated.samples[end],
            )
            if all(sample.world_time_ticks is not None for sample in relevant):
                ref_delta = _world_delta(relevant[0], relevant[1])
                acc_delta = _world_delta(relevant[2], relevant[3])
                tolerance = max(
                    float(WORLD_TIME_ABSOLUTE_TOLERANCE_TICKS),
                    abs(ref_delta) * WORLD_TIME_RELATIVE_TOLERANCE,
                )
                checks.append(
                    _check(
                        f"paired.{name}-world-ticks",
                        _within(float(acc_delta), float(ref_delta), tolerance),
                        acc_delta,
                        ref_delta,
                        tolerance,
                    )
                )

        for field_name in ("health_points", "food_points"):
            ref_value = getattr(reference.samples[-1], field_name)
            acc_value = getattr(accelerated.samples[-1], field_name)
            checks.append(
                _check(
                    f"paired.final-{field_name}",
                    ref_value is not None
                    and acc_value is not None
                    and _within(float(acc_value), float(ref_value), 0.0),
                    acc_value,
                    ref_value,
                    0.0,
                )
            )
    equivalent = clean and all(check.passed for check in checks)
    return TimingPairReportV0(
        seed=reference.seed,
        attempt=attempt,
        clean=clean,
        equivalent=equivalent,
        checks=tuple(checks),
        infrastructure_failures=tuple(infrastructure_failures),
    )


def validate_timing_run(run: TimingRunResultV0) -> tuple[CheckResultV0, ...]:
    """Validate one timing run without pretending it is a cross-mode comparison."""

    return tuple(_run_structure_checks(run, run.clock_mode))


def evaluate_lockstep_run(
    run: TimingRunResultV0,
    events: Sequence[LockstepTraceEventV0],
    *,
    reset_world_time_ticks: int,
) -> tuple[CheckResultV0, ...]:
    """Require exact action/world-tick correspondence in both client and server traces."""

    checks = [
        check
        for check in _run_structure_checks(
            run,
            CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
        )
        if check.name != "lockstep_accelerated.camera-lag"
    ]
    checks.append(
        _check(
            "lockstep.clean",
            run.primary_failure is None
            and not run.cleanup_failures
            and run.process_stopped
            and run.port_released
            and run.actual_port == run.requested_port,
            {
                "primary_failure": run.primary_failure,
                "cleanup_failures": list(run.cleanup_failures),
                "process_stopped": run.process_stopped,
                "port_released": run.port_released,
                "actual_port": run.actual_port,
            },
            "clean worker and exact port",
        )
    )
    if len(run.samples) == TIMING_STEP_COUNT:
        previous = reset_world_time_ticks
        for sample in run.samples:
            delta = (
                None
                if sample.world_time_ticks is None
                else sample.world_time_ticks - previous
            )
            checks.append(
                _check(
                    f"lockstep.cycle-{sample.cycle}.world-time-delta",
                    delta == 1,
                    delta,
                    1,
                )
            )
            if sample.world_time_ticks is not None:
                previous = sample.world_time_ticks
        checks.append(
            _check(
                "lockstep.camera-lag",
                _camera_lag(run.samples) == 0,
                _camera_lag(run.samples),
                0,
            )
        )
        checks.extend(_lockstep_behavior_checks(run.samples))

    sessions = {event.session_id for event in events}
    checks.append(
        _check("lockstep.trace-session-count", len(sessions) == 1, len(sessions), 1)
    )
    arm = [event for event in events if event.event == "server_arm_complete"]
    server = [event for event in events if event.event == "server_tick_complete"]
    client = [event for event in events if event.event == "client_observation"]
    expected_action_count = TIMING_STEP_COUNT + 1
    expected_order = [
        ("server_arm_complete", 0),
        ("client_observation", 0),
    ]
    for generation in range(1, expected_action_count + 1):
        expected_order.extend(
            (
                ("server_tick_complete", generation),
                ("client_observation", generation),
            )
        )
    actual_order = [(event.event, event.generation) for event in events]
    mismatch_index = next(
        (
            index
            for index, (actual, expected) in enumerate(
                zip(actual_order, expected_order, strict=False)
            )
            if actual != expected
        ),
        min(len(actual_order), len(expected_order))
        if len(actual_order) != len(expected_order)
        else None,
    )
    checks.extend(
        (
            _check(
                "lockstep.trace-causal-order",
                actual_order == expected_order,
                {
                    "event_count": len(actual_order),
                    "first_mismatch_index": mismatch_index,
                    "event": None
                    if mismatch_index is None or mismatch_index >= len(actual_order)
                    else actual_order[mismatch_index],
                },
                {
                    "event_count": len(expected_order),
                    "first_mismatch_index": None,
                    "event": None
                    if mismatch_index is None or mismatch_index >= len(expected_order)
                    else expected_order[mismatch_index],
                },
            ),
            _check("lockstep.arm-count", len(arm) == 1, len(arm), 1),
            _check(
                "lockstep.server-completion-count",
                len(server) == expected_action_count,
                len(server),
                expected_action_count,
            ),
            _check(
                "lockstep.client-observation-count",
                len(client) == expected_action_count + 1,
                len(client),
                expected_action_count + 1,
            ),
        )
    )
    if len(arm) == 1:
        checks.append(
            _check(
                "lockstep.arm-generation",
                arm[0].generation == 0,
                arm[0].generation,
                0,
            )
        )
    if len(arm) == 1 and len(server) == expected_action_count:
        previous_server = arm[0].server_world_time
        for expected_generation, event in enumerate(server, start=1):
            delta = (
                None
                if previous_server is None or event.server_world_time is None
                else event.server_world_time - previous_server
            )
            checks.append(
                _check(
                    f"lockstep.server-generation-{expected_generation}",
                    event.generation == expected_generation and delta == 1,
                    {"generation": event.generation, "world_time_delta": delta},
                    {"generation": expected_generation, "world_time_delta": 1},
                )
            )
            previous_server = event.server_world_time
    if len(client) == expected_action_count + 1 and len(run.samples) == TIMING_STEP_COUNT:
        client_values = [reset_world_time_ticks]
        client_values.extend(sample.world_time_ticks for sample in run.samples)
        last_sample_world_time = run.samples[-1].world_time_ticks
        client_values.append(
            None
            if last_sample_world_time is None
            else last_sample_world_time + 1
        )
        server_world_times = [arm[0].server_world_time] if len(arm) == 1 else []
        server_world_times.extend(event.server_world_time for event in server)
        for expected_generation, (event, expected_world_time) in enumerate(
            zip(client, client_values, strict=True)
        ):
            expected_server_world_time = (
                server_world_times[expected_generation]
                if len(server_world_times) == len(client_values)
                else None
            )
            checks.append(
                _check(
                    f"lockstep.client-generation-{expected_generation}",
                    event.generation == expected_generation
                    and event.client_world_time == expected_world_time
                    and event.server_world_time == expected_server_world_time,
                    {
                        "generation": event.generation,
                        "server_world_time": event.server_world_time,
                        "client_world_time": event.client_world_time,
                    },
                    {
                        "generation": expected_generation,
                        "server_world_time": expected_server_world_time,
                        "client_world_time": expected_world_time,
                    },
                )
            )
    return tuple(checks)


def _failed_check_signatures(
    report: TimingPairReportV0,
) -> frozenset[tuple[str, object]]:
    signatures: set[tuple[str, object]] = set()
    for check in report.checks:
        if check.passed:
            continue
        actual = check.actual
        expected = check.expected
        if (
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and isinstance(expected, (int, float))
            and not isinstance(expected, bool)
            and math.isfinite(float(actual))
            and math.isfinite(float(expected))
        ):
            delta = float(actual) - float(expected)
            direction = 0 if abs(delta) <= _FLOAT_EPSILON else (1 if delta > 0 else -1)
            signatures.add((check.name, ("numeric-direction", direction)))
        else:
            signatures.add(
                (check.name, ("value-transition", repr(actual), repr(expected)))
            )
    return frozenset(signatures)


def aggregate_timing_verdict(
    pair_reports: Sequence[TimingPairReportV0],
) -> TimingVerdictV0:
    grouped: dict[int, list[TimingPairReportV0]] = {}
    for report in pair_reports:
        grouped.setdefault(report.seed, []).append(report)
    if set(grouped) != set(TIMING_SEEDS):
        return TimingVerdictV0.INCONCLUSIVE
    inconclusive = False
    for seed in TIMING_SEEDS:
        reports = sorted(grouped[seed], key=lambda item: item.attempt)
        attempts = [item.attempt for item in reports]
        if attempts not in ([1], [1, 2]):
            return TimingVerdictV0.INCONCLUSIVE
        if (
            attempts == [1, 2]
            and reports[0].clean
            and reports[0].equivalent
        ):
            return TimingVerdictV0.INCONCLUSIVE
        mismatches = [item for item in reports if item.clean and not item.equivalent]
        if len(mismatches) >= 2:
            reproduced = set(_failed_check_signatures(mismatches[0]))
            for item in mismatches[1:]:
                reproduced.intersection_update(_failed_check_signatures(item))
            if reproduced:
                return TimingVerdictV0.NON_EQUIVALENT
            inconclusive = True
            continue
        if mismatches:
            inconclusive = True
            continue
        if not reports[-1].equivalent:
            inconclusive = True
    return (
        TimingVerdictV0.INCONCLUSIVE
        if inconclusive
        else TimingVerdictV0.EQUIVALENT
    )


def percentile(values: Iterable[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= quantile <= 1.0 or not math.isfinite(quantile):
        raise ValueError("quantile must be finite and between zero and one")
    if not all(math.isfinite(value) for value in ordered):
        raise ValueError("percentile values must be finite")
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


@dataclass(frozen=True, slots=True)
class ResourceSampleV0:
    monotonic_ns: int
    ram_used_percent: float
    cpu_used_percent: float
    gpu_monitor_available: bool
    vram_used_mib: float | None
    vram_total_mib: float | None
    gpu_used_percent: float | None
    worker_tree_rss_bytes: tuple[tuple[str, int], ...] = ()
    worker_tree_cpu_percent: tuple[tuple[str, float], ...] = ()
    schema_version: str = field(default="mc2p.resource-sample.v0", init=False)


@dataclass(frozen=True, slots=True)
class ProcessIdentityV0:
    pid: int
    create_time: float

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("process pid must be a positive integer")
        if not math.isfinite(self.create_time) or self.create_time <= 0:
            raise ValueError("process create_time must be finite and positive")


class ProcessIdentityError(RuntimeError):
    """Raised before mutation when a PID no longer identifies the same process."""


@dataclass(frozen=True, slots=True)
class ProcessTreeSnapshotV0:
    root: ProcessIdentityV0
    identities: tuple[ProcessIdentityV0, ...]


@dataclass(frozen=True, slots=True)
class ProcessTreeCleanupV0:
    attempted: tuple[ProcessIdentityV0, ...]
    stopped: tuple[ProcessIdentityV0, ...]
    surviving: tuple[ProcessIdentityV0, ...]
    errors: tuple[str, ...]


def _identity(process: psutil.Process) -> ProcessIdentityV0:
    return ProcessIdentityV0(process.pid, process.create_time())


def _same_process(process: psutil.Process, identity: ProcessIdentityV0) -> bool:
    try:
        return process.pid == identity.pid and abs(process.create_time() - identity.create_time) <= 0.01
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def capture_registered_tree(root: ProcessIdentityV0) -> ProcessTreeSnapshotV0:
    try:
        process = psutil.Process(root.pid)
        if not _same_process(process, root):
            raise ProcessIdentityError(
                f"pid {root.pid} creation time does not match the registered process"
            )
        descendants = process.children(recursive=True)
        identities = (_identity(process),) + tuple(
            sorted((_identity(item) for item in descendants), key=lambda item: item.pid)
        )
    except psutil.NoSuchProcess as error:
        raise ProcessIdentityError(f"registered root pid {root.pid} is not alive") from error
    except psutil.AccessDenied as error:
        raise ProcessIdentityError(f"cannot inspect registered root pid {root.pid}") from error
    return ProcessTreeSnapshotV0(root=root, identities=identities)


def terminate_registered_tree(
    root: ProcessIdentityV0,
    registered: Sequence[ProcessIdentityV0],
    *,
    grace_seconds: float = 5.0,
) -> ProcessTreeCleanupV0:
    if not math.isfinite(grace_seconds) or grace_seconds < 0:
        raise ValueError("grace_seconds must be finite and nonnegative")
    identities = tuple(registered)
    by_pid = {identity.pid: identity for identity in identities}
    if len(by_pid) != len(identities) or by_pid.get(root.pid) != root:
        raise ProcessIdentityError("registered tree does not contain the exact root identity")
    processes: dict[int, psutil.Process] = {}
    stopped: list[ProcessIdentityV0] = []
    for identity in identities:
        try:
            process = psutil.Process(identity.pid)
        except psutil.NoSuchProcess:
            stopped.append(identity)
            continue
        if not _same_process(process, identity):
            if identity == root:
                raise ProcessIdentityError(
                    f"root pid {identity.pid} creation time changed before cleanup"
                )
            stopped.append(identity)
            continue
        processes[identity.pid] = process

    errors: list[str] = []
    ordered = [
        processes[identity.pid]
        for identity in identities
        if identity.pid != root.pid and identity.pid in processes
    ]
    if root.pid in processes:
        ordered.append(processes[root.pid])
    for process in ordered:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
            if isinstance(error, psutil.AccessDenied):
                errors.append(f"terminate pid {process.pid}: access denied")
    gone, alive = psutil.wait_procs(ordered, timeout=grace_seconds)
    del gone
    for process in alive:
        identity = by_pid[process.pid]
        if not _same_process(process, identity):
            continue
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
            if isinstance(error, psutil.AccessDenied):
                errors.append(f"kill pid {process.pid}: access denied")
    if alive:
        psutil.wait_procs(alive, timeout=grace_seconds)

    surviving: list[ProcessIdentityV0] = []
    already_stopped = {identity.pid for identity in stopped}
    for identity in identities:
        if identity.pid in already_stopped:
            continue
        try:
            process = psutil.Process(identity.pid)
        except psutil.NoSuchProcess:
            stopped.append(identity)
            continue
        if _same_process(process, identity) and process.is_running():
            surviving.append(identity)
        else:
            stopped.append(identity)
    return ProcessTreeCleanupV0(
        attempted=identities,
        stopped=tuple(sorted(stopped, key=lambda item: item.pid)),
        surviving=tuple(sorted(surviving, key=lambda item: item.pid)),
        errors=tuple(errors),
    )


def _query_gpu(
    command_runner: Callable[..., Any],
) -> tuple[bool, float | None, float | None, float | None]:
    command = [
        "nvidia-smi",
        "--query-gpu=memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = command_runner(
            command,
            capture_output=True,
            text=True,
            timeout=GPU_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("nvidia-smi returned nonzero")
        rows: list[tuple[float, float, float]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 3:
                raise ValueError("nvidia-smi row does not have three columns")
            row = tuple(float(part) for part in parts)
            if not all(math.isfinite(value) for value in row):
                raise ValueError("nvidia-smi returned non-finite values")
            if row[0] < 0 or row[1] <= 0:
                raise ValueError("nvidia-smi memory values are invalid")
            rows.append(row)
        if not rows:
            raise ValueError("nvidia-smi returned no GPU rows")
        return (
            True,
            sum(row[0] for row in rows),
            sum(row[1] for row in rows),
            statistics.fmean(row[2] for row in rows),
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, AttributeError):
        return False, None, None, None


def query_host_resources(
    roots: Mapping[str, ProcessIdentityV0],
    *,
    command_runner: Callable[..., Any] = subprocess.run,
) -> ResourceSampleV0:
    memory = psutil.virtual_memory()
    worker_rss: list[tuple[str, int]] = []
    worker_cpu: list[tuple[str, float]] = []
    for name, root in sorted(roots.items()):
        try:
            snapshot = capture_registered_tree(root)
        except ProcessIdentityError:
            continue
        rss = 0
        cpu = 0.0
        for identity in snapshot.identities:
            try:
                process = psutil.Process(identity.pid)
                if not _same_process(process, identity):
                    continue
                rss += int(process.memory_info().rss)
                cpu += float(process.cpu_percent(interval=None))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        worker_rss.append((name, rss))
        worker_cpu.append((name, cpu))
    gpu_available, vram_used, vram_total, gpu_used = _query_gpu(command_runner)
    return ResourceSampleV0(
        monotonic_ns=time.perf_counter_ns(),
        ram_used_percent=float(memory.percent),
        cpu_used_percent=float(psutil.cpu_percent(interval=None)),
        gpu_monitor_available=gpu_available,
        vram_used_mib=vram_used,
        vram_total_mib=vram_total,
        gpu_used_percent=gpu_used,
        worker_tree_rss_bytes=tuple(worker_rss),
        worker_tree_cpu_percent=tuple(worker_cpu),
    )


@dataclass(frozen=True, slots=True)
class GuardDecisionV0:
    triggered: bool
    reasons: tuple[str, ...]


def evaluate_resource_guard(
    sample: ResourceSampleV0,
    *,
    semantic_failure: bool = False,
    cleanup_failure: bool = False,
    hang: bool = False,
    isolation_failure: bool = False,
) -> GuardDecisionV0:
    reasons: list[str] = []
    if sample.ram_used_percent >= RAM_GUARD_PERCENT:
        reasons.append("ram-85-percent")
    if not sample.gpu_monitor_available:
        reasons.append("gpu-monitor-unavailable")
    elif sample.vram_used_mib is None or sample.vram_total_mib is None or sample.vram_total_mib <= 0:
        reasons.append("gpu-monitor-unavailable")
    elif 100.0 * sample.vram_used_mib / sample.vram_total_mib >= VRAM_GUARD_PERCENT:
        reasons.append("vram-90-percent")
    if semantic_failure:
        reasons.append("semantic-failure")
    if cleanup_failure:
        reasons.append("cleanup-failure")
    if hang:
        reasons.append("hang")
    if isolation_failure:
        reasons.append("isolation-failure")
    return GuardDecisionV0(bool(reasons), tuple(reasons))


@dataclass(frozen=True, slots=True)
class CapacityBatchResultV0:
    parallelism: int
    batch_index: int
    worker_measured_steps: tuple[int, ...]
    batch_elapsed_seconds: float
    step_latencies_seconds: tuple[float, ...]
    worker_reset_seconds: tuple[float, ...] = ()
    worker_measured_elapsed_seconds: tuple[float, ...] = ()
    worker_steps_per_second: tuple[float, ...] = ()
    resource_summary: Mapping[str, object] = field(default_factory=dict)
    semantic_failures: tuple[str, ...] = ()
    cleanup_failures: tuple[str, ...] = ()
    guard_reasons: tuple[str, ...] = ()
    schema_version: str = field(default="mc2p.capacity-batch.v0", init=False)

    @property
    def aggregate_steps_per_second(self) -> float:
        if self.batch_elapsed_seconds <= 0 or not math.isfinite(self.batch_elapsed_seconds):
            return 0.0
        return sum(self.worker_measured_steps) / self.batch_elapsed_seconds

    @property
    def passed(self) -> bool:
        return (
            self.parallelism in {1, 2, 4}
            and len(self.worker_measured_steps) == self.parallelism
            and all(value == CAPACITY_STEP_COUNT for value in self.worker_measured_steps)
            and self.batch_elapsed_seconds > 0
            and math.isfinite(self.batch_elapsed_seconds)
            and bool(self.step_latencies_seconds)
            and len(self.step_latencies_seconds)
            == self.parallelism * CAPACITY_STEP_COUNT
            and all(
                value >= 0 and math.isfinite(value)
                for value in self.step_latencies_seconds
            )
            and len(self.worker_reset_seconds) == self.parallelism
            and all(
                value >= 0 and math.isfinite(value)
                for value in self.worker_reset_seconds
            )
            and len(self.worker_measured_elapsed_seconds) == self.parallelism
            and all(
                value > 0 and math.isfinite(value)
                for value in self.worker_measured_elapsed_seconds
            )
            and len(self.worker_steps_per_second) == self.parallelism
            and all(
                value > 0 and math.isfinite(value)
                for value in self.worker_steps_per_second
            )
            and type(self.resource_summary.get("sample_count")) is int
            and int(self.resource_summary["sample_count"]) > 0
            and not self.semantic_failures
            and not self.cleanup_failures
            and not self.guard_reasons
        )


@dataclass(frozen=True, slots=True)
class CapacitySummaryV0:
    safe_capacity: int
    recommended_capacity: int
    efficiency_by_parallelism: Mapping[int, float]
    latency_percentiles_by_parallelism: Mapping[int, Mapping[str, float]]
    schema_version: str = field(default="mc2p.capacity-summary.v0", init=False)


def _complete_batches(
    grouped: Mapping[int, list[CapacityBatchResultV0]],
    parallelism: int,
) -> list[CapacityBatchResultV0] | None:
    batches = grouped.get(parallelism, [])
    if (
        len(batches) != 3
        or {batch.batch_index for batch in batches} != {0, 1, 2}
        or not all(batch.passed for batch in batches)
    ):
        return None
    return batches


def summarize_capacity(
    batches: Sequence[CapacityBatchResultV0],
    *,
    fault_isolation_passed: bool,
) -> CapacitySummaryV0:
    grouped: dict[int, list[CapacityBatchResultV0]] = {}
    for batch in batches:
        grouped.setdefault(batch.parallelism, []).append(batch)
    accepted: list[int] = []
    n1 = _complete_batches(grouped, 1)
    if n1 is not None:
        accepted.append(1)
    n2 = _complete_batches(grouped, 2)
    if n1 is not None and n2 is not None and fault_isolation_passed:
        accepted.append(2)
    n4 = _complete_batches(grouped, 4)
    if (
        n1 is not None
        and n2 is not None
        and fault_isolation_passed
        and n4 is not None
    ):
        accepted.append(4)
    safe_capacity = max(accepted, default=0)

    efficiency: dict[int, float] = {}
    latency: dict[int, Mapping[str, float]] = {}
    if n1 is not None:
        baseline = statistics.median(
            batch.aggregate_steps_per_second for batch in n1
        )
        for parallelism in accepted:
            accepted_batches = _complete_batches(grouped, parallelism)
            assert accepted_batches is not None
            aggregate = statistics.median(
                batch.aggregate_steps_per_second for batch in accepted_batches
            )
            efficiency[parallelism] = aggregate / (parallelism * baseline)
            values = tuple(
                value
                for batch in accepted_batches
                for value in batch.step_latencies_seconds
            )
            latency[parallelism] = {
                "p50": percentile(values, 0.50),
                "p95": percentile(values, 0.95),
                "p99": percentile(values, 0.99),
                "max": max(values),
            }
    recommended = max(
        (
            parallelism
            for parallelism in accepted
            if efficiency.get(parallelism, 0.0) >= RECOMMENDED_EFFICIENCY
        ),
        default=0,
    )
    return CapacitySummaryV0(
        safe_capacity=safe_capacity,
        recommended_capacity=recommended,
        efficiency_by_parallelism=efficiency,
        latency_percentiles_by_parallelism=latency,
    )
