"""Run the frozen v9 inputs on the unmodified F2-R formal chain."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
from unittest.mock import patch

from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.support_surfaces import (
    query_support_surfaces,
    standable_point_in_region,
)
from mc2p.motion_nav.world_model import Aabb
from tests.sim.backend import CalculatorBackend, Perturbations
from tests.sim.f2r_cases import goal_for
from tests.sim.f2s_cases import input_digest, materialized_manifest, scene_for


_LAYERS = {
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


def _standable_point_exists(world, goal: Aabb) -> bool:
    """Independent diagnostic; it never grants execution permission."""
    for x in range(
        math.floor(goal.min_x),
        math.floor(math.nextafter(goal.max_x, -math.inf)) + 1,
    ):
        for z in range(
            math.floor(goal.min_z),
            math.floor(math.nextafter(goal.max_z, -math.inf)) + 1,
        ):
            result = query_support_surfaces(
                world, x, z, goal.min_y, goal.max_y,
                collect_complete_missing=True,
            )
            if any(
                standable_point_in_region(world, surface, goal).status
                is QueryStatus.FEASIBLE
                for surface in result.surfaces
            ):
                return True
    return False


def run_v9_case(case: dict) -> dict:
    import tests.sim.runner as runner

    scene = scene_for(case)
    goal = replace(
        goal_for(tuple(case["goal"]), case["target"]),
        region=Aabb(*case["goal_box"]),
    )
    truth = CalculatorBackend(
        [0], scene, tuple(case["start"]), case["yaw_degrees"],
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
                    perturbations=Perturbations(
                        late_ticks=frozenset({first}),
                    ),
                )
        result = runner.run(scenario)
    final = result.trace[-1] if result.trace else {}
    return {
        "id": case["id"],
        "layer": _LAYERS[case["family"]],
        "family": case["family"],
        "target": case["target"],
        "direction": case["direction"],
        "condition": case["condition"],
        "input_sha256": input_digest(case),
        "outcome": result.outcome,
        "reason": result.reason,
        "standable_point_exists": point_exists,
        "standable_point_exists_but_task_failed": (
            point_exists and result.outcome != "success"
        ),
        "violations": result.violations,
        "verification_complete": result.verification_complete,
        "final_position": result.final_position,
        "damage": result.damage,
        "source_released": final.get("source_bound") is False,
        "terminal_sneaking": bool(final.get("sneaking")),
        "applied_perturbations": result.applied_perturbations,
        "ticks": result.ticks,
    }


def collect(output: Path, *, workers: int = 4) -> dict:
    if output.exists():
        raise FileExistsError(output)
    cases = materialized_manifest()["support_region_cases"]
    output.mkdir(parents=True)
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        with (output / "runs.jsonl").open(
            "w", encoding="utf-8", newline="\n",
        ) as stream:
            for row in pool.map(run_v9_case, cases):
                rows.append(row)
                stream.write(json.dumps(row, sort_keys=True) + "\n")
    reason_counts = Counter(row["reason"] for row in rows)
    summary = {
        "schema_version": "mc2p.f2rec-r0-v9.v1",
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
        ).strip(),
        "formal_platform": "windows",
        "measured_platform": platform.platform(),
        "python": sys.version,
        "tasks": len(rows),
        "completed": sum(row["outcome"] == "success" for row in rows),
        "standable_point_exists_but_task_failed": sum(
            row["standable_point_exists_but_task_failed"] for row in rows
        ),
        "reason_counts": dict(sorted(reason_counts.items())),
        "safety_events": sum(bool(row["violations"]) for row in rows),
        "damage_events": sum(row["damage"] > 0 for row in rows),
        "source_leaks": sum(not row["source_released"] for row in rows),
        "terminal_sneaking": sum(row["terminal_sneaking"] for row in rows),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    args = parser.parse_args(argv)
    print(json.dumps(collect(args.output, workers=args.workers), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
