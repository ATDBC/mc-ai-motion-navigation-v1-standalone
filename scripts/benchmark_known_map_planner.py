"""Measure the B04 10,000-node known-map snapshot and first-route budget."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, PlanningRequest, PlanningStatus,
    SnapshotBuildStatus, plan_known_snapshot,
)
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)
from scripts.control_probe_core import write_json_atomic


def percentile(values: list[float], ratio: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * ratio) - 1)]


def motion_profile() -> GroundMotionProfile:
    value = json.loads(
        (ROOT / "config/motion-navigation/ordinary-ground-v1.json").read_text("utf-8")
    )
    profile = value["profile"]
    return GroundMotionProfile(
        float(value["scope"]["tick_seconds"]),
        float(profile["acceleration_blocks_per_second2"]),
        float(profile["velocity_retention_per_tick"]),
        float(profile["maximum_speed_blocks_per_second"]),
        frozenset(value["scope"]["support_materials"]),
    )


def run_benchmark(*, size: int = 100, runs: int = 20,
                  snapshot_batch_cells: int = 2048) -> dict:
    session = WorldSessionId("b04-benchmark")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 0, 0, "benchmark-clock", 0)
    world.observe_blocks(stamp, {
        (x, 0, z): BlockGeometry.full_cube("minecraft:grass_block")
        for x in range(size) for z in range(size)
    })
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(size)
        for z in range(size)
        for y in (-1, 1, 2)
    ))
    bounds = KnownMapBounds(0, size - 1, 1, 1, 0, size - 1, True)
    builder = KnownMapSnapshotBuilder(world.view(), bounds)
    snapshot_batches_ms: list[float] = []
    while True:
        started = time.perf_counter_ns()
        progress = builder.advance(world.view(), snapshot_batch_cells)
        snapshot_batches_ms.append((time.perf_counter_ns() - started) / 1e6)
        if progress.status is SnapshotBuildStatus.COMPLETE:
            break
    if progress.snapshot is None or not progress.snapshot.bounds.complete_scope:
        raise RuntimeError("benchmark snapshot is incomplete")
    request = PlanningRequest(
        1, "benchmark-request", "benchmark-goal", 1, session.value,
        (0, 1, 0), (size - 1, 1, size - 1),
    )
    planning_ms: list[float] = []
    result = None
    for _ in range(runs):
        started = time.perf_counter_ns()
        result = plan_known_snapshot(progress.snapshot, motion_profile(), request)
        planning_ms.append((time.perf_counter_ns() - started) / 1e6)
    assert result is not None
    if result.status is not PlanningStatus.COMPLETE:
        raise RuntimeError(f"benchmark route failed: {result.status.value}")
    return dict(
        schema_version="mc2p.b04-known-map-benchmark.v1",
        node_count=size * size, runs=runs,
        snapshot_batch_cells=snapshot_batch_cells,
        snapshot_batch_count=len(snapshot_batches_ms),
        snapshot_total_ms=sum(snapshot_batches_ms),
        snapshot_batch_p95_ms=percentile(snapshot_batches_ms, .95),
        snapshot_batch_max_ms=max(snapshot_batches_ms),
        planning_p50_ms=percentile(planning_ms, .50),
        planning_p95_ms=percentile(planning_ms, .95),
        planning_max_ms=max(planning_ms),
        expanded_nodes=result.expanded_nodes,
        path_nodes=len(result.path),
        passed=percentile(planning_ms, .95) <= 500.0,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_benchmark()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(args.output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
