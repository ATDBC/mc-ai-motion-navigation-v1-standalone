"""Calibrate B02 ordinary-ground prediction from two independent runtime traces."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mc2p.motion_nav.evidence.ground_calibration import (
    GroundMotionSample, calibrate_ground_motion,
)
from mc2p.motion_nav.ground_motion import (
    GroundControl, GroundMotionProfile, PlanarBodyState, predict_ground,
)


def _read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _body(value: dict[str, Any]) -> PlanarBodyState:
    position, velocity = value["position"], value["velocity"]
    return PlanarBodyState(
        x=float(position["x"]),
        z=float(position["z"]),
        velocity_x=float(velocity["x"]) * 20.0,
        velocity_z=float(velocity["z"]) * 20.0,
        yaw_radians=math.radians(float(value["yaw_degrees"])),
    )


def samples_from_trace(path: Path) -> tuple[tuple[GroundMotionSample, ...], dict[str, int]]:
    previous: dict[str, Any] | None = None
    samples: list[GroundMotionSample] = []
    excluded = {"camera_turn": 0, "unsupported_state": 0}
    for record in _read_records(path):
        if record.get("record_type") == "reset":
            previous = record["payload"]["result"]["observation"]["self_state"]["value"]
            continue
        if record.get("record_type") != "step":
            continue
        payload = record["payload"]
        after = payload["backend_result"]["observation"]["self_state"]["value"]
        movement = payload["decision"]["action"]["movement"]
        if previous is None:
            raise ValueError("step appeared before reset observation")
        yaw_change = (float(after["yaw_degrees"]) - float(previous["yaw_degrees"]) + 180.0) % 360.0 - 180.0
        ordinary_ground = (
            previous["is_on_ground"] and after["is_on_ground"]
            and previous["pose"] == "standing" and after["pose"] == "standing"
            and not previous["horizontal_collision"] and not after["horizontal_collision"]
            and not movement["jump"] and not movement["sneak"] and not movement["sprint"]
        )
        if abs(yaw_change) > 1.0e-6:
            excluded["camera_turn"] += 1
        elif not ordinary_ground:
            excluded["unsupported_state"] += 1
        else:
            samples.append(GroundMotionSample(
                before=_body(previous),
                # Minecraft's positive strafe axis means left; GroundControl's
                # positive strafe axis means right.
                control=GroundControl(
                    float(movement["forward"]),
                    -float(movement["strafe"]),
                    math.radians(float(previous["yaw_degrees"])),
                ),
                after=_body(after),
            ))
        previous = after
    if not samples:
        raise ValueError(f"no ordinary-ground samples in {path}")
    return tuple(samples), excluded


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def rollout_error_summary(samples: tuple[GroundMotionSample, ...],
                          profile: GroundMotionProfile, *, horizon: int = 6) -> dict[str, float | int]:
    """Measure independent multi-step rollouts without bridging excluded trace gaps."""
    if (type(samples) is not tuple or any(type(sample) is not GroundMotionSample for sample in samples)
            or type(profile) is not GroundMotionProfile):
        raise ValueError("rollout validation requires typed samples and profile")
    if type(horizon) is not int or horizon < 2:
        raise ValueError("rollout horizon must be at least two steps")
    position_errors: list[float] = []
    velocity_errors: list[float] = []
    for start in range(len(samples) - horizon + 1):
        window = samples[start:start + horizon]
        if any(previous.after != following.before
               for previous, following in zip(window, window[1:])):
            continue
        predicted = predict_ground(
            window[0].before, tuple(sample.control for sample in window), profile,
        )[-1]
        actual = window[-1].after
        position_errors.append(math.hypot(predicted.x - actual.x, predicted.z - actual.z))
        velocity_errors.append(math.hypot(
            predicted.velocity_x - actual.velocity_x,
            predicted.velocity_z - actual.velocity_z,
        ))
    if not position_errors:
        raise ValueError(f"no contiguous {horizon}-step validation windows")
    return {
        "horizon_steps": horizon,
        "sample_count": len(position_errors),
        "position_p99_blocks": _percentile(position_errors, .99),
        "position_max_blocks": max(position_errors),
        "velocity_p99_blocks_per_second": _percentile(velocity_errors, .99),
        "velocity_max_blocks_per_second": max(velocity_errors),
    }


def input_coverage(samples: tuple[GroundMotionSample, ...]) -> dict[str, int]:
    counts = {"forward_only": 0, "strafe_only": 0, "diagonal": 0, "release": 0}
    for sample in samples:
        forward = abs(sample.control.forward) > 1.0e-9
        strafe = abs(sample.control.strafe) > 1.0e-9
        key = ("diagonal" if forward and strafe else "forward_only" if forward
               else "strafe_only" if strafe else "release")
        counts[key] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibration-trace", type=Path, required=True)
    parser.add_argument("--validation-trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    calibration_samples, calibration_excluded = samples_from_trace(args.calibration_trace)
    validation_samples, validation_excluded = samples_from_trace(args.validation_trace)
    result = calibrate_ground_motion(
        calibration_samples,
        tick_seconds=0.05,
        validation_samples=validation_samples,
    )
    rollout = rollout_error_summary(validation_samples, result.profile, horizon=6)
    calibration_coverage = input_coverage(calibration_samples)
    validation_coverage = input_coverage(validation_samples)
    if not all(calibration_coverage.values()) or not all(validation_coverage.values()):
        raise ValueError("ordinary-ground calibration lacks a required input category")
    calibrated_profile = asdict(result.profile)
    calibrated_profile.pop("support_materials")
    payload = {
        "schema_version": "mc2p.motion-nav-ground-calibration.v1",
        "calibration_trace": str(args.calibration_trace.resolve()),
        "validation_trace": str(args.validation_trace.resolve()),
        "profile": calibrated_profile,
        "calibration_sample_count": result.calibration_sample_count,
        "validation_sample_count": result.validation_sample_count,
        "calibration_excluded": calibration_excluded,
        "validation_excluded": validation_excluded,
        "calibration_input_coverage": calibration_coverage,
        "validation_input_coverage": validation_coverage,
        "error": {
            "position_p95_blocks": result.position_error_p95_blocks,
            "position_max_blocks": result.position_error_max_blocks,
            "velocity_p95_blocks_per_second": result.velocity_error_p95_blocks_per_second,
            "velocity_max_blocks_per_second": result.velocity_error_max_blocks_per_second,
        },
        "six_step_validation": rollout,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
