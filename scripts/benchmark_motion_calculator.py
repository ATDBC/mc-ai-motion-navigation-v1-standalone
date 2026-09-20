"""Reproducible CPU baseline for B09-R single-branch and batch rollouts."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    initial, world = _fixture()
    results = []
    tracemalloc.start()
    for ticks in (1, 6, 20, 60):
        inputs = tuple(TickInput(1, 0, False, False, False, 0) for _ in range(ticks))
        timings = []
        for _ in range(args.repetitions):
            started = time.perf_counter_ns()
            result = rollout(initial, inputs, world, JAVA_1_21_RULESET,
                             RolloutOptions(output_mode=RolloutOutputMode.SUMMARY))
            timings.append((time.perf_counter_ns() - started) / 1_000_000)
            if result.ticks_completed != ticks:
                raise RuntimeError(f"benchmark stopped at {result.ticks_completed}/{ticks}")
        results.append({
            "ticks": ticks, "branches": 1,
            "p50_ms": statistics.median(timings),
            "p95_ms": _percentile(timings, .95),
            "p99_ms": _percentile(timings, .99), "max_ms": max(timings),
            "effective_ticks_per_second": ticks / (statistics.median(timings) / 1000),
        })
    for branches in (32, 128):
        inputs = tuple(TickInput(1, 0, False, False, False, 0) for _ in range(20))
        timings = []
        for _ in range(max(3, args.repetitions // 5)):
            started = time.perf_counter_ns()
            for _ in range(branches):
                result = rollout(initial, inputs, world, JAVA_1_21_RULESET,
                                 RolloutOptions(output_mode=RolloutOutputMode.SUMMARY))
                if result.ticks_completed != 20:
                    raise RuntimeError("batch benchmark stopped early")
            timings.append((time.perf_counter_ns() - started) / 1_000_000)
        results.append({
            "ticks": 20, "branches": branches,
            "p50_ms": statistics.median(timings),
            "p95_ms": _percentile(timings, .95),
            "p99_ms": _percentile(timings, .99), "max_ms": max(timings),
            "effective_ticks_per_second": branches * 20 / (statistics.median(timings) / 1000),
        })
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    payload = {
        "schema_version": "mc2p.physics-benchmark.v1",
        "ruleset_id": JAVA_1_21_RULESET.ruleset_id,
        "clock": "perf_counter_ns", "repetitions": args.repetitions,
        "peak_traced_bytes": peak, "results": results,
    }
    encoded = json.dumps(payload, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
