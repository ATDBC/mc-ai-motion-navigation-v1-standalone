"""Freeze F2-S RED inputs, Windows timings, and independent v8 labels."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.support_surfaces import (
    StandableRegionDiagnostics,
    query_support_surfaces,
    standable_point_in_region,
    standable_region_in_goal,
)
from mc2p.motion_nav.world_model import Aabb, WorldQueryCache
from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.f2r_cases import materialized_manifest as v8_manifest
from tests.sim.f2r_cases import goal_for
from tests.sim.f2s_cases import MANIFEST, input_digest, materialized_manifest, scene_for


V8_RUNS = ROOT / "evidence/motion_navigation/F2R-piecewise-completion-v1/final/v8"
REGION_P95_LIMIT_MS = 4.0
REGION_P99_LIMIT_MS = 6.0
REGION_MAXIMUM_LIMIT_MS = 10.0

_V9_LAYERS = {
    "platform_outer_corner": "support_edge",
    "bridge_head": "support_edge",
    "column_top": "height_edge",
    "cliff_corner": "support_edge",
    "holes": "support_edge",
    "slab_edge": "height_edge",
    "stair_edge": "height_edge",
    "obstacle_support": "obstacle_edge",
    "cross_piece": "cross_piece",
}


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("timing sample cannot be empty")
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(statistics.median(values), 6),
        "p95": round(_percentile(values, .95), 6),
        "p99": round(_percentile(values, .99), 6),
        "maximum": round(max(values), 6),
    }


def _performance_fixture():
    import random
    config = materialized_manifest()["performance_inputs"]
    rng = random.Random(config["clutter_seed"])
    solids = {(x, 63, z): "minecraft:stone" for x in range(-6, 7) for z in range(-6, 7)}
    posts = {
        (x, z) for x in range(-5, 6) for z in range(-5, 6)
        if rng.random() < config["clutter_density"]
    }
    posts.discard((0, 0))
    for x, z in posts:
        for y in (64, 65, 66):
            solids[(x, y, z)] = "minecraft:stone"
    world = CalculatorBackend(
        [0], Scene(solids, ((-8, 8), (60, 68), (-8, 8))), (.5, 64., .5), 0.
    ).world._world
    half = 2.5 / math.sqrt(2.0)
    goal = Aabb(.5 - half, 63.92, .5 - half, .5 + half, 64.08, .5 + half)
    candidates = []
    for x in range(math.floor(goal.min_x), math.floor(math.nextafter(goal.max_x, -math.inf)) + 1):
        for z in range(math.floor(goal.min_z), math.floor(math.nextafter(goal.max_z, -math.inf)) + 1):
            candidates.extend(query_support_surfaces(
                world, x, z, goal.min_y, goal.max_y, collect_complete_missing=True
            ).surfaces)
    statuses = [standable_region_in_goal(world, surface, goal).status for surface in candidates]
    feasible = [surface for surface, status in zip(candidates, statuses) if status is QueryStatus.FEASIBLE]
    unavailable = [surface for surface, status in zip(candidates, statuses) if status is not QueryStatus.FEASIBLE]
    if not feasible or len(unavailable) < 2:
        raise AssertionError("frozen performance world no longer contains both result classes")
    xs = {goal.min_x, goal.max_x}
    zs = {goal.min_z, goal.max_z}
    for x, z in posts:
        for value in (x - .3, x + 1.3):
            if goal.min_x < value < goal.max_x:
                xs.add(value)
        for value in (z - .3, z + 1.3):
            if goal.min_z < value < goal.max_z:
                zs.add(value)
    expected_grid = {"x_boundaries": len(xs), "z_boundaries": len(zs),
                     "cells": (len(xs) - 1) * (len(zs) - 1)}
    orders = {
        "first_feasible": [feasible[0], *unavailable],
        "last_feasible": [*unavailable, feasible[0]],
        "all_unavailable": unavailable,
    }
    return world, goal, orders, expected_grid


def benchmark() -> dict:
    config = materialized_manifest()["performance_inputs"]
    world, goal, orders, expected_grid = _performance_fixture()
    output = {}
    for name, candidates in orders.items():
        timings = []
        counts = []
        observed_grids = set()
        for iteration in range(config["warmup_runs"] + config["measured_runs"]):
            cache = WorldQueryCache(world)
            exact_queries = 0
            started = time.perf_counter_ns()
            for surface in candidates:
                exact_queries += 1
                diagnostics = StandableRegionDiagnostics()
                selected = standable_region_in_goal(
                    world,
                    surface,
                    goal,
                    query_cache=cache,
                    diagnostics=diagnostics,
                )
                if iteration >= config["warmup_runs"]:
                    observed_grids.add((
                        diagnostics.x_boundaries,
                        diagnostics.z_boundaries,
                        diagnostics.cell_count,
                        diagnostics.valid_cell_count,
                    ))
                if selected.status is QueryStatus.FEASIBLE:
                    break
            elapsed = (time.perf_counter_ns() - started) / 1_000_000.0
            if iteration >= config["warmup_runs"]:
                timings.append(elapsed)
                counts.append(exact_queries)
        output[name] = {
            "candidate_count": len(candidates),
            "exact_query_count": {"minimum": min(counts), "maximum": max(counts)},
            "fixture_expected_unified_grid": expected_grid,
            "observed_unified_grids": [
                {
                    "x_boundaries": values[0],
                    "z_boundaries": values[1],
                    "cells": values[2],
                    "valid_cells": values[3],
                }
                for values in sorted(observed_grids)
            ],
            "timing_ms": _summary(timings),
        }
        timing = output[name]["timing_ms"]
        output[name]["gates"] = {
            "p95": timing["p95"] <= REGION_P95_LIMIT_MS,
            "p99": timing["p99"] <= REGION_P99_LIMIT_MS,
            "maximum": timing["maximum"] < REGION_MAXIMUM_LIMIT_MS,
        }
    return {
        "schema_version": "mc2p.f2s-support-region-windows-green.v1",
        "formal_platform": "windows",
        "measured_platform": platform.platform(),
        "python": sys.version,
        "python_executable": sys.executable,
        "hash_basis": "windows_worktree_bytes",
        "selection_is_wall_clock_independent": True,
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "cases": output,
        "thresholds_ms": {
            "p95_maximum": REGION_P95_LIMIT_MS,
            "p99_maximum": REGION_P99_LIMIT_MS,
            "maximum_strictly_less_than": REGION_MAXIMUM_LIMIT_MS,
        },
        "passed": all(
            all(case["gates"].values()) for case in output.values()
        ),
    }


def summarize_v9_rows(rows) -> dict:
    """Summarize later v9 runs without interpreting the gap as a gate."""
    rows = tuple(rows)
    identifiers = tuple(row.get("id") for row in rows)
    if (any(type(value) is not str or not value for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError("v9 result rows require unique nonempty ids")
    layers: dict[str, dict] = {}
    for row in rows:
        layer = row.get("layer")
        outcome = row.get("outcome")
        reason = row.get("reason")
        standable = row.get("standable_point_exists")
        if (type(layer) is not str or not layer
                or type(outcome) is not str or not outcome
                or type(reason) is not str or not reason
                or type(standable) is not bool):
            raise ValueError("v9 result row is missing its frozen output fields")
        summary = layers.setdefault(layer, {
            "tasks": 0,
            "completed": 0,
            "standable_point_exists_but_task_failed": 0,
            "reason_counts": Counter(),
        })
        summary["tasks"] += 1
        completed = outcome == "success"
        summary["completed"] += int(completed)
        summary["standable_point_exists_but_task_failed"] += int(
            standable and not completed
        )
        summary["reason_counts"][reason] += 1
    return {
        "schema_version": "mc2p.f2s-v9-layer-summary.v1",
        "tasks": len(rows),
        "layers": {
            name: {
                **summary,
                "reason_counts": dict(sorted(summary["reason_counts"].items())),
            }
            for name, summary in sorted(layers.items())
        },
        "product_gap_is_gate": False,
    }


def _standable_point_exists(world, goal: Aabb) -> bool:
    """Independent product-gap ruler; never grants execution permission."""
    for x in range(math.floor(goal.min_x), math.floor(math.nextafter(goal.max_x, -math.inf)) + 1):
        for z in range(math.floor(goal.min_z), math.floor(math.nextafter(goal.max_z, -math.inf)) + 1):
            result = query_support_surfaces(
                world, x, z, goal.min_y, goal.max_y, collect_complete_missing=True
            )
            if any(
                standable_point_in_region(world, surface, goal).status
                is QueryStatus.FEASIBLE
                for surface in result.surfaces
            ):
                return True
    return False


def run_v9_case(case: dict) -> dict:
    """Run one frozen F2-S case through the formal Runtime/Session chain."""
    import tests.sim.runner as runner
    from tests.sim.backend import CalculatorBackend, Perturbations

    scene = scene_for(case)
    goal = replace(
        goal_for(tuple(case["goal"]), case["target"]),
        region=Aabb(*case["goal_box"]),
    )
    truth = CalculatorBackend(
        [0], scene, tuple(case["start"]), case["yaw_degrees"]
    ).world._world
    point_exists = _standable_point_exists(truth, goal.region)
    scenario = runner.Scenario(
        case["id"], scene, tuple(case["start"]), tuple(case["goal"]),
        case["yaw_degrees"], max_ticks=case["max_ticks"],
    )
    with patch.object(runner, "_goal", lambda *_args, **_kwargs: goal):
        if case["condition"] == "first_late":
            normal = runner.run(scenario)
            first = next((
                row["movement_tick"]
                for row in normal.trace
                if row["applied_movement"]["forward"]
                or row["applied_movement"]["strafe"]
            ), None)
            if first is not None:
                scenario = replace(
                    scenario,
                    perturbations=Perturbations(late_ticks=frozenset({first})),
                )
        result = runner.run(scenario)
    final = result.trace[-1] if result.trace else {}
    return {
        "id": case["id"],
        "layer": _V9_LAYERS[case["family"]],
        "family": case["family"],
        "target": case["target"],
        "direction": case["direction"],
        "condition": case["condition"],
        "input_sha256": input_digest(case),
        "outcome": result.outcome,
        "reason": result.reason,
        "standable_point_exists": point_exists,
        "standable_point_exists_but_task_failed": point_exists and result.outcome != "success",
        "violations": result.violations,
        "verification_complete": result.verification_complete,
        "final_position": result.final_position,
        "damage": result.damage,
        "source_released": final.get("source_bound") is False,
        "terminal_sneaking": bool(final.get("sneaking")),
        "applied_perturbations": result.applied_perturbations,
        "ticks": result.ticks,
    }


def v9_new_gate(rows) -> bool:
    """Completion gaps are reported; only newly unsafe outcomes fail Task 4."""
    return all(
        not row["violations"]
        and row["source_released"]
        and row["damage"] == 0.0
        and not row["terminal_sneaking"]
        for row in rows
    )


def collect_v9_new(output: Path, *, workers: int = 4) -> dict:
    if output.exists():
        raise FileExistsError(output)
    import subprocess
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    source_worktree_clean = not subprocess.check_output(
        ["git", "status", "--porcelain"], text=True
    ).strip()
    cases = materialized_manifest()["support_region_cases"]
    output.mkdir(parents=True)
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        with (output / "runs.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
            for row in pool.map(run_v9_case, cases):
                rows.append(row)
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
                if len(rows) % 24 == 0:
                    print(f"F2-S v9 new: {len(rows)}/{len(cases)}", flush=True)
    layer_summary = summarize_v9_rows(rows)
    passed = v9_new_gate(rows)
    summary = {
        **layer_summary,
        "source_commit": source_commit,
        "source_worktree_clean_before_output": source_worktree_clean,
        "formal_platform": "windows",
        "measured_platform": platform.platform(),
        "python": sys.version,
        "completed": sum(row["outcome"] == "success" for row in rows),
        "verification_incomplete": sum(
            not row["verification_complete"] for row in rows
        ),
        "completion_is_gate": False,
        "gate": "zero safety events, damage, source leaks, and terminal sneaking",
        "passed": passed,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def _v8_rows() -> list[dict]:
    rows = []
    for path in sorted((V8_RUNS / "layers").glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return rows


def _case_by_id() -> dict[str, dict]:
    v8 = v8_manifest()
    return {row["id"]: row for row in (*v8["tasks"], *v8["clutter_scan"])}


def _goal_intersects_safe_center(case: dict, cell: tuple[int, int]) -> bool:
    x, z = cell
    min_x, _, min_z, max_x, _, max_z = case["goal_box"]
    return min(max_x, x + .7) >= max(min_x, x + .3) and min(max_z, z + .7) >= max(min_z, z + .3)


def independent_reference_label(case: dict, *, maximum_states: int, maximum_expansions: int) -> dict:
    """Bounded four-neighbour flood fill for the frozen flat clutter worlds.

    This function intentionally does not import the production planner.  A free
    column is a floor cell without a three-block post.  Exhausting this finite
    graph proves only reachability in the frozen flat reference model.
    """
    if "posts" not in case:
        return {"label": "UNRESOLVED", "expanded": 0, "reason": "not_flat_clutter"}
    posts = {tuple(item) for item in case["posts"]}
    floor = v8_manifest()["clutter"]["floor_ranges"]
    bounds = (range(*floor["x"]), range(*floor["z"]))
    free = {(x, z) for x in bounds[0] for z in bounds[1] if (x, z) not in posts}
    start = (math.floor(v8_manifest()["clutter"]["start"][0]),
             math.floor(v8_manifest()["clutter"]["start"][2]))
    if start not in free:
        return {"label": "UNRESOLVED", "expanded": 0,
                "reason": "discrete_reference_start_blocked"}
    queue = deque([start])
    visited = {start}
    expanded = 0
    while queue:
        if len(visited) > maximum_states or expanded >= maximum_expansions:
            return {"label": "UNRESOLVED", "expanded": expanded, "reason": "budget_exhausted"}
        current = queue.popleft()
        expanded += 1
        if _goal_intersects_safe_center(case, current):
            return {"label": "REFERENCE_REACHABLE", "expanded": expanded, "reason": "goal_reached"}
        x, z = current
        for candidate in ((x + 1, z), (x - 1, z), (x, z + 1), (x, z - 1)):
            if candidate in free and candidate not in visited:
                visited.add(candidate)
                queue.append(candidate)
    # Exhausting cell-centre four-neighbour motion does not exhaust the
    # player's continuous configuration space.  It can prove reachability,
    # but cannot prove physical unreachability.
    return {"label": "UNRESOLVED", "expanded": expanded,
            "reason": "discrete_reference_exhausted"}


def classify() -> dict:
    manifest = materialized_manifest()
    config = manifest["reference_search"]
    rows = _v8_rows()
    cases = _case_by_id()
    no_route = sorted(row["id"] for row in rows if row.get("reason") == config["source_reason"])
    labels = []
    for case_id in no_route:
        result = independent_reference_label(
            cases[case_id], maximum_states=config["maximum_states"],
            maximum_expansions=config["maximum_expansions"],
        )
        labels.append({"id": case_id, **result})
    failures = {}
    for reason in ("fixed_route_has_no_forward_control", "fixed_route_stalled"):
        failures[reason] = sorted(row["id"] for row in rows if row.get("reason") == reason)
    return {
        "schema_version": "mc2p.f2s-v8-failure-classification.v1",
        "hash_basis": "windows_worktree_bytes",
        "source_rows": len(rows),
        "no_route_count": len(no_route),
        "reference_label_counts": dict(sorted(Counter(row["label"] for row in labels).items())),
        "reference_labels": labels,
        "execution_failures": failures,
    }


def freeze() -> dict:
    manifest = materialized_manifest()
    return {
        "schema_version": manifest["schema_version"],
        "hash_basis": "windows_worktree_bytes",
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "v8_geometry_cases": len(manifest["v8_tasks"]),
        "v8_clutter_cases": len(manifest["v8_clutter_scan"]),
        "support_region_cases": len(manifest["support_region_cases"]),
        "support_region_input_sha256": input_digest(manifest["support_region_cases"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("freeze", "benchmark", "classify", "v9-new-formal")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    args = parser.parse_args(argv)
    if args.mode == "v9-new-formal":
        if args.output is None:
            parser.error("v9-new-formal requires --output")
        result = collect_v9_new(args.output, workers=args.workers)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    result = {"freeze": freeze, "benchmark": benchmark, "classify": classify}[args.mode]()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 1 if args.mode == "benchmark" and not result["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
