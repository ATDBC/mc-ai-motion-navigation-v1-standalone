"""Deterministic Fabric fixtures for profile-4 observation cost measurement."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.task import (
    ComparisonOperatorV0,
    SuccessCriterionV0,
    TaskIntentV0,
)
from scripts.control_probe_core import append_jsonl, write_json_atomic


@dataclass(frozen=True)
class SurfaceCostScene:
    name: str
    x: int
    y: int
    z: int
    yaw: float
    pitch: float
    commands: tuple[str, ...]
    frames: int = 200
    warmup_frames: int = 30


def _base(x: int, z: int, *, y: int = 70) -> list[str]:
    return [
        f"forceload add {x - 24} {z - 24} {x + 24} {z + 24}",
        f"fill {x - 20} {y - 18} {z - 20} {x + 20} {y - 2} {z + 20} minecraft:air replace",
        f"fill {x - 20} {y - 1} {z - 20} {x + 20} {y + 14} {z + 20} minecraft:air replace",
        f"fill {x - 20} {y + 15} {z - 20} {x + 20} {y + 20} {z + 20} minecraft:air replace",
        f"fill {x - 20} {y - 1} {z - 20} {x + 20} {y - 1} {z + 20} minecraft:stone replace",
    ]


def _forest(x: int, z: int) -> tuple[str, ...]:
    commands = _base(x, z)
    for dx, dz, height in (
        (-8, 5, 5), (-3, 8, 6), (3, 6, 5), (8, 10, 7),
        (-10, 13, 6), (0, 14, 7), (7, 16, 5), (12, 7, 6),
    ):
        commands.extend((
            f"fill {x + dx} 70 {z + dz} {x + dx} {69 + height} {z + dz} minecraft:oak_log replace",
            f"fill {x + dx - 2} {68 + height} {z + dz - 2} {x + dx + 2} {71 + height} {z + dz + 2} minecraft:oak_leaves[persistent=true] replace",
        ))
    return tuple(commands)


def _slope(x: int, z: int) -> tuple[str, ...]:
    commands = _base(x, z)
    for step in range(1, 9):
        commands.append(
            f"fill {x - 16} 70 {z + step * 2} {x + 16} {69 + step} {z + step * 2 + 1} minecraft:stone replace"
        )
    return tuple(commands)


def _canyon(x: int, z: int) -> tuple[str, ...]:
    commands = _base(x, z)
    commands.extend((
        f"fill {x - 18} 50 {z + 6} {x + 18} 69 {z + 13} minecraft:air replace",
        f"fill {x - 18} 50 {z + 14} {x + 18} 69 {z + 20} minecraft:stone replace",
    ))
    return tuple(commands)


def _cave(x: int, z: int) -> tuple[str, ...]:
    commands = _base(x, z)
    commands.extend((
        f"fill {x - 16} 70 {z + 7} {x + 16} 84 {z + 20} minecraft:stone replace",
        f"fill {x - 2} 70 {z + 7} {x + 2} 73 {z + 20} minecraft:air replace",
    ))
    return tuple(commands)


def _tunnel(x: int, z: int) -> tuple[str, ...]:
    # Split the solid volume so each /fill remains below Minecraft's limit.
    return (
        f"forceload add {x - 24} {z - 24} {x + 24} {z + 24}",
        f"fill {x - 18} 52 {z - 18} {x + 18} 68 {z + 18} minecraft:stone replace",
        f"fill {x - 18} 69 {z - 18} {x + 18} 86 {z + 18} minecraft:stone replace",
        f"fill {x} 70 {z - 18} {x} 71 {z + 18} minecraft:air replace",
    )


def _fluid_edge(x: int, z: int, material: str) -> tuple[str, ...]:
    commands = _base(x, z)
    commands.extend((
        f"fill {x - 18} 69 {z + 5} {x + 18} 69 {z + 20} minecraft:stone replace",
        f"fill {x - 18} 70 {z + 5} {x + 18} 70 {z + 20} {material} replace",
    ))
    return tuple(commands)


SCENES = (
    SurfaceCostScene("forest", 0, 70, 0, 0.0, 0.0, _forest(0, 0)),
    SurfaceCostScene("slope", 80, 70, 0, 0.0, -8.0, _slope(80, 0)),
    SurfaceCostScene("canyon_edge", 160, 70, 0, 0.0, 12.0, _canyon(160, 0)),
    SurfaceCostScene("cave_entrance", 240, 70, 0, 0.0, 0.0, _cave(240, 0)),
    SurfaceCostScene("underground_1x2", 320, 70, 0, 0.0, 0.0, _tunnel(320, 0)),
    SurfaceCostScene("water_edge", 400, 70, 0, 0.0, 8.0, _fluid_edge(400, 0, "minecraft:water")),
    SurfaceCostScene("lava_edge", 480, 70, 0, 0.0, 8.0, _fluid_edge(480, 0, "minecraft:lava")),
)


def summarize_scene_rows(rows: list[dict]) -> dict:
    from scripts.probe_fabric_deployment_observation import summarize_observation_pipeline

    measured = [
        row for row in rows
        if row.get("measured", True) and type(row.get("scene")) is str
    ]
    names = sorted({row["scene"] for row in measured})
    return {
        "schema_version": "mc2p.surface-cost-report.v1",
        "scenes": {
            name: summarize_observation_pipeline([
                row for row in measured if row["scene"] == name
            ])
            for name in names
        },
    }


def run_surface_cost_runtime(
        runtime, backend, episode: str, directory: Path, deadline: int,
        *, fixture_writer, diagnostics_builder) -> tuple[dict, list[dict], list[dict]]:
    task = TaskIntentV0(
        "surface-cost-probe", "stationary_surface_cost", "{}",
        (SuccessCriterionV0(
            "stationary_observation", ComparisonOperatorV0.GREATER_THAN, 0.0, "frames",
        ),),
        2_000, deadline, True, 0.0,
    )
    profile = BehaviorProfileV0()
    rows: list[dict] = []
    stage_rows = []
    initial_row = {
        "episode_id": episode,
        "observation_sequence_id": runtime.observation.sequence_id,
        "diagnostics": backend.last_diagnostics,
        "observation_pipeline": diagnostics_builder(runtime, backend),
        "measured": False,
    }
    rows.append(initial_row)
    append_jsonl(directory / "diagnostics.jsonl", initial_row)
    for scene in SCENES:
        if time.perf_counter_ns() >= deadline:
            raise TimeoutError("surface cost probe deadline exceeded")
        fixture_writer(scene)
        # Let chunk, block-state and surface caches settle before measurement.
        for warmup_frame in range(scene.warmup_frames):
            result = runtime.step(
                task, profile,
                min(deadline, time.perf_counter_ns() + 5_000_000_000),
            )
            if result.observation is None or result.report.status.value != "running":
                raise RuntimeError(f"surface cost warmup failed: {result.report}")
            row = {
                "episode_id": episode,
                "scene": scene.name,
                "scene_frame": warmup_frame,
                "measured": False,
                "observation_sequence_id": runtime.observation.sequence_id,
                "diagnostics": backend.last_diagnostics,
                "observation_pipeline": diagnostics_builder(runtime, backend),
            }
            rows.append(row)
            append_jsonl(directory / "diagnostics.jsonl", row)
        for frame in range(scene.frames):
            result = runtime.step(
                task, profile,
                min(deadline, time.perf_counter_ns() + 5_000_000_000),
            )
            if result.observation is None or result.report.status.value != "running":
                raise RuntimeError(f"surface cost sample failed: {result.report}")
            row = {
                "episode_id": episode,
                "scene": scene.name,
                "scene_frame": frame,
                "measured": True,
                "observation_sequence_id": runtime.observation.sequence_id,
                "diagnostics": backend.last_diagnostics,
                "observation_pipeline": diagnostics_builder(runtime, backend),
            }
            rows.append(row)
            append_jsonl(directory / "diagnostics.jsonl", row)
        stage_rows.append({
            "scene": scene.name,
            "frames": scene.frames,
            "warmup_frames": scene.warmup_frames,
            "position": [scene.x + .5, scene.y, scene.z + .5],
            "yaw": scene.yaw,
            "pitch": scene.pitch,
        })
    report = summarize_scene_rows(rows)
    write_json_atomic(directory / "surface-cost-report.json", report)
    checks = [
        {"name": "surface_cost_has_seven_scenes", "passed": len(report["scenes"]) == 7},
        {"name": "surface_cost_has_200_frames_per_scene", "passed": all(
            value["frame_count"] == 200 for value in report["scenes"].values()
        )},
        {"name": "surface_cost_profile4_pack_p95_within_8ms", "passed": all(
            value["metrics"].get("surface_pack_ns", {}).get("p95", 8_000_001) <= 8_000_000
            for value in report["scenes"].values()
        )},
    ]
    return {"scenes": stage_rows, "report": report}, rows, checks
