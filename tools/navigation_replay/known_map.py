"""Convert B04 Fabric evidence into the replay viewer's stable data shape."""
from __future__ import annotations

import math


TICK_SECONDS = .05
TITLES = {
    "wall": "自动绕行 · 两格高墙",
    "floating": "自动绕行 · 第二层悬浮墙",
    "pit": "自动绕行 · 地面坑洞",
}


def _point(value):
    if isinstance(value, dict):
        return [float(value[axis]) for axis in ("x", "y", "z")]
    return [float(component) for component in value]


def _horizontal_path_length(positions):
    return sum(math.hypot(b[0] - a[0], b[2] - a[2])
               for a, b in zip(positions, positions[1:]))


def _sample_rows(decisions):
    first_sequence = decisions[0]["sequence"]
    positions = [_point(row["position"]) for row in decisions]
    samples = []
    prior_yaw = 0.0
    for index, (row, position) in enumerate(zip(decisions, positions)):
        previous = positions[max(0, index - 1)]
        following = positions[min(len(positions) - 1, index + 1)]
        elapsed_ticks = max(1, row["sequence"] - decisions[max(0, index - 1)]["sequence"])
        velocity = [
            (position[axis] - previous[axis]) / (elapsed_ticks * TICK_SECONDS)
            for axis in range(3)
        ] if index else [0.0, 0.0, 0.0]
        dx, dz = following[0] - position[0], following[2] - position[2]
        if math.hypot(dx, dz) > 1e-6:
            prior_yaw = math.degrees(math.atan2(-dx, dz))
        samples.append({
            "sequence": row["sequence"],
            "t": (row["sequence"] - first_sequence) * TICK_SECONDS,
            "position": position,
            "velocity": velocity,
            "yaw": prior_yaw,
            "pitch": 0.0,
        })
    return samples


def _layout(evidence, name):
    bounds = dict(evidence["bounds"])
    feet_y = int(bounds["feet_y"])
    obstacle_x = int(evidence["start"][0])
    obstacle_z = int(evidence["start"][2]) + 4
    obstacles = []
    pits = []
    if name == "wall":
        obstacles = [
            {"x": obstacle_x, "y": feet_y, "z": obstacle_z,
             "block": "minecraft:stone", "visual_kind": "wall"},
            {"x": obstacle_x, "y": feet_y + 1, "z": obstacle_z,
             "block": "minecraft:stone", "visual_kind": "wall"},
        ]
    elif name == "floating":
        obstacles = [{"x": obstacle_x, "y": feet_y + 1, "z": obstacle_z,
                      "block": "minecraft:stone", "visual_kind": "floating"}]
    elif name == "pit":
        pits = [{"x": obstacle_x, "y": feet_y - 1, "z": obstacle_z,
                 "block": "minecraft:air"}]
    return {
        "bounds": bounds,
        "ground_y": feet_y - 1,
        "obstacles": obstacles,
        "pit_cells": pits,
    }


def normalize_known_map_scenario(evidence, scenario):
    """Return one self-contained replay without inventing actor observations."""
    name = scenario["name"]
    if name not in TITLES:
        raise ValueError(f"unsupported B04 replay scenario: {name}")
    decisions = scenario.get("decisions", ())
    if not decisions:
        raise ValueError(f"B04 {name} has no decisions")
    samples = _sample_rows(decisions)
    graph = scenario.get("graph_path", ())
    start = samples[0]["position"]
    goal = ([float(graph[-1][0]) + .5, float(graph[-1][1]), float(graph[-1][2]) + .5]
            if graph else samples[-1]["position"])
    normalized_decisions = []
    for row, sample in zip(decisions, samples):
        normalized_decisions.append({
            "sequence": row["sequence"],
            "t": sample["t"],
            "state": row["state"],
            "reason": row["reason"],
            "movement": row["movement"],
            "progress_blocks": row.get("progress"),
            "planning_ms": scenario.get("planning_ms"),
            "target": goal,
        })
    return {
        "schema_version": "mc2p.known-map-replay.v1",
        "case": name,
        "title": TITLES[name],
        "kind": "known_map",
        "state": "succeeded" if decisions[-1]["state"] == "succeeded" else decisions[-1]["state"],
        "duration": samples[-1]["t"],
        "start": start,
        "goal": goal,
        "reference_path": [_point(point) for point in scenario.get("fixed_route", ())],
        "samples": samples,
        "decisions": normalized_decisions,
        "layout": _layout(evidence, name),
        "knowledge": [],
        "observations": [],
        "entities": [],
        "changes": [],
        "warnings": [],
        "memory": {"available": False, "imports_checked": 0,
                   "retained_in_radius": False},
        "navigation_volume": False,
        "current_history_batches": False,
        "map_note": ("底图按 B04 实机测试夹具还原；橙色虚线是后台规划并获准执行的固定路线，"
                     "蓝线是游戏中的实际身体轨迹。"),
        "summary": {
            "outcome": "success" if decisions[-1]["state"] == "succeeded" else "failed",
            "engineering": "valid",
            "path_length": _horizontal_path_length([sample["position"] for sample in samples]),
        },
        "planning": {
            "elapsed_ms": scenario.get("planning_ms"),
            "waiting_polls": scenario.get("waiting_polls"),
            "expanded_nodes": scenario.get("expanded_nodes"),
        },
    }
