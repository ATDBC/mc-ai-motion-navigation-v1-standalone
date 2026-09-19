"""Calibration and explicit error bounds for the ordinary-ground predictor."""
from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import median

from mc2p.motion_nav.ground_motion import (
    GroundControl, GroundMotionProfile, PlanarBodyState, control_world_direction,
    predict_ground,
)


_EPSILON = 1.0e-9


@dataclass(frozen=True, slots=True)
class GroundMotionSample:
    before: PlanarBodyState
    control: GroundControl
    after: PlanarBodyState

    def __post_init__(self) -> None:
        if (type(self.before) is not PlanarBodyState or type(self.control) is not GroundControl
                or type(self.after) is not PlanarBodyState):
            raise ValueError("ground calibration sample requires typed values")


@dataclass(frozen=True, slots=True)
class GroundMotionCalibration:
    profile: GroundMotionProfile
    calibration_sample_count: int
    validation_sample_count: int
    position_error_p95_blocks: float
    position_error_max_blocks: float
    velocity_error_p95_blocks_per_second: float
    velocity_error_max_blocks_per_second: float


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _retention(samples: tuple[GroundMotionSample, ...]) -> float:
    ratios = []
    for sample in samples:
        if abs(sample.control.forward) > _EPSILON or abs(sample.control.strafe) > _EPSILON:
            continue
        for before, after in ((sample.before.velocity_x, sample.after.velocity_x),
                              (sample.before.velocity_z, sample.after.velocity_z)):
            if abs(before) > 1.0e-6 and before * after >= 0:
                ratios.append(after / before)
    if not ratios:
        raise ValueError("ground calibration requires release samples with nonzero velocity")
    value = median(ratios)
    if not 0 < value <= 1:
        raise ValueError("ground calibration produced invalid velocity retention")
    return value


def _profile(samples: tuple[GroundMotionSample, ...], tick_seconds: float) -> GroundMotionProfile:
    retention = _retention(samples)
    accelerations: list[float] = []
    pre_drag_speeds: list[float] = []
    for sample in samples:
        pre_x = sample.after.velocity_x / retention
        pre_z = sample.after.velocity_z / retention
        pre_drag_speeds.append(math.hypot(pre_x, pre_z))
        direction_x, direction_z = control_world_direction(sample.control)
        magnitude = math.hypot(direction_x, direction_z)
        if magnitude <= _EPSILON:
            continue
        direction_x /= magnitude
        direction_z /= magnitude
        acceleration = ((pre_x - sample.before.velocity_x) * direction_x
                        + (pre_z - sample.before.velocity_z) * direction_z) / tick_seconds
        if acceleration > _EPSILON:
            accelerations.append(acceleration)
    if not accelerations:
        raise ValueError("ground calibration requires powered movement samples")
    # Capped samples can only reduce the apparent acceleration. At least one
    # uncapped start sample is required, so the largest candidate is the fit.
    acceleration = max(accelerations)
    maximum_speed = max(pre_drag_speeds)
    if maximum_speed <= _EPSILON:
        raise ValueError("ground calibration did not observe movement")
    return GroundMotionProfile(tick_seconds, acceleration, retention, maximum_speed)


def calibrate_ground_motion(
        calibration_samples: tuple[GroundMotionSample, ...], *, tick_seconds: float,
        validation_samples: tuple[GroundMotionSample, ...] | None = None) -> GroundMotionCalibration:
    if type(calibration_samples) is not tuple or not calibration_samples:
        raise ValueError("ground calibration requires immutable samples")
    if any(type(sample) is not GroundMotionSample for sample in calibration_samples):
        raise ValueError("ground calibration contains an invalid sample")
    if type(tick_seconds) not in (int, float) or not math.isfinite(tick_seconds) or tick_seconds <= 0:
        raise ValueError("tick seconds must be finite and positive")
    validation = calibration_samples if validation_samples is None else validation_samples
    if (type(validation) is not tuple or not validation
            or any(type(sample) is not GroundMotionSample for sample in validation)):
        raise ValueError("ground validation requires immutable samples")
    profile = _profile(calibration_samples, float(tick_seconds))
    position_errors, velocity_errors = [], []
    for sample in validation:
        predicted = predict_ground(sample.before, (sample.control,), profile)[-1]
        position_errors.append(math.hypot(predicted.x - sample.after.x,
                                          predicted.z - sample.after.z))
        velocity_errors.append(math.hypot(predicted.velocity_x - sample.after.velocity_x,
                                          predicted.velocity_z - sample.after.velocity_z))
    return GroundMotionCalibration(
        profile=profile,
        calibration_sample_count=len(calibration_samples),
        validation_sample_count=len(validation),
        position_error_p95_blocks=_percentile(position_errors, 0.95),
        position_error_max_blocks=max(position_errors),
        velocity_error_p95_blocks_per_second=_percentile(velocity_errors, 0.95),
        velocity_error_max_blocks_per_second=max(velocity_errors),
    )
