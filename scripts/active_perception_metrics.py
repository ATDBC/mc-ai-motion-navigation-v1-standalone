"""Bounded numerical quality metrics from actual observation samples.

This module deliberately has no artifact parser and no scenario knowledge.  Its
input is already-authenticated post-observation data.  Controller-clock elapsed
time is used for phase coverage and temporal integration; client-clock elapsed
time is used only for pose derivatives.  The two clock domains are never
subtracted from one another.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from mc2p.contracts.observation import Vec3V0


MAX_SAMPLES = 40_000
MAX_PHASE_NS = 900_000_000_000
MAX_INTERVAL_NS = 250_000_000
MIN_COVERAGE = 0.95
MIN_HORIZONTAL_DISPLACEMENT = 0.01
LOW_HEAD_PITCH_DEGREES = 25.0


def _finite_number(value: object, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _nonnegative_int(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative int")


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a nonempty identifier")


@dataclass(frozen=True, slots=True)
class QualitySample:
    """One authenticated actual post-observation quality sample.

    ``incoming_forward`` is the actual action associated with the transition
    ending at this sample.  It is ``None`` when no step can be associated.
    Requested look angles are intentionally absent.
    """

    episode_id: str
    sequence_id: int
    controller_clock_id: str
    client_clock_id: str
    controller_ns: int
    client_ns: int
    position: Vec3V0
    yaw: float
    pitch: float
    incoming_forward: int | None
    target_visible: bool | None
    target_distance: float | None
    task_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.episode_id, "episode_id")
        _identifier(self.controller_clock_id, "controller_clock_id")
        _identifier(self.client_clock_id, "client_clock_id")
        _nonnegative_int(self.sequence_id, "sequence_id")
        _nonnegative_int(self.controller_ns, "controller_ns")
        _nonnegative_int(self.client_ns, "client_ns")
        if type(self.position) is not Vec3V0:
            raise ValueError("position must be Vec3V0")
        _finite_number(self.yaw, "yaw")
        pitch = _finite_number(self.pitch, "pitch")
        if not -90.0 <= pitch <= 90.0:
            raise ValueError("pitch must be within [-90, 90]")
        if self.incoming_forward is not None and (
            type(self.incoming_forward) is not int or self.incoming_forward not in (-1, 0, 1)
        ):
            raise ValueError("incoming_forward must be -1, 0, 1, or None")
        if self.target_visible is not None and type(self.target_visible) is not bool:
            raise ValueError("target_visible must be bool or None")
        if self.target_distance is not None:
            distance = _finite_number(self.target_distance, "target_distance")
            if distance < 0:
                raise ValueError("target_distance must be nonnegative")
        if self.task_id is not None:
            _identifier(self.task_id, "task_id")


def angular_velocity(before: float, after: float, dt_ns: int) -> float:
    """Return wrapped yaw velocity in degrees/second for an actual interval."""

    if type(dt_ns) is not int or dt_ns <= 0:
        raise ValueError("invalid angular samples")
    if type(before) not in (int, float) or type(after) not in (int, float):
        raise ValueError("invalid angular samples")
    if not math.isfinite(before) or not math.isfinite(after):
        raise ValueError("invalid angular samples")
    return ((after - before + 180.0) % 360.0 - 180.0) * 1_000_000_000.0 / dt_ns


def _linear_angular_velocity(before: float, after: float, dt_ns: int) -> float:
    return (after - before) * 1_000_000_000.0 / dt_ns


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _histogram() -> dict[str, float]:
    return {f"{lower}:{lower + 5}": 0.0 for lower in range(-90, 90, 5)}


def _pitch_bin(pitch: float) -> str:
    lower = math.floor(pitch / 5.0) * 5
    lower = min(85, max(-90, lower))
    return f"{lower}:{lower + 5}"


def _empty_result(
    sample_count: int,
    *,
    input_sample_count: int,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "valid_coverage_fraction": None,
        "valid_seconds": None,
        "missing_seconds": None,
        "moving_seconds": None,
        "low_head_fraction": None,
        "pitch_histogram_seconds": _histogram(),
        "yaw_velocity_abs_p95": None,
        "pitch_velocity_abs_p95": None,
        "yaw_acceleration_abs_p95": None,
        "pitch_acceleration_abs_p95": None,
        "angular_acceleration_abs_p95": None,
        "target_unseen_seconds": None,
        "target_unseen_seconds_lower": None,
        "target_unseen_seconds_upper": None,
        "target_unknown_seconds": None,
        "target_loss_duration_seconds_lower": None,
        "target_loss_duration_seconds_upper": None,
        "target_loss_count": 0,
        "target_reacquisition_count": 0,
        "stop_starts": 0,
        "yaw_reversals": 0,
        "pitch_reversals": 0,
        "reversals": 0,
        "sample_count": sample_count,
        "input_sample_count": input_sample_count,
        "reason": reason,
        "provenance": (
            "actual authenticated post-observation pose/action only; controller time for "
            "durations; JVM client-sample completion time for derivatives; no requested angles"
        ),
        "angular_acceleration_definition": (
            "linear-interpolation p95 over the pooled absolute yaw and pitch acceleration "
            "components; component p95 values are also reported separately"
        ),
        "pitch_histogram_bin_definition": (
            "5-degree [lower,upper) bins from -90 through 90; +90 is included in 85:90"
        ),
    }


def _continuous_pair(before: QualitySample, after: QualitySample) -> bool:
    if before.episode_id != after.episode_id:
        return False
    if before.controller_clock_id != after.controller_clock_id:
        return False
    if before.client_clock_id != after.client_clock_id:
        return False
    if after.sequence_id != before.sequence_id + 1:
        return False
    controller_dt = after.controller_ns - before.controller_ns
    client_dt = after.client_ns - before.client_ns
    return (
        0 < controller_dt <= MAX_INTERVAL_NS
        and 0 < client_dt <= MAX_INTERVAL_NS
    )


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def summarize_phase(
    samples: tuple[QualitySample, ...],
    *,
    start_ns: int,
    end_ns: int,
) -> dict[str, Any]:
    """Summarize one fixed controller-clock quality phase.

    A valid interval has consecutive sequence numbers, unchanged episode and
    clock identities, and separately positive intervals no longer than 250 ms
    in both clocks.  Invalid intervals contribute no derivatives or temporal
    interpolation.  Acceleration begins only with the fourth sample of a
    continuous run and is computed between velocity midpoint timestamps.

    ``status='passed'`` means the numerical phase is evaluable; comparative
    quality gates are intentionally outside this pure metric module.
    """

    if type(samples) is not tuple or any(type(item) is not QualitySample for item in samples):
        raise ValueError("samples must be a tuple of QualitySample")
    _nonnegative_int(start_ns, "start_ns")
    _nonnegative_int(end_ns, "end_ns")
    if end_ns <= start_ns:
        raise ValueError("end_ns must be greater than start_ns")

    input_sample_count = len(samples)
    sample_count = sum(start_ns <= item.controller_ns <= end_ns for item in samples)
    duration_ns = end_ns - start_ns
    if input_sample_count > MAX_SAMPLES:
        return _empty_result(
            sample_count,
            input_sample_count=input_sample_count,
            status="failed",
            reason=f"sample capacity exceeded: {input_sample_count} > {MAX_SAMPLES}",
        )
    if duration_ns > MAX_PHASE_NS:
        return _empty_result(
            sample_count,
            input_sample_count=input_sample_count,
            status="failed",
            reason="phase duration exceeds 900 seconds",
        )

    valid_ns = 0
    moving_ns = 0
    low_head_ns = 0
    histogram = _histogram()
    target_unseen_ns = 0
    target_known_ns = 0
    yaw_velocities: list[float] = []
    pitch_velocities: list[float] = []
    yaw_accelerations: list[float] = []
    pitch_accelerations: list[float] = []
    yaw_reversals = 0
    pitch_reversals = 0
    target_loss_count = 0
    target_reacquisition_count = 0
    stop_starts = 0
    order_broken = False

    continuous_samples = 1
    previous_yaw_velocity: tuple[int, float] | None = None
    previous_pitch_velocity: tuple[int, float] | None = None
    last_yaw_sign = 0
    last_pitch_sign = 0
    last_movement_state: bool | None = None

    for before, after in zip(samples, samples[1:]):
        if after.controller_ns < before.controller_ns:
            order_broken = True
        valid = not order_broken and _continuous_pair(before, after)
        if not valid:
            continuous_samples = 1
            previous_yaw_velocity = None
            previous_pitch_velocity = None
            last_yaw_sign = 0
            last_pitch_sign = 0
            last_movement_state = None
            continue

        controller_dt = after.controller_ns - before.controller_ns
        client_dt = after.client_ns - before.client_ns
        overlap_start = max(start_ns, before.controller_ns)
        overlap_end = min(end_ns, after.controller_ns)
        overlap_ns = max(0, overlap_end - overlap_start)
        in_phase = overlap_ns > 0
        if in_phase:
            valid_ns += overlap_ns
            seconds = overlap_ns / 1_000_000_000.0
            histogram[_pitch_bin(float(before.pitch))] += seconds

        dx = after.position.x - before.position.x
        dz = after.position.z - before.position.z
        displaced = math.hypot(dx, dz) > MIN_HORIZONTAL_DISPLACEMENT
        if in_phase and after.incoming_forward is not None:
            movement_state = displaced
            if last_movement_state is not None and movement_state != last_movement_state:
                stop_starts += 1
            last_movement_state = movement_state
        elif in_phase:
            last_movement_state = None
        if in_phase and after.incoming_forward == 1 and displaced:
            moving_ns += overlap_ns
            if before.pitch > LOW_HEAD_PITCH_DEGREES:
                low_head_ns += overlap_ns

        if in_phase and before.target_visible is not None:
            target_known_ns += overlap_ns
            if before.target_visible is False:
                target_unseen_ns += overlap_ns

        derivative_pair = (
            in_phase
            and start_ns <= before.controller_ns <= end_ns
            and start_ns <= after.controller_ns <= end_ns
        )
        if not derivative_pair:
            continuous_samples = 1
            previous_yaw_velocity = None
            previous_pitch_velocity = None
            last_yaw_sign = 0
            last_pitch_sign = 0
            continue

        if before.target_visible is True and after.target_visible is False:
            target_loss_count += 1
        elif before.target_visible is False and after.target_visible is True:
            target_reacquisition_count += 1

        yaw_velocity = angular_velocity(before.yaw, after.yaw, client_dt)
        pitch_velocity = _linear_angular_velocity(before.pitch, after.pitch, client_dt)
        yaw_velocities.append(yaw_velocity)
        pitch_velocities.append(pitch_velocity)
        yaw_sign = _sign(yaw_velocity)
        pitch_sign = _sign(pitch_velocity)
        if yaw_sign:
            if last_yaw_sign and yaw_sign != last_yaw_sign:
                yaw_reversals += 1
            last_yaw_sign = yaw_sign
        if pitch_sign:
            if last_pitch_sign and pitch_sign != last_pitch_sign:
                pitch_reversals += 1
            last_pitch_sign = pitch_sign

        continuous_samples += 1
        midpoint_twice_ns = before.client_ns + after.client_ns
        if continuous_samples >= 4:
            if previous_yaw_velocity is not None and previous_pitch_velocity is not None:
                yaw_midpoint_twice, prior_yaw = previous_yaw_velocity
                pitch_midpoint_twice, prior_pitch = previous_pitch_velocity
                yaw_dt_twice = midpoint_twice_ns - yaw_midpoint_twice
                pitch_dt_twice = midpoint_twice_ns - pitch_midpoint_twice
                if yaw_dt_twice > 0 and pitch_dt_twice > 0:
                    yaw_accelerations.append(
                        (yaw_velocity - prior_yaw) * 2_000_000_000.0 / yaw_dt_twice
                    )
                    pitch_accelerations.append(
                        (pitch_velocity - prior_pitch) * 2_000_000_000.0 / pitch_dt_twice
                    )
        previous_yaw_velocity = (midpoint_twice_ns, yaw_velocity)
        previous_pitch_velocity = (midpoint_twice_ns, pitch_velocity)

    missing_ns = max(0, duration_ns - valid_ns)
    coverage = valid_ns / duration_ns
    target_unknown_ns = missing_ns + max(0, valid_ns - target_known_ns)
    target_trusted = target_known_ns * 100 >= duration_ns * 95
    yaw_velocity_abs = [abs(value) for value in yaw_velocities]
    pitch_velocity_abs = [abs(value) for value in pitch_velocities]
    yaw_abs = [abs(value) for value in yaw_accelerations]
    pitch_abs = [abs(value) for value in pitch_accelerations]

    reasons: list[str] = []
    if valid_ns * 100 < duration_ns * 95:
        reasons.append("valid coverage below 0.95")
    if moving_ns == 0:
        reasons.append("no associated actual forward movement")
    if not yaw_accelerations or not pitch_accelerations:
        reasons.append("fewer than four continuous actual pose samples for acceleration")
    if not target_trusted:
        reasons.append("target visibility coverage below 0.95")
    if order_broken:
        reasons.append("controller sample order is not monotonic")

    result = _empty_result(
        sample_count,
        input_sample_count=input_sample_count,
        status="inconclusive" if reasons else "passed",
        reason="; ".join(reasons) if reasons else "phase metrics evaluable; comparative gate not applied",
    )
    result.update(
        valid_coverage_fraction=coverage,
        valid_seconds=valid_ns / 1_000_000_000.0,
        missing_seconds=missing_ns / 1_000_000_000.0,
        moving_seconds=moving_ns / 1_000_000_000.0,
        low_head_fraction=(low_head_ns / moving_ns if moving_ns else None),
        pitch_histogram_seconds=histogram,
        yaw_velocity_abs_p95=_percentile(yaw_velocity_abs, 0.95),
        pitch_velocity_abs_p95=_percentile(pitch_velocity_abs, 0.95),
        yaw_acceleration_abs_p95=_percentile(yaw_abs, 0.95),
        pitch_acceleration_abs_p95=_percentile(pitch_abs, 0.95),
        angular_acceleration_abs_p95=_percentile(yaw_abs + pitch_abs, 0.95),
        target_unseen_seconds=(
            target_unseen_ns / 1_000_000_000.0 if target_trusted else None
        ),
        target_unseen_seconds_lower=target_unseen_ns / 1_000_000_000.0,
        target_unseen_seconds_upper=(target_unseen_ns + target_unknown_ns) / 1_000_000_000.0,
        target_unknown_seconds=target_unknown_ns / 1_000_000_000.0,
        target_loss_duration_seconds_lower=target_unseen_ns / 1_000_000_000.0,
        target_loss_duration_seconds_upper=(
            target_unseen_ns + target_unknown_ns
        ) / 1_000_000_000.0,
        target_loss_count=target_loss_count,
        target_reacquisition_count=target_reacquisition_count,
        stop_starts=stop_starts,
        yaw_reversals=yaw_reversals,
        pitch_reversals=pitch_reversals,
        reversals=yaw_reversals + pitch_reversals,
    )
    return result


__all__ = ["QualitySample", "angular_velocity", "summarize_phase"]
