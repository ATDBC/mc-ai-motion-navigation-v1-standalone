"""Measure the formal lazy surface-planning entry on a known flat map."""
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

from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SnapshotBuildStatus,
    SurfacePlanningRequest, SurfacePlanningStatus, plan_known_surface_snapshot,
)
from mc2p.motion_nav.step_transition import load_step_profile
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)
from scripts.control_probe_core import write_json_atomic


def percentile(values: list[float], ratio: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * ratio) - 1)]


def profiles():
    environment = load_frozen_environment(
        ROOT / "config/motion-navigation/environment-v1.json"
    )
    catalog = BlockMotionCatalog.load(
        ROOT / "config/motion-navigation/block-motion-traits-v1.json",
        ROOT / "config/motion-navigation/vanilla-block-registry-1_21.json",
    )
    ground = load_ground_motion_profile(
        ROOT / "config/motion-navigation/ordinary-ground-b07-v1.json",
        environment=environment, catalog=catalog,
    )
    step = load_step_profile(
        ROOT / "config/motion-navigation/step-b07-v1.json",
        environment=environment,
    )
    return ground, step


def run_benchmark(*, size: int = 100, runs: int = 20,
                  snapshot_batch_cells: int = 2048) -> dict:
    if size < 2 or runs < 1:
        raise ValueError("surface benchmark requires size >= 2 and runs >= 1")
    session = WorldSessionId("surface-planner-benchmark")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 0, 0, "benchmark-clock", 0)
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(size)
        for z in range(size)
        for y in (-1, 1, 2)
    ))
    world.observe_blocks(stamp, {
        (x, 0, z): BlockGeometry.full_cube("minecraft:stone")
        for x in range(size) for z in range(size)
    })
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
        raise RuntimeError("surface benchmark snapshot is incomplete")
    request = SurfacePlanningRequest(
        1, "surface-benchmark", "surface-benchmark-goal", 1, session.value,
        SurfaceNodeId(0, 0, 1, 0),
        SurfaceNodeId(size - 1, size - 1, 1, 0),
    )
    ground, step = profiles()
    planning_ms: list[float] = []
    result = None
    for _ in range(runs):
        started = time.perf_counter_ns()
        result = plan_known_surface_snapshot(
            progress.snapshot, ground, step, request,
        )
        planning_ms.append((time.perf_counter_ns() - started) / 1e6)
    assert result is not None
    if result.status is not SurfacePlanningStatus.COMPLETE:
        raise RuntimeError(f"surface benchmark route failed: {result.status.value}")
    return dict(
        schema_version="mc2p.surface-planner-benchmark.v1",
        planner_entry="plan_known_surface_snapshot",
        node_count=size * size,
        runs=runs,
        snapshot_batch_cells=snapshot_batch_cells,
        snapshot_batch_count=len(snapshot_batches_ms),
        snapshot_total_ms=sum(snapshot_batches_ms),
        snapshot_batch_p95_ms=percentile(snapshot_batches_ms, .95),
        planning_p50_ms=percentile(planning_ms, .50),
        planning_p95_ms=percentile(planning_ms, .95),
        planning_max_ms=max(planning_ms),
        expanded_nodes=result.expanded_nodes,
        path_nodes=len(result.path),
        passed=percentile(planning_ms, .95) <= 500.0,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_benchmark(size=args.size, runs=args.runs)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(args.output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
