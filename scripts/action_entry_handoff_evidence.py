"""Run the frozen D092 Walk-to-strict-entry handoff matrix.

This is a deterministic Windows formal-chain check.  It uses the real
Runtime -> Driver -> Session -> Executor -> Coordinator path and replaces
only Minecraft with the project's calculator-backed simulation backend.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter_ns
from unittest.mock import patch

from mc2p.motion_nav.world_model import Aabb
from mc2p.contracts.behavior import BehaviorProfileV0
from tests.sim.backend import Perturbations, Scene
from tests.sim.continuous_height_matrix import (
    _YAW_BY_DIRECTION,
    _rotate_cell,
    _rotate_point,
)
from tests.sim.f2r_cases import goal_for
from tests.sim.f2s_cases import materialized_manifest, scene_for
from tests.sim.runner import Scenario


STONE = "minecraft:stone"
DIRECTIONS = ("south", "east", "north", "west")
CONDITIONS = ("normal", "first_late")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _rotate_yaw(yaw_degrees: float, direction: str) -> float:
    return yaw_degrees + _YAW_BY_DIRECTION[direction]


def _scene_volume(solids: dict) -> tuple:
    xs = [position[0] for position in solids]
    zs = [position[2] for position in solids]
    return ((min(xs) - 3, max(xs) + 4), (60, 70),
            (min(zs) - 3, max(zs) + 4))


def _case(
    family: str,
    variant: int,
    direction: str,
    condition: str,
    solids: dict,
    start: tuple[float, float, float],
    goal: tuple[float, float, float],
    start_yaw_degrees: float,
) -> dict:
    rotated_solids = {
        _rotate_cell(position, direction): material
        for position, material in solids.items()
    }
    rotated = {
        "id": f"handoff/{family}-{variant}/{direction}/{condition}",
        "family": family,
        "variant": variant,
        "direction": direction,
        "condition": condition,
        "solids": [[*position, material]
                   for position, material in sorted(rotated_solids.items())],
        "volume": _scene_volume(rotated_solids),
        "start": _rotate_point(start, direction),
        "goal": _rotate_point(goal, direction),
        "yaw_degrees": _rotate_yaw(start_yaw_degrees, direction),
        "max_ticks": 400,
    }
    rotated["input_sha256"] = _digest(rotated)
    return rotated


def frozen_cases() -> tuple[dict, ...]:
    """Return 56 synthetic cases plus the existing 24 v9 column cases."""
    rows = []
    broad_floor = {
        (x, 63, z): STONE for x in range(-5, 6) for z in range(-6, 7)
    }
    raised = {
        (x, 64, z): STONE for x in range(-5, 6) for z in range(2, 6)
    }
    for direction in DIRECTIONS:
        for condition in CONDITIONS:
            rows.append(_case(
                "straight_wrong_yaw", 3, direction, condition,
                broad_floor | raised, (.5, 64., -1.5), (.5, 65., 2.5),
                90.0,
            ))
            for run_up in (1, 2, 3):
                rows.append(_case(
                    "short_run_up", run_up, direction, condition,
                    broad_floor | raised,
                    (.5, 64., 2 - run_up - .5), (.5, 65., 2.5),
                    90.0,
                ))
                low = (
                    {(0, 63, z): STONE for z in range(-3, 6)}
                    | {(x, 63, 5): STONE for x in range(0, 3)}
                )
                high = {(x, 64, 5): STONE for x in range(3, 7)}
                rows.append(_case(
                    "turn_90", run_up, direction, condition,
                    low | high,
                    (.5, 64., 5 - run_up - .5), (3.5, 65., 5.5),
                    0.0,
                ))
    for case in materialized_manifest()["support_region_cases"]:
        if case["family"] == "column_top":
            rows.append({
                "id": case["id"],
                "family": "column_top",
                "variant": case["target"],
                "direction": case["direction"],
                "condition": case["condition"],
                "frozen_v9_case": case,
                "input_sha256": _digest(case),
            })
    assert len(rows) == 80
    assert len({row["id"] for row in rows}) == 80
    return tuple(rows)


def _scenario(case: dict) -> tuple[Scenario, object | None]:
    if "frozen_v9_case" in case:
        source = case["frozen_v9_case"]
        goal = replace(
            goal_for(tuple(source["goal"]), source["target"]),
            region=Aabb(*source["goal_box"]),
        )
        return Scenario(
            source["id"], scene_for(source), tuple(source["start"]),
            tuple(source["goal"]), source["yaw_degrees"],
            max_ticks=source["max_ticks"],
        ), goal
    solids = {
        tuple(row[:3]): row[3] for row in case["solids"]
    }
    return Scenario(
        case["id"], Scene(solids, case["volume"]), tuple(case["start"]),
        tuple(case["goal"]), case["yaw_degrees"],
        max_ticks=case["max_ticks"],
    ), None


def _run_once(scenario: Scenario, goal):
    import tests.sim.runner as runner
    entry_yaws = {}

    def control_step(context):
        active = context.session._active_route
        if active is not None:
            for index, action in enumerate(active.action_route.actions):
                window = getattr(action, "entry_window", None)
                if (window is not None
                        and window.required_yaw_radians is not None):
                    entry_yaws[index] = window.required_yaw_radians
        context.driver.tick(
            BehaviorProfileV0(), context.clock[0] + 500_000_000,
        )
        return ()

    if goal is None:
        return runner.run(scenario, control_step=control_step), entry_yaws
    with patch.object(runner, "_goal", lambda *_args, **_kwargs: goal):
        return runner.run(scenario, control_step=control_step), entry_yaws


def run_case(case: dict) -> dict:
    scenario, goal = _scenario(case)
    if case["condition"] == "first_late":
        normal, _ = _run_once(scenario, goal)
        first_tick = next((
            frame["movement_tick"] for frame in normal.trace
            if frame["applied_movement"]["forward"]
            or frame["applied_movement"]["strafe"]
        ), None)
        if first_tick is not None:
            scenario = replace(
                scenario,
                perturbations=Perturbations(
                    late_ticks=frozenset({first_tick}),
                ),
            )
    started = perf_counter_ns()
    result, entry_yaws = _run_once(scenario, goal)
    elapsed = perf_counter_ns() - started
    jump_frames = [
        frame for frame in result.trace if frame["applied_movement"]["jump"]
    ]
    yaw_errors = [
        math.degrees(abs(math.atan2(
            math.sin(frame["yaw_radians"] - entry_yaws[frame["action_index"]]),
            math.cos(frame["yaw_radians"] - entry_yaws[frame["action_index"]]),
        )))
        for frame in jump_frames
        if frame["action_index"] in entry_yaws
    ]
    final = result.trace[-1] if result.trace else {}
    action_kinds = []
    for frame in result.trace:
        kind = frame["action_kind"]
        if kind and (not action_kinds or action_kinds[-1] != kind):
            action_kinds.append(kind)
    return {
        "id": case["id"],
        "family": case["family"],
        "variant": case["variant"],
        "direction": case["direction"],
        "condition": case["condition"],
        "input_sha256": case["input_sha256"],
        "outcome": result.outcome,
        "reason": result.reason,
        "ticks": result.ticks,
        "elapsed_ns": elapsed,
        "action_kinds": action_kinds,
        "jump_frames": len(jump_frames),
        "jump_frames_with_entry_yaw": len(yaw_errors),
        "maximum_jump_yaw_error_degrees": (
            None if not yaw_errors else max(yaw_errors)
        ),
        "applied_perturbations": result.applied_perturbations,
        "violations": result.violations,
        "damage": result.damage,
        "source_released": final.get("source_bound") is False,
        "final_position": result.final_position,
    }


def collect(output: Path) -> dict:
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    rows = []
    with (output / "runs.jsonl").open(
        "w", encoding="utf-8", newline="\n",
    ) as stream:
        for case in frozen_cases():
            row = run_case(case)
            rows.append(row)
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    summary = {
        "schema_version": "mc2p.action-entry-handoff-evidence.v1",
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
        ).strip(),
        "formal_platform": "windows",
        "measured_platform": platform.platform(),
        "python": sys.version,
        "cases": len(rows),
        "completed": sum(row["outcome"] == "success" for row in rows),
        "reason_counts": dict(sorted(Counter(
            row["reason"] for row in rows
        ).items())),
        "missing_jump": sum(not row["jump_frames"] for row in rows),
        "jump_frames_missing_entry_yaw": sum(
            row["jump_frames"] != row["jump_frames_with_entry_yaw"]
            for row in rows
        ),
        "entry_yaw_outside_two_degrees": sum(
            row["maximum_jump_yaw_error_degrees"] is not None
            and row["maximum_jump_yaw_error_degrees"] > 2.0 + 1.0e-9
            for row in rows
        ),
        "safety_events": sum(bool(row["violations"]) for row in rows),
        "damage_events": sum(row["damage"] > 0 for row in rows),
        "source_leaks": sum(not row["source_released"] for row in rows),
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args(argv)
    if args.plan_only:
        print(json.dumps(frozen_cases(), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(collect(args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
