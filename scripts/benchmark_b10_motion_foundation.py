"""Measure the B10-A command projection plus deterministic physics chain."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.online_motion import ProjectionStatus, project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET
from scripts.benchmark_motion_calculator import _fixture


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _complete_check(initial, world, ticks: int, branches: int) -> None:
    command = MovementV1(forward=1)
    for _ in range(branches):
        current = initial
        for _ in range(ticks):
            projected = project_movement_command(current, command)
            if projected.status is not ProjectionStatus.READY or projected.tick_input is None:
                raise RuntimeError("B10 input projection was incomplete")
            calculated = step(current, projected.tick_input, world, JAVA_1_21_RULESET)
            if calculated.status is not CalculationStatus.OK or calculated.next_state is None:
                raise RuntimeError("B10 physics chain was incomplete")
            current = calculated.next_state


def _measure(initial, world, ticks: int, branches: int,
             repetitions: int) -> dict[str, float | int]:
    wall: list[float] = []
    cpu: list[float] = []
    for _ in range(repetitions):
        wall_started = time.perf_counter_ns()
        cpu_started = time.thread_time_ns()
        _complete_check(initial, world, ticks, branches)
        cpu.append((time.thread_time_ns() - cpu_started) / 1_000_000)
        wall.append((time.perf_counter_ns() - wall_started) / 1_000_000)
    return {
        "ticks": ticks,
        "branches": branches,
        "samples": repetitions,
        "wall_p50_ms": statistics.median(wall),
        "wall_p95_ms": _percentile(wall, .95),
        "wall_p99_ms": _percentile(wall, .99),
        "wall_max_ms": max(wall),
        "cpu_p50_ms": statistics.median(cpu),
        "cpu_p95_ms": _percentile(cpu, .95),
        "cpu_p99_ms": _percentile(cpu, .99),
        "cpu_max_ms": max(cpu),
    }


def run_benchmark(*, repetitions: int = 30,
                  batch_sizes: tuple[int, ...] = (8, 32)) -> dict:
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be positive")
    if (type(batch_sizes) is not tuple or any(
            type(value) is not int or value < 1 for value in batch_sizes)):
        raise ValueError("batch sizes must be positive integers")
    initial, world = _fixture()
    wall_started = time.perf_counter_ns()
    cpu_started = time.thread_time_ns()
    _complete_check(initial, world, 20, 1)
    cold = {
        "ticks": 20,
        "branches": 1,
        "wall_ms": (time.perf_counter_ns() - wall_started) / 1_000_000,
        "cpu_ms": (time.thread_time_ns() - cpu_started) / 1_000_000,
    }
    results = [_measure(initial, world, ticks, 1, repetitions)
               for ticks in (1, 20, 60)]
    batch_repetitions = max(1, repetitions // 5)
    results.extend(_measure(initial, world, 20, branches, batch_repetitions)
                   for branches in batch_sizes)
    return {
        "schema_version": "mc2p.b10-foundation-benchmark.v1",
        "ruleset_id": JAVA_1_21_RULESET.ruleset_id,
        "input_projection_version": "mc2p.input-projection.v1",
        "clocks": {"wall": "perf_counter_ns", "cpu": "thread_time_ns"},
        "repetitions": repetitions,
        "cold_start": cold,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = run_benchmark(repetitions=args.repetitions)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
