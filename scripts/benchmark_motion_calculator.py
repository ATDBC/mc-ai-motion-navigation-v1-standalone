"""Reproducible CPU baseline for B09-R single-branch and batch rollouts."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_rollout import RolloutOptions, RolloutOutputMode, rollout
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState, TickInput
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)


def _fixture():
    session = WorldSessionId("b09r-benchmark")
    stamp = ObservationStamp(session, 0, 0, "benchmark", 0)
    state = PhysicsState(
        ruleset_id=JAVA_1_21_RULESET.ruleset_id,
        state_schema=JAVA_1_21_RULESET.state_schema, session=session,
        movement_tick_id=0, position=(.5, 64., .5),
        velocity_blocks_per_tick=(0., -.0784000015258789, 0.),
        yaw_radians=0., pitch_radians=0., pose="standing",
        body_width=.6, body_height=1.8, on_ground=True,
        horizontal_collision=False, vertical_collision=True,
        sprinting=False, sneaking=False, jumping_cooldown_ticks=0,
        fall_distance_blocks=0., movement_speed_attribute=.1,
        step_height_blocks=.6, gravity_attribute=.08,
        jump_strength_attribute=.42, food_points=20, saturation_points=5.,
        game_mode="survival", status_effects=(), swimming=False,
        submerged_in_water=False, climbing=False, fall_flying=False,
        flying=False, allow_flying=False,
    )
    knowledge = WorldKnowledge(session)
    cells = tuple((x, y, z) for x in range(-24, 25) for y in range(61, 69)
                  for z in range(-24, 25))
    knowledge.confirm_air(stamp, cells)
    knowledge.observe_blocks(stamp, {
        (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
        for x in range(-24, 25) for z in range(-24, 25)
    })
    return state, PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET)


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _run_case(initial, world, inputs, branches: int):
    for _ in range(branches):
        result = rollout(
            initial, inputs, world, JAVA_1_21_RULESET,
            RolloutOptions(output_mode=RolloutOutputMode.SUMMARY),
        )
        if result.ticks_completed != len(inputs):
            raise RuntimeError(
                f"benchmark stopped at {result.ticks_completed}/{len(inputs)}"
            )


def run_benchmark(*, repetitions: int = 30, warmups: int = 5) -> dict:
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be positive")
    if type(warmups) is not int or warmups < 0:
        raise ValueError("warmups must be nonnegative")
    if tracemalloc.is_tracing():
        raise RuntimeError("timing benchmark cannot run under tracemalloc")
    initial, world = _fixture()
    results = []
    cases = tuple((ticks, 1) for ticks in (1, 6, 20, 60)) + (
        (20, 32), (20, 128),
    )
    for ticks, branches in cases:
        inputs = tuple(TickInput(1, 0, False, False, False, 0) for _ in range(ticks))
        for _ in range(warmups):
            _run_case(initial, world, inputs, branches)
        timings = []
        samples = repetitions if branches == 1 else max(3, repetitions // 5)
        for _ in range(samples):
            started = time.perf_counter_ns()
            _run_case(initial, world, inputs, branches)
            timings.append((time.perf_counter_ns() - started) / 1_000_000)
        results.append({
            "ticks": ticks, "branches": branches,
            "p50_ms": statistics.median(timings),
            "p95_ms": _percentile(timings, .95),
            "p99_ms": _percentile(timings, .99), "max_ms": max(timings),
            "effective_ticks_per_second": (
                branches * ticks / (statistics.median(timings) / 1000)
            ),
        })

    # Memory is deliberately measured in a separate pass.  tracemalloc adds
    # substantial allocation overhead and must never surround the timing loop.
    tracemalloc.start()
    for ticks, branches in cases:
        inputs = tuple(TickInput(1, 0, False, False, False, 0) for _ in range(ticks))
        _run_case(initial, world, inputs, branches)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "schema_version": "mc2p.physics-benchmark.v1",
        "ruleset_id": JAVA_1_21_RULESET.ruleset_id,
        "clock": "perf_counter_ns", "repetitions": repetitions,
        "warmups": warmups,
        "timing_under_tracemalloc": False,
        "python_version": platform.python_version(),
        "cpu": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
        "peak_traced_bytes": peak, "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.warmups < 0:
        parser.error("--warmups must be nonnegative")
    payload = run_benchmark(repetitions=args.repetitions, warmups=args.warmups)
    encoded = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
