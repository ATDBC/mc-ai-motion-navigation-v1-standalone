"""Fabric-only B05 probe for one low-speed adjacent one-block JumpUp."""
from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fixed_route_runtime_core import receipt_confirms_input


_SOURCE = "b05-jump-calibration"


def summarize_jump_trial(samples: list[dict], *, start_y: float, target_y: float,
                         target_center: tuple[float, float]) -> dict:
    if not samples:
        raise ValueError("jump calibration requires samples")
    takeoff = next((row for row in samples if row["is_on_ground"] is False), None)
    landing = None
    if takeoff is not None:
        takeoff_index = samples.index(takeoff)
        landing = next((row for row in samples[takeoff_index + 1:]
                        if row["is_on_ground"] is True), None)
    maximum_rise = max(float(row["position"][1]) - start_y for row in samples)
    error = None
    level_error = None
    if landing is not None:
        error = math.hypot(float(landing["position"][0]) - target_center[0],
                           float(landing["position"][2]) - target_center[1])
        level_error = abs(float(landing["position"][1]) - target_y)
    succeeded = bool(landing is not None and level_error is not None
                     and level_error <= .10 and error is not None and error <= .30)
    return {
        "takeoff_sequence": None if takeoff is None else takeoff["sequence"],
        "landing_sequence": None if landing is None else landing["sequence"],
        "maximum_rise_blocks": maximum_rise,
        "landing_horizontal_error_blocks": error,
        "landing_level_error_blocks": level_error,
        "succeeded": succeeded,
    }


def run_jump_up_calibration_runtime(
        runtime, backend, episode: str, directory: Path, deadline_ns: int,
        fixture_writer: Callable[[tuple[BlockPos, ...], str], None],
) -> tuple[dict, list[dict], list[dict]]:
    """Run one conservative real jump before freezing a B05 profile."""
    task = TaskIntentV0(
        "b05-jump-calibration", "jump_up_calibration", "{}",
        (SuccessCriterionV0("jump_up_calibrated", ComparisonOperatorV0.EQUAL, 1, "count"),),
        200, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    adapter = NavigationObservationAdapter()
    frame = adapter.ingest(runtime.observation)
    rows: list[dict] = []
    counter = 0

    def diagnostic() -> None:
        row = {
            "episode_id": episode,
            "observation_sequence_id": runtime.observation.sequence_id,
            "diagnostics": backend.last_diagnostics,
        }
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    def step(movement: MovementV1 = MovementV1(), *, look: LookV1 | None = None,
             request: ObservationRequestV3 | None = None, valid_for_ticks: int = 1):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"b05-calibration-{counter}", _SOURCE, episode,
            runtime.observation.sequence_id, ActionPriorityV0.TASK,
            now, min(deadline_ns, now + 750_000_000), movement=movement,
            look=look, valid_for_ticks=valid_for_ticks,
        ))
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B05 Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B05 movement bypassed the formal arbiter")
        frame = adapter.ingest(result.observation)
        diagnostic()
        return result

    def absolute_look(yaw_degrees: float, pitch_degrees: float,
                      request: ObservationRequestV3 | None = None):
        yaw = math.degrees(frame.body.yaw_radians)
        pitch = math.degrees(frame.body.pitch_radians)
        delta = (yaw_degrees - yaw + 180.0) % 360.0 - 180.0
        return step(look=LookV1(delta, pitch_degrees - pitch), request=request)

    diagnostic()
    feet_y = math.floor(frame.body.position[1] + 1e-6)
    start_cell = (math.floor(frame.body.position[0]), feet_y,
                  math.floor(frame.body.position[2]))
    platform = (start_cell[0], feet_y, start_cell[2] + 1)
    required_air = tuple(
        (start_cell[0], y, z)
        for z in (start_cell[2], start_cell[2] + 1)
        for y in range(feet_y + 1, feet_y + 4)
    )
    fixture_writer((platform,), "minecraft:grass_block")
    fixture_writer(required_air, "minecraft:air")
    air_request = ObservationRequestV3("navigation_v1", required_air)

    # The block itself must arrive through normal visibility. The positive air
    # request only confirms declared empty cells and cannot disclose a solid.
    for _ in range(8):
        absolute_look(0.0, 45.0, air_request)
        if (frame.world.cell(platform).knowledge is CellKnowledge.BLOCK
                and all(frame.world.cell(position).knowledge is CellKnowledge.AIR
                        for position in required_air)):
            break
    else:
        raise RuntimeError("B05 calibration fixture was not legally observed")

    absolute_look(0.0, 0.0, air_request)
    for _ in range(20):
        speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                           frame.body.velocity_blocks_per_second[2])
        if speed <= .03 and frame.body.is_on_ground:
            break
        step(request=air_request)
    else:
        raise RuntimeError("B05 calibration body did not settle before takeoff")

    start_position = frame.body.position
    target_center = (platform[0] + .5, platform[2] + .5)
    samples: list[dict] = []

    def record(movement: MovementV1, confirmed: bool) -> None:
        samples.append({
            "sequence": frame.body.sequence_id,
            "position": list(frame.body.position),
            "velocity_blocks_per_second": list(frame.body.velocity_blocks_per_second),
            "is_on_ground": frame.body.is_on_ground,
            "horizontal_collision": frame.body.horizontal_collision,
            "vertical_collision": frame.body.vertical_collision,
            "requested": asdict(movement),
            "input_confirmed": confirmed,
        })
        append_jsonl(directory / "b05-jump-samples.jsonl", samples[-1])

    record(MovementV1(), True)
    airborne = False
    landed = False
    for tick in range(40):
        horizontal = math.hypot(frame.body.position[0] - start_position[0],
                                frame.body.position[2] - start_position[2])
        movement = MovementV1(
            forward=1 if horizontal < .62 else 0,
            jump=tick == 0,
        )
        result = step(movement, request=air_request)
        confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        record(movement, confirmed)
        airborne = airborne or not frame.body.is_on_ground
        if (airborne and frame.body.is_on_ground
                and frame.body.position[1] >= feet_y + .9):
            landed = True
            for _ in range(6):
                result = step(request=air_request)
                record(MovementV1(), receipt_confirms_input(
                    result.backend_result.receipt.status))
            break
    summary = summarize_jump_trial(
        samples, start_y=start_position[1], target_y=float(feet_y + 1),
        target_center=target_center,
    )
    summary.update({
        "schema_version": "mc2p.b05-jump-calibration.v1",
        "start_position": list(start_position),
        "start_cell": list(start_cell),
        "platform": list(platform),
        "target_center": list(target_center),
        "sample_count": len(samples),
        "landed_on_upper_level": landed,
    })
    write_json_atomic(directory / "b05-jump-calibration.json", summary)
    checks = [
        {"name": "b05_observed_actual_takeoff", "passed": summary["takeoff_sequence"] is not None},
        {"name": "b05_landed_on_declared_upper_level", "passed": summary["succeeded"]},
        {"name": "b05_jump_input_was_bounded", "passed":
         sum(bool(row["requested"]["jump"]) for row in samples) == 1},
        {"name": "b05_no_horizontal_collision", "passed":
         not any(row["horizontal_collision"] for row in samples)},
    ]
    return summary, rows, checks
