"""Measure D108 snapshot invalidation and bounded materialization on Windows.

The gate times public WorldKnowledge calls. Internal cache sets are inspected
only to prove lifecycle and range isolation; they are never used as a latency
substitute.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
from pathlib import Path
import statistics
import threading
import time

from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.world_model import (
    BlockGeometry,
    ObservationStamp,
    WorldKnowledge,
    WorldSessionId,
)


SCHEMA = "mc2p.d108-snapshot-materialization-performance.v1"
STONE = BlockGeometry.full_cube("minecraft:stone")
DIRT = BlockGeometry.full_cube("minecraft:dirt")
SMALL_BOUNDS = PhysicsWorldBounds(0, 3, 0, 3, 0, 3)
B10_BOUNDS = PhysicsWorldBounds(-2, 2, 61, 68, -2, 4)


class FixtureClock:
    def __init__(self, session: WorldSessionId) -> None:
        self.session = session
        self.sequence = 0

    def stamp(self) -> ObservationStamp:
        self.sequence += 1
        return ObservationStamp(
            self.session, self.sequence, self.sequence,
            "d108-performance-clock", self.sequence,
        )


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def _summary(values: list[float]) -> dict[str, object]:
    return {
        "samples": len(values),
        "raw_samples_ms": list(values),
        "p50_ms": statistics.median(values),
        "p95_ms": _percentile(values, .95),
        "p99_ms": _percentile(values, .99),
        "max_ms": max(values),
    }


def _passes_deadline(summary: dict[str, object]) -> bool:
    return (summary["p95_ms"] <= 8.0 and summary["p99_ms"] <= 15.0
            and summary["max_ms"] < 30.0)


def _full_section_world(section_count: int, name: str):
    session = WorldSessionId(name)
    clock = FixtureClock(session)
    world = WorldKnowledge(
        session, max_known_cells=4096 * section_count + 16,
    )
    for section in range(section_count):
        base = section * 16
        world.observe_blocks(clock.stamp(), {
            (base + x, y, z): STONE
            for x in range(16) for y in range(16) for z in range(16)
        })
    return world, clock


def _b10_world():
    session = WorldSessionId("d108-b10-bounds")
    clock = FixtureClock(session)
    world = WorldKnowledge(session)
    cells = tuple(
        (x, y, z)
        for x in range(-4, 5)
        for y in range(40, 71)
        for z in range(-4, 5)
    )
    world.confirm_air(clock.stamp(), cells)
    world.observe_blocks(clock.stamp(), {
        (x, 63, z): STONE
        for x in range(-4, 5)
        for z in range(-4, 5)
        if (x, 63, z) != (0, 63, 1)
    })
    return world, clock


def _semantic_case(section_count: int, samples: int) -> dict[str, object]:
    world, clock = _full_section_world(section_count, f"d108-semantic-{section_count}")
    durations = []

    def change(index: int) -> None:
        material = DIRT if index % 2 == 0 else STONE
        changes = {
            (section * 16 + index % 16, 0, 0): material
            for section in range(section_count)
        }
        world.observe_blocks(clock.stamp(), changes)

    for index in range(3):
        change(index)
    gc.collect(2)
    for index in range(3, samples + 3):
        started = time.perf_counter_ns()
        change(index)
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
    summary = _summary(durations)
    return {"sections": section_count, **summary,
            "deadline_passed": _passes_deadline(summary)}


def _borrow_samples(world, clock: FixtureClock, bounds: PhysicsWorldBounds,
                    dirty_positions: tuple[tuple[int, int, int], ...],
                    samples: int) -> list[float]:
    def borrow_once() -> float:
        facts = {position: world.view().cell(position) for position in dirty_positions}
        blocks = {position: fact.block for position, fact in facts.items()
                  if fact.block is not None}
        air = tuple(position for position, fact in facts.items() if fact.block is None)
        if blocks:
            world.observe_blocks(clock.stamp(), blocks)
        if air:
            world.confirm_air(clock.stamp(), air)
        started = time.perf_counter_ns()
        borrowed = world.borrow_snapshot_version(bounds, ruleset=JAVA_1_21_RULESET)
        duration = (time.perf_counter_ns() - started) / 1_000_000
        if borrowed is None:
            raise AssertionError("formal snapshot borrow unexpectedly returned no version")
        return duration

    for _ in range(3):
        borrow_once()
    gc.collect(2)
    durations = []
    for _ in range(samples):
        durations.append(borrow_once())
    return durations


def _deterministic_lock_wait(world, clock: FixtureClock,
                             bounds: PhysicsWorldBounds,
                             refresh: tuple[int, int, int]) -> dict[str, float]:
    """Separate lock wait from time before the observer obtains a Python turn."""
    world.observe_blocks(clock.stamp(), {
        refresh: world.view().cell(refresh).block,
    })
    commit_entered = threading.Event()
    observation_released = threading.Event()
    observation_attempting = threading.Event()
    result: dict[str, float] = {}
    original_commit = world._commit_snapshot_sections

    def synchronized_commit(sections):
        commit_entered.set()
        if not observation_attempting.wait(2):
            raise AssertionError("observation did not attempt the held owner lock")
        return original_commit(sections)

    def observe():
        observation_released.wait()
        result["attempt_ns"] = time.perf_counter_ns()
        observation_attempting.set()
        world.observe_blocks(clock.stamp(), {
            refresh: world.view().cell(refresh).block,
        })
        result["finished_ns"] = time.perf_counter_ns()

    observer = threading.Thread(target=observe, name="d108-control-observer")
    observer.start()
    from unittest.mock import patch
    with patch.object(world, "_commit_snapshot_sections", side_effect=synchronized_commit):
        borrower = threading.Thread(
            target=lambda: world.borrow_snapshot_version(
                bounds, ruleset=JAVA_1_21_RULESET,
            ),
            name="d108-snapshot-borrower",
        )
        borrower.start()
        if not commit_entered.wait(2):
            raise AssertionError("borrow did not enter materialization")
        result["released_ns"] = time.perf_counter_ns()
        observation_released.set()
        borrower.join(2)
        observer.join(2)
    if borrower.is_alive() or observer.is_alive():
        raise AssertionError("snapshot contention probe did not terminate")
    return {
        "python_turn_delay_ms": (result["attempt_ns"] - result["released_ns"]) / 1_000_000,
        "owner_lock_wait_ms": (result["finished_ns"] - result["attempt_ns"]) / 1_000_000,
    }


def _python_control_pause(world, bounds: PhysicsWorldBounds) -> float:
    """Measure the longest interval a ready Python control thread cannot run."""
    begin = threading.Event()
    sampling = threading.Event()
    finished = threading.Event()
    gaps_ns = []

    def sample():
        begin.wait()
        previous = time.perf_counter_ns()
        sampling.set()
        while not finished.is_set():
            current = time.perf_counter_ns()
            gaps_ns.append(current - previous)
            previous = current
        current = time.perf_counter_ns()
        gaps_ns.append(current - previous)

    sampler = threading.Thread(target=sample, name="d108-python-control-sampler")
    sampler.start()
    begin.set()
    if not sampling.wait(2):
        raise AssertionError("Python control sampler did not start")
    borrowed = world.borrow_snapshot_version(bounds, ruleset=JAVA_1_21_RULESET)
    finished.set()
    sampler.join(2)
    if sampler.is_alive() or borrowed is None or not gaps_ns:
        raise AssertionError("Python control pause probe did not terminate")
    return max(gaps_ns) / 1_000_000


def _dirty_case(section_count: int, samples: int) -> dict[str, object]:
    world, clock = _full_section_world(section_count, f"d108-dirty-{section_count}")
    dirty = tuple((section * 16, 0, 0) for section in range(section_count))
    durations = _borrow_samples(world, clock, SMALL_BOUNDS, dirty, samples)
    summary = _summary(durations)
    lock = _deterministic_lock_wait(world, clock, SMALL_BOUNDS, (0, 0, 0))
    python_pause_ms = _python_control_pause(world, SMALL_BOUNDS)
    lifecycle_ok = set(range_sections(1, section_count)).issubset(
        world._snapshot_dirty_sections
    )
    return {
        "unrelated_dirty_sections": section_count,
        **summary,
        **lock,
        "python_control_unavailable_ms": python_pause_ms,
        "deadline_passed": _passes_deadline(summary),
        "range_lifecycle_passed": lifecycle_ok,
    }


def range_sections(start: int, stop: int | None = None):
    if stop is None:
        start, stop = 0, start
    return ((section, 0, 0) for section in range(start, stop))


def _b10_case(samples: int) -> dict[str, object]:
    world, clock = _b10_world()
    facts = tuple(
        position for section in world._sections.values() for position in section
        if (B10_BOUNDS.min_x <= position[0] <= B10_BOUNDS.max_x
            and B10_BOUNDS.min_y <= position[1] <= B10_BOUNDS.max_y
            and B10_BOUNDS.min_z <= position[2] <= B10_BOUNDS.max_z)
    )
    durations = _borrow_samples(world, clock, B10_BOUNDS, facts, samples)
    summary = _summary(durations)
    block = next(position for position in facts
                 if world.view().cell(position).block is not None)
    lock = _deterministic_lock_wait(world, clock, B10_BOUNDS, block)
    python_pause_ms = _python_control_pause(world, B10_BOUNDS)
    return {"bounds": [B10_BOUNDS.min_x, B10_BOUNDS.max_x,
                       B10_BOUNDS.min_y, B10_BOUNDS.max_y,
                       B10_BOUNDS.min_z, B10_BOUNDS.max_z],
            "known_cells_in_bounds": len(facts), **summary, **lock,
            "python_control_unavailable_ms": python_pause_ms,
            "deadline_passed": _passes_deadline(summary)}


def run_probe(samples: int = 20) -> dict[str, object]:
    if type(samples) is not int or samples < 3:
        raise ValueError("D108 performance probe needs at least three samples")
    semantic = [_semantic_case(count, samples) for count in (1, 2, 4, 8)]
    dirty = [_dirty_case(count, samples) for count in (4, 16, 32)]
    b10 = _b10_case(samples)
    passed = (all(row["deadline_passed"] for row in semantic)
              and all(row["deadline_passed"] and row["range_lifecycle_passed"]
                      and row["owner_lock_wait_ms"] < 10.0
                      and row["python_control_unavailable_ms"] < 30.0
                      for row in dirty)
              and b10["deadline_passed"] and b10["owner_lock_wait_ms"] < 10.0
              and b10["python_control_unavailable_ms"] < 30.0)
    return {"schema": SCHEMA, "warmups_per_case": 3,
            "samples_per_case": samples,
            "semantic_changes": semantic, "unrelated_dirty": dirty,
            "b10_bounds": b10, "passed": passed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_probe(args.samples)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
