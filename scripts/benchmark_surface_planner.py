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
    ground, step = profiles()
    planning_ms: list[float] = []
    targets = tuple(dict.fromkeys((
        (size - 1, size - 1),
        (size - 1, (size - 1) // 3),
        ((size - 1) // 4, size - 1),
    )))
    cases = []
    result = None
    for case_index, (goal_x, goal_z) in enumerate(targets, start=1):
        request = SurfacePlanningRequest(
            case_index, f"surface-benchmark-{case_index}",
            f"surface-benchmark-goal-{case_index}", 1, session.value,
            SurfaceNodeId(0, 0, 1, 0),
            SurfaceNodeId(goal_x, goal_z, 1, 0),
        )
        case_ms: list[float] = []
        for _ in range(runs):
            started = time.perf_counter_ns()
            result = plan_known_surface_snapshot(
                progress.snapshot, ground, step, request,
            )
            elapsed_ms = (time.perf_counter_ns() - started) / 1e6
            case_ms.append(elapsed_ms)
            planning_ms.append(elapsed_ms)
        if result.status is not SurfacePlanningStatus.COMPLETE:
            raise RuntimeError(
                f"surface benchmark route failed for {(goal_x, goal_z)}: "
                f"{result.status.value}"
            )
        cases.append(dict(
            kind="flat",
            goal=[goal_x, goal_z],
            planning_p50_ms=percentile(case_ms, .50),
            planning_p95_ms=percentile(case_ms, .95),
            planning_max_ms=max(case_ms),
            expanded_nodes=result.expanded_nodes,
            path_nodes=len(result.path),
        ))
    maze_size = min(size, 20)
    if maze_size >= 8:
        maze_session = WorldSessionId("surface-planner-maze-benchmark")
        maze_world = WorldKnowledge(maze_session)
        maze_stamp = ObservationStamp(
            maze_session, 0, 0, "benchmark-clock", 0,
        )
        wall_cells: set[tuple[int, int, int]] = set()
        for wall_index, x in enumerate(range(2, maze_size - 1, 3)):
            opening = maze_size - 1 if wall_index % 2 == 0 else 0
            for z in range(maze_size):
                if z != opening:
                    wall_cells.update(((x, 1, z), (x, 2, z)))
        maze_world.confirm_air(maze_stamp, tuple(
            (x, y, z)
            for x in range(maze_size)
            for z in range(maze_size)
            for y in (-1, 1, 2)
            if (x, y, z) not in wall_cells
        ))
        maze_blocks = {
            (x, 0, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(maze_size) for z in range(maze_size)
        }
        maze_blocks.update({
            position: BlockGeometry.full_cube("minecraft:stone")
            for position in wall_cells
        })
        maze_world.observe_blocks(maze_stamp, maze_blocks)
        maze_bounds = KnownMapBounds(
            0, maze_size - 1, 1, 1, 0, maze_size - 1, True,
        )
        maze_progress = KnownMapSnapshotBuilder(
            maze_world.view(), maze_bounds,
        ).advance(maze_world.view(), 10_000_000)
        if maze_progress.snapshot is None:
            raise RuntimeError("surface maze benchmark snapshot is incomplete")
        maze_request = SurfacePlanningRequest(
            len(cases) + 1, "surface-benchmark-maze",
            "surface-benchmark-maze-goal", 1, maze_session.value,
            SurfaceNodeId(0, 0, 1, 0),
            SurfaceNodeId(maze_size - 1, maze_size - 1, 1, 0),
        )
        maze_ms: list[float] = []
        maze_result = None
        for _ in range(min(runs, 5)):
            started = time.perf_counter_ns()
            maze_result = plan_known_surface_snapshot(
                maze_progress.snapshot, ground, step, maze_request,
            )
            elapsed_ms = (time.perf_counter_ns() - started) / 1e6
            maze_ms.append(elapsed_ms)
            planning_ms.append(elapsed_ms)
        if (maze_result is None
                or maze_result.status is not SurfacePlanningStatus.COMPLETE):
            status = None if maze_result is None else maze_result.status.value
            raise RuntimeError(f"surface maze benchmark failed: {status}")
        cases.append(dict(
            kind="maze",
            size=maze_size,
            goal=[maze_size - 1, maze_size - 1],
            planning_p50_ms=percentile(maze_ms, .50),
            planning_p95_ms=percentile(maze_ms, .95),
            planning_max_ms=max(maze_ms),
            expanded_nodes=maze_result.expanded_nodes,
            path_nodes=len(maze_result.path),
        ))
    assert result is not None
    diagonal = cases[0]
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
        expanded_nodes=diagonal["expanded_nodes"],
        path_nodes=diagonal["path_nodes"],
        cases=cases,
        passed=all(case["planning_p95_ms"] <= 500.0 for case in cases),
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
