"""Deterministic Fabric acceptance for same-frame profile-4 visual air."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import time

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import (
    ComparisonOperatorV0,
    SuccessCriterionV0,
    TaskIntentV0,
)
from scripts.control_probe_core import append_jsonl, write_json_atomic


BASE_X, BASE_Y, BASE_Z = 400, 65, 400


@dataclass(frozen=True, slots=True)
class VisualAirCase:
    name: str
    position: tuple[int, int, int]
    expected_air: bool


FIXED_CASES = (
    VisualAirCase("front_open", (400, 65, 403), True),
    VisualAirCase("behind_stone", (396, 65, 406), False),
    VisualAirCase("behind_glass", (398, 65, 406), True),
    VisualAirCase("barrier_visual_air", (400, 65, 406), True),
    VisualAirCase("behind_lava", (402, 65, 406), False),
    VisualAirCase("behind_fence", (404, 65, 406), False),
    VisualAirCase("behind_camera", (400, 65, 397), False),
    VisualAirCase("beyond_16", (400, 65, 417), False),
    VisualAirCase("view_edge_partial", (410, 65, 406), False),
)


def benchmark_positions(x: int, y: int, z: int) -> tuple[tuple[int, int, int], ...]:
    """128 whole cells that fit in the 120 degree, 16 block clear fixture."""
    return tuple(
        (x + dx, y + dy, z + dz)
        for dx in range(-3, 5)
        for dy in range(2)
        for dz in range(4, 12)
    )


def _percentile(values: list[int], percent: int) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)]


def evaluate_visual_air(
        *, benchmark_expected, benchmark_frames, fixed_confirmed,
        occupied_before_sources, removed_after_sources,
        contact_block_id, contact_sources) -> tuple[dict, list[dict]]:
    expected = tuple(sorted(benchmark_expected))
    benchmark_exact = bool(benchmark_frames) and all(
        tuple(sorted(frame["confirmed"])) == expected for frame in benchmark_frames
    )
    timings = [frame["air_query_ns"] for frame in benchmark_frames]
    expected_fixed = tuple(sorted(
        case.position for case in FIXED_CASES if case.expected_air
    ))
    report = {
        "schema_version": "mc2p.visual-air-report.v1",
        "benchmark_candidate_count": len(expected),
        "benchmark_frame_count": len(benchmark_frames),
        "benchmark_all_exact": benchmark_exact,
        "fixed_expected": [list(position) for position in expected_fixed],
        "fixed_confirmed": [list(position) for position in sorted(fixed_confirmed)],
        "air_query_ns": {
            "p50": _percentile(timings, 50),
            "p95": _percentile(timings, 95),
            "p99": _percentile(timings, 99),
            "maximum": max(timings),
        },
        "occupied_before_sources": list(occupied_before_sources),
        "removed_after_sources": list(removed_after_sources),
        "contact_block_id": contact_block_id,
        "contact_sources": list(contact_sources),
    }
    checks = [
        {"name": "visual_air_128_candidates_exact", "passed": benchmark_exact},
        {"name": "visual_air_fixed_cases_exact", "passed": tuple(sorted(fixed_confirmed)) == expected_fixed},
        {"name": "visible_block_prevents_air", "passed": "surface_depth" in occupied_before_sources and "air_query" not in occupied_before_sources},
        {"name": "removed_block_becomes_air", "passed": "air_query" in removed_after_sources},
        {"name": "barrier_contact_corrects_visual_air", "passed": contact_block_id == "minecraft:barrier" and "body_contact" in contact_sources},
        {"name": "visual_air_native_p95_within_1_5ms", "passed": report["air_query_ns"]["p95"] <= 1_500_000},
    ]
    return report, checks


def _clear_commands() -> tuple[str, ...]:
    return (
        "difficulty peaceful",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "time set noon",
        "weather clear",
        "gamemode creative MC2PProbe",
        "forceload add 376 376 424 424",
        "fill 376 48 376 424 60 424 minecraft:air replace",
        "fill 376 61 376 424 73 424 minecraft:air replace",
        "fill 376 74 376 424 84 424 minecraft:air replace",
        "fill 376 64 376 424 64 424 minecraft:stone replace",
        "tp MC2PProbe 400.5 65 400.5 0 0",
    )


FIXED_OBSTACLES = {
    # Occluders are aligned with the eye-to-candidate ray. Using the same
    # world x coordinate would leave an off-axis candidate visibly beside it.
    "behind_stone": (("fill 398 65 403 398 67 403 minecraft:stone replace",),
                     ((398, 66, 403), "minecraft:stone")),
    "behind_glass": (("fill 399 65 403 399 67 403 minecraft:glass replace",),
                     ((399, 66, 403), "minecraft:glass")),
    "barrier_visual_air": (("setblock 400 65 406 minecraft:barrier replace",), None),
    "behind_lava": (("fill 401 65 403 401 67 403 minecraft:lava replace",),
                    ((401, 66, 403), "minecraft:lava")),
    "behind_fence": (("fill 402 65 403 402 67 403 minecraft:oak_fence replace",),
                     ((402, 66, 403), "minecraft:oak_fence")),
}


def _case_commands(case: VisualAirCase, revision: int) -> tuple[tuple[str, ...], tuple[int, int, int], str]:
    material = "minecraft:gold_block" if revision % 2 else "minecraft:diamond_block"
    commands, _ = FIXED_OBSTACLES.get(case.name, ((), None))
    sync = (400, 69, 403)
    return (
        (
            "fill 390 65 401 410 70 412 minecraft:air replace",
            *commands,
            f"setblock {sync[0]} {sync[1]} {sync[2]} {material} replace",
        ),
        sync,
        material,
    )


def run_visual_air_runtime(
        runtime, backend, episode: str, directory: Path, deadline: int,
        *, fixture_writer, diagnostics_builder) -> tuple[dict, list[dict], list[dict]]:
    task = TaskIntentV0(
        "visual-air-probe", "stationary_visual_air", "{}",
        (SuccessCriterionV0(
            "visual_air_frames", ComparisonOperatorV0.GREATER_THAN, 0.0, "frames",
        ),),
        1_000, deadline, True, 0.0,
    )
    profile = BehaviorProfileV0()
    rows: list[dict] = [{
        "episode_id": episode,
        "label": "initial",
        "observation_sequence_id": runtime.observation.sequence_id,
        "diagnostics": backend.last_diagnostics,
        "observation_pipeline": diagnostics_builder(runtime, backend),
    }]
    append_jsonl(directory / "diagnostics.jsonl", rows[0])

    def step(label: str, request: ObservationRequestV3 | None = None):
        result = runtime.step(
            task, profile,
            min(deadline, time.perf_counter_ns() + 5_000_000_000),
            observation_request=request,
        )
        if result.observation is None or result.report.status.value != "running":
            raise RuntimeError(f"visual air {label} failed: {result.report}")
        row = {
            "episode_id": episode,
            "label": label,
            "observation_sequence_id": runtime.observation.sequence_id,
            "diagnostics": backend.last_diagnostics,
            "observation_pipeline": diagnostics_builder(runtime, backend),
        }
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)
        return runtime.observation, row

    def air_blocks(observation) -> dict[tuple[int, int, int], object]:
        return {
            block.position: block
            for block in observation.perception.value.blocks
            if "air_query" in block.sources
        }

    def wait_until(label: str, predicate, request=None, maximum: int = 60):
        for index in range(maximum):
            observation, row = step(f"{label}-wait-{index}", request)
            if predicate(observation):
                return observation, row
        raise RuntimeError(f"visual air fixture did not synchronize: {label}")

    def block_at(observation, position):
        return next(
            (block for block in observation.perception.value.blocks
             if block.position == position),
            None,
        )

    fixture_writer(_clear_commands(), "clear")
    for index in range(20):
        step(f"clear-warmup-{index}")

    benchmark = benchmark_positions(BASE_X, BASE_Y, BASE_Z)
    benchmark_request = ObservationRequestV3("navigation_v1", benchmark)
    benchmark_frames = []
    for index in range(100):
        observation, row = step(f"benchmark-{index}", benchmark_request)
        benchmark_frames.append({
            "confirmed": tuple(sorted(air_blocks(observation))),
            "air_query_ns": row["observation_pipeline"]["jvm"]["air_query_ns"],
        })

    fixed_confirmed = []
    revision = 1
    for case in FIXED_CASES:
        commands, sync_position, sync_material = _case_commands(case, revision)
        revision += 1
        fixture_writer(commands, "fixed-" + case.name)
        obstacle = FIXED_OBSTACLES.get(case.name, ((), None))[1]
        wait_until("fixed-" + case.name, lambda observation, obstacle=obstacle,
                   sync_position=sync_position, sync_material=sync_material: (
            (sync_block := block_at(observation, sync_position)) is not None
            and sync_block.block_id == sync_material
            and "surface_depth" in sync_block.sources
            and (obstacle is None or (
                (obstacle_block := block_at(observation, obstacle[0])) is not None
                and obstacle_block.block_id == obstacle[1]
                and "surface_depth" in obstacle_block.sources
            ))
        ))
        observation, _ = step(
            "fixed-result-" + case.name,
            ObservationRequestV3("navigation_v1", (case.position,)),
        )
        if case.position in air_blocks(observation):
            fixed_confirmed.append(case.position)
    fixed_confirmed = tuple(sorted(fixed_confirmed))

    changed = (400, 65, 404)
    sync = (400, 69, 403)
    fixture_writer((
        "fill 390 65 401 410 70 412 minecraft:air replace",
        "setblock 400 65 404 minecraft:stone replace",
        "setblock 400 69 403 minecraft:gold_block replace",
    ), "block-present")
    occupied, _ = wait_until(
        "block-present",
        lambda observation: (
            (block := block_at(observation, changed)) is not None
            and block.block_id == "minecraft:stone"
            and "surface_depth" in block.sources
            and (sync_block := block_at(observation, sync)) is not None
            and sync_block.block_id == "minecraft:gold_block"
        ),
        ObservationRequestV3("navigation_v1", (changed,)),
    )
    occupied_block = next(
        block for block in occupied.perception.value.blocks
        if block.position == changed
    )
    fixture_writer((
        "setblock 400 65 404 minecraft:air replace",
        "setblock 400 69 403 minecraft:diamond_block replace",
    ), "block-removed")
    removed, _ = wait_until(
        "block-removed",
        lambda observation: (
            (block := block_at(observation, changed)) is not None
            and block.block_id == "minecraft:air"
            and "air_query" in block.sources
            and (sync_block := block_at(observation, sync)) is not None
            and sync_block.block_id == "minecraft:diamond_block"
        ),
        ObservationRequestV3("navigation_v1", (changed,)),
    )
    removed_block = next(
        block for block in removed.perception.value.blocks
        if block.position == changed
    )

    barrier = next(case.position for case in FIXED_CASES if case.name == "barrier_visual_air")
    fixture_writer((
        "fill 390 65 401 410 70 412 minecraft:air replace",
        "setblock 400 65 406 minecraft:barrier replace",
        "setblock 400 69 403 minecraft:gold_block replace",
    ), "barrier-contact-ready")
    wait_until("barrier-contact-ready", lambda observation: (
        (sync_block := block_at(observation, sync)) is not None
        and sync_block.block_id == "minecraft:gold_block"
    ))
    fixture_writer(("tp MC2PProbe 400.5 66 406.5 0 0",), "barrier-contact")
    contact, _ = wait_until(
        "barrier-contact",
        lambda observation: (
            (block := block_at(observation, barrier)) is not None
            and block.block_id == "minecraft:barrier"
            and "body_contact" in block.sources
        ),
    )
    contact_block = next(
        block for block in contact.perception.value.blocks
        if block.position == barrier and "body_contact" in block.sources
    )

    report, checks = evaluate_visual_air(
        benchmark_expected=benchmark,
        benchmark_frames=benchmark_frames,
        fixed_confirmed=fixed_confirmed,
        occupied_before_sources=occupied_block.sources,
        removed_after_sources=removed_block.sources,
        contact_block_id=contact_block.block_id,
        contact_sources=contact_block.sources,
    )
    write_json_atomic(directory / "b02-air-query.json", report)
    stages = {
        "benchmark": {
            "candidate_count": len(benchmark),
            "frames": len(benchmark_frames),
        },
        "fixed_cases": report,
    }
    return stages, rows, checks
