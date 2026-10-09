"""Frozen F2-S inputs; builders here are test-only world oracles."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from tests.sim.backend import Scene
from tests.sim.continuous_height_matrix import _rotate_cell, _rotate_point, _YAW_BY_DIRECTION
from tests.sim.f2r_cases import (
    DIRECTIONS,
    TARGETS,
    goal_for,
    materialized_manifest as v8_manifest,
)


MANIFEST = Path(__file__).parent / "manifests/navigation-product-f2s-support-region-v9.json"
FAMILIES = (
    "platform_outer_corner",
    "bridge_head",
    "column_top",
    "cliff_corner",
    "holes",
    "slab_edge",
    "stair_edge",
    "obstacle_support",
    "cross_piece",
)
CONDITIONS = ("normal", "first_late")
STONE = "minecraft:stone"
SLAB = "minecraft:smooth_stone_slab[type=bottom]"
STAIR = "minecraft:oak_stairs[facing=south,half=bottom,shape=straight]"


def input_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def frozen_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _base_scene(family: str, seed: int) -> tuple[dict, tuple, tuple, tuple]:
    floor = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
    start = (.5, 64., .5)
    goal = (4.7, 64., 10.7)
    volume = ((-7, 8), (60, 69), (-5, 14))
    if family == "platform_outer_corner":
        solids = floor
    elif family == "bridge_head":
        solids = {(x, 63, z): STONE for x in range(-2, 3) for z in range(-2, 2)}
        solids.update({(0, 63, z): STONE for z in range(2, 10)})
        goal = (.5, 64., 9.65)
    elif family == "column_top":
        solids = floor
        solids.update({(x, 64, z): STONE for x in (3, 4) for z in (9, 10)})
        goal = (4.7, 65., 10.7)
    elif family == "cliff_corner":
        solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 7)}
        goal = (4.7, 64., 6.7)
    elif family == "holes":
        holes = {(-2, 3), (1, 4), (2, 8), (4, 9)}
        solids = {p: material for p, material in floor.items() if (p[0], p[2]) not in holes}
        goal = (3.7, 64., 9.7)
    elif family == "slab_edge":
        solids = floor
        solids[(4, 64, 10)] = SLAB
        goal = (4.7, 64.5, 10.7)
    elif family == "stair_edge":
        solids = floor
        solids[(4, 64, 10)] = STAIR
        goal = (4.7, 65., 10.7)
    elif family == "obstacle_support":
        solids = floor
        solids.update({(3, y, 10): STONE for y in (64, 65, 66)})
        goal = (4.5, 64., 10.5)
    elif family == "cross_piece":
        solids = {(x, 63, z): STONE for x in range(-2, 3) for z in range(-2, 3)}
        solids.update({(1, y, 1): STONE for y in (64, 65, 66)})
        start = (.5, 64., -.5)
        goal = (.8, 64., .6)
        volume = ((-4, 5), (60, 68), (-4, 5))
    else:
        raise ValueError(family)
    return solids, start, goal, volume


def _rotate_volume(volume: tuple, direction: str) -> tuple:
    corners = [
        _rotate_cell((x, volume[1][0], z), direction)
        for x in (volume[0][0], volume[0][1])
        for z in (volume[2][0], volume[2][1])
    ]
    return (
        (min(p[0] for p in corners), max(p[0] for p in corners)),
        tuple(volume[1]),
        (min(p[2] for p in corners), max(p[2] for p in corners)),
    )


def support_region_cases(config: dict | None = None) -> tuple[dict, ...]:
    config = frozen_manifest()["support_region_inputs"] if config is None else config
    rows = []
    for family in config["families"]:
        for target in config["targets"]:
            for direction in config["directions"]:
                for condition in config["conditions"]:
                    rows.append({
                        "id": f"f2s/{family}/{target}/{direction}/{condition}",
                        "family": family,
                        "target": target,
                        "direction": direction,
                        "condition": condition,
                        "seed": config["seeds"].get(family),
                        **_serialized_scene(family, target, direction, config["seeds"].get(family)),
                        "expected_output_fields": [
                            "standable_point_exists",
                            "standable_point_exists_but_task_failed",
                        ],
                    })
    return tuple(rows)


def _serialized_scene(family: str, target: str, direction: str, seed: int) -> dict:
    solids, start, position, volume = _base_scene(family, seed)
    rotated_solids = {_rotate_cell(p, direction): material for p, material in solids.items()}
    rotated_start = _rotate_point(start, direction)
    rotated_goal = _rotate_point(position, direction)
    goal = goal_for(rotated_goal, target)
    return {
        "start": list(rotated_start),
        "goal": list(rotated_goal),
        "goal_box": list(goal.region.as_tuple()),
        "yaw_degrees": _YAW_BY_DIRECTION[direction],
        "solids": [[*position, material] for position, material in sorted(rotated_solids.items())],
        "volume": _rotate_volume(volume, direction),
        "max_ticks": 400,
    }


def scene_for(case: dict) -> Scene:
    return Scene(
        {tuple(row[:3]): row[3] for row in case["solids"]},
        tuple(tuple(axis) for axis in case["volume"]),
    )


def materialized_manifest() -> dict:
    manifest = frozen_manifest()
    if (manifest.get("generator_id"), manifest.get("generator_version")) != (
        "mc2p.f2s-support-region-inputs", 1
    ):
        raise ValueError("unsupported F2-S input generator")
    base_path = MANIFEST.parent / manifest["base_manifest"]
    if hashlib.sha256(base_path.read_bytes()).hexdigest() != manifest["base_manifest_sha256"]:
        raise ValueError("frozen v8 parameter file changed")
    base = v8_manifest()
    cases = support_region_cases(manifest["support_region_inputs"])
    expected = manifest["expected_inputs"]
    if len(cases) != expected["support_region_cases"]:
        raise ValueError("F2-S support-region case count changed")
    if input_digest(cases) != expected["support_region_input_sha256"]:
        raise ValueError("F2-S support-region inputs changed")
    if len(base["tasks"]) != expected["v8_geometry_cases"]:
        raise ValueError("F2-S no longer preserves the v8 geometry inputs")
    if len(base["clutter_scan"]) != expected["v8_clutter_cases"]:
        raise ValueError("F2-S no longer preserves the v8 clutter inputs")
    manifest["v8_tasks"] = base["tasks"]
    manifest["v8_clutter_scan"] = base["clutter_scan"]
    manifest["support_region_cases"] = list(cases)
    return manifest


def validate_taxonomy() -> None:
    manifest = frozen_manifest()
    config = manifest["support_region_inputs"]
    if tuple(config["families"]) != FAMILIES:
        raise ValueError("F2-S family order changed")
    if tuple(config["targets"]) != TARGETS:
        raise ValueError("F2-S target order changed")
    if tuple(config["directions"]) != DIRECTIONS:
        raise ValueError("F2-S direction order changed")
    if tuple(config["conditions"]) != CONDITIONS:
        raise ValueError("F2-S condition order changed")
