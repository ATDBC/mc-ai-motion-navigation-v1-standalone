"""Fabric-only repeated B05 JumpUp trials and cancellation evidence."""
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
from mc2p.motion_nav.jump_up import (
    JumpUpController, JumpUpState, load_jump_up_profile,
)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fixed_route_runtime_core import receipt_confirms_input


ROOT = Path(__file__).resolve().parents[1]
_SOURCE = "b05-jump-acceptance"
_DIRECTIONS = ((0, 1, 0.0), (1, 0, -90.0), (0, -1, 180.0), (-1, 0, 90.0))


def run_jump_up_acceptance_runtime(
        runtime, backend, episode: str, directory: Path, deadline_ns: int,
        fixture_writer: Callable[[tuple[BlockPos, ...], str], None],
        player_teleporter: Callable[[float, float, float, float, float], None],
) -> tuple[dict, list[dict], list[dict]]:
    task = TaskIntentV0(
        "b05-jump-acceptance", "repeated_jump_up", "{}",
        (SuccessCriterionV0("valid_jumps", ComparisonOperatorV0.EQUAL, 100, "count"),),
        4000, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    adapter = NavigationObservationAdapter()
    frame = adapter.ingest(runtime.observation)
    rows: list[dict] = []
    counter = 0

    def diagnostic() -> None:
        row = dict(
            episode_id=episode,
            observation_sequence_id=runtime.observation.sequence_id,
            diagnostics=backend.last_diagnostics,
        )
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    def step(movement: MovementV1 = MovementV1(), *, look: LookV1 | None = None,
             request: ObservationRequestV3 | None = None):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"b05-acceptance-{counter}", _SOURCE, episode,
            runtime.observation.sequence_id, ActionPriorityV0.TASK,
            now, min(deadline_ns, now + 750_000_000), movement=movement,
            look=look, valid_for_ticks=1,
        ))
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B05 acceptance Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B05 acceptance movement bypassed the formal arbiter")
        frame = adapter.ingest(result.observation)
        diagnostic()
        return result

    diagnostic()
    profile = load_jump_up_profile(ROOT / "config/motion-navigation/jump-up-v1.json")
    feet_y = math.floor(frame.body.position[1] + 1e-6)
    center_x = math.floor(frame.body.position[0]) + .5
    center_z = math.floor(frame.body.position[2]) + .5
    center_cell = (math.floor(center_x), feet_y, math.floor(center_z))
    floor = ((center_cell[0], feet_y - 1, center_cell[2]),)
    platforms = tuple(
        (center_cell[0] + dx, feet_y, center_cell[2] + dz)
        for dx, dz, _ in _DIRECTIONS
    )
    buried_air = tuple((x, feet_y - 1, z) for x, _, z in platforms)
    volume = tuple(
        (x, y, z)
        for x in range(center_cell[0] - 1, center_cell[0] + 2)
        for z in range(center_cell[2] - 1, center_cell[2] + 2)
        for y in range(feet_y, feet_y + 4)
        if (x, y, z) not in set(platforms)
    )
    fixture_writer(floor + platforms, "minecraft:grass_block")
    fixture_writer(buried_air + volume, "minecraft:air")
    air_request = ObservationRequestV3("navigation_v1", buried_air + volume)

    def teleport(x: float, z: float, yaw: float) -> None:
        nonlocal frame
        player_teleporter(x, float(feet_y), z, yaw, 0.0)
        for _ in range(12):
            step(request=air_request)
            if (math.hypot(frame.body.position[0] - x, frame.body.position[2] - z) <= .02
                    and abs(frame.body.position[1] - feet_y) <= .02
                    and frame.body.is_on_ground):
                return
        raise RuntimeError("B05 acceptance teleport did not settle on the start cell")

    # Establish the solid facts through ordinary first-hit visibility. Air is
    # requested through the bounded positive-only navigation query.
    for x, y, z in floor + platforms:
        target_yaw = math.degrees(math.atan2(-(x + .5 - center_x), z + .5 - center_z))
        for _ in range(5):
            current_yaw = math.degrees(frame.body.yaw_radians)
            current_pitch = math.degrees(frame.body.pitch_radians)
            yaw_delta = (target_yaw - current_yaw + 180.0) % 360.0 - 180.0
            step(look=LookV1(yaw_delta, 65.0 - current_pitch), request=air_request)
            if frame.world.cell((x, y, z)).knowledge is CellKnowledge.BLOCK:
                break
        else:
            raise RuntimeError(f"B05 acceptance support was not observed: {(x, y, z)}")

    offsets = (
        (0.00, 0.00, False),
        (0.08, 0.00, False),
        (0.15, 0.00, False),
        (0.08, 0.16, False),
        (0.21, -0.05, True),
    )
    trials: list[dict] = []
    control_times_ns: list[int] = []
    for direction_index, (dx, dz, yaw) in enumerate(_DIRECTIONS):
        right_x, right_z = dz, -dx
        start_node = center_cell
        end_node = (center_cell[0] + dx, feet_y + 1, center_cell[2] + dz)
        for repeat in range(5):
            for offset_index, (back, lateral, moving_entry) in enumerate(offsets):
                trial_index = len(trials)
                x = center_x - dx * back + right_x * lateral
                z = center_z - dz * back + right_z * lateral
                teleport(x, z, yaw)
                if moving_entry:
                    step(MovementV1(forward=1), request=air_request)
                    for _ in range(12):
                        speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                                           frame.body.velocity_blocks_per_second[2])
                        if speed <= profile.maximum_entry_speed_blocks_per_second:
                            break
                        step(request=air_request)
                    else:
                        raise RuntimeError("B05 moving entry did not enter calibrated speed")

                entry = frame.body.position
                entry_speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                                         frame.body.velocity_blocks_per_second[2])
                controller = JumpUpController(profile)
                controller.start(start_node, end_node, frame)
                input_confirmed = True
                airborne = False
                jump_pulses = 0
                decision_rows = []
                for _ in range(40):
                    decision = controller.decide(frame, input_confirmed=input_confirmed)
                    control_times_ns.append(decision.control_time_ns)
                    jump_pulses += int(decision.movement.jump)
                    airborne = airborne or not frame.body.is_on_ground
                    decision_rows.append(dict(
                        sequence=frame.body.sequence_id,
                        state=decision.state.value,
                        reason=decision.reason_code,
                        position=list(frame.body.position),
                        velocity=list(frame.body.velocity_blocks_per_second),
                        movement=asdict(decision.movement),
                    ))
                    if decision.state is JumpUpState.COMPLETE:
                        break
                    if decision.state in {
                        JumpUpState.BLOCKED, JumpUpState.FAILED,
                        JumpUpState.NEEDS_INFORMATION, JumpUpState.UNSUPPORTED,
                    }:
                        raise RuntimeError(
                            f"B05 valid trial {trial_index} failed: "
                            f"{decision.state.value}/{decision.reason_code}"
                        )
                    result = step(decision.movement, look=decision.look, request=air_request)
                    input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
                else:
                    raise TimeoutError(f"B05 valid trial {trial_index} exceeded 40 frames")
                landing_error = math.hypot(
                    frame.body.position[0] - (end_node[0] + .5),
                    frame.body.position[2] - (end_node[2] + .5),
                )
                trial = dict(
                    trial=trial_index, direction_index=direction_index,
                    repeat=repeat, offset_index=offset_index,
                    requested_back_offset=back,
                    requested_lateral_offset=lateral,
                    moving_entry=moving_entry,
                    entry_position=list(entry), entry_speed=entry_speed,
                    airborne=airborne, jump_pulses=jump_pulses,
                    landing_position=list(frame.body.position),
                    landing_error=landing_error,
                    decisions=decision_rows,
                )
                trials.append(trial)
                append_jsonl(directory / "b05-jump-trials.jsonl", trial)

    def run_cancel_case(phase: JumpUpState) -> dict:
        teleport(center_x, center_z, 0.0)
        start_node = center_cell
        end_node = (center_cell[0], feet_y + 1, center_cell[2] + 1)
        controller = JumpUpController(profile)
        controller.start(start_node, end_node, frame)
        input_confirmed = True
        injected_sequence: int | None = None
        history: list[dict] = []
        for _ in range(40):
            if phase is JumpUpState.PREPARE and injected_sequence is None:
                controller.cancel()
                injected_sequence = frame.body.sequence_id
            decision = controller.decide(frame, input_confirmed=input_confirmed)
            control_times_ns.append(decision.control_time_ns)
            history.append(dict(
                sequence=frame.body.sequence_id,
                state=decision.state.value,
                reason=decision.reason_code,
                position=list(frame.body.position),
                velocity=list(frame.body.velocity_blocks_per_second),
                on_ground=frame.body.is_on_ground,
                movement=asdict(decision.movement),
            ))
            if phase is JumpUpState.REQUEST_TAKEOFF and injected_sequence is None:
                if decision.state is not JumpUpState.REQUEST_TAKEOFF:
                    raise RuntimeError("B05 REQUEST_TAKEOFF cancellation state was not reached")
                # Dispatch the takeoff request first.  Cancellation must handle
                # the real window where input was accepted but the next body
                # observation may still be grounded and moving.
                result = step(decision.movement, look=decision.look, request=air_request)
                input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
                controller.cancel()
                injected_sequence = frame.body.sequence_id
                continue
            if (phase is JumpUpState.AIRBORNE and injected_sequence is None
                    and decision.state is JumpUpState.AIRBORNE):
                controller.cancel()
                injected_sequence = frame.body.sequence_id
            if (phase is JumpUpState.VERIFY_LANDING and injected_sequence is None
                    and decision.state is JumpUpState.VERIFY_LANDING):
                controller.cancel()
                injected_sequence = frame.body.sequence_id
            if decision.state is JumpUpState.CANCELLED:
                result = dict(
                    requested_phase=phase.value,
                    injected_sequence=injected_sequence,
                    terminal_sequence=frame.body.sequence_id,
                    terminal_state=decision.state.value,
                    terminal_on_ground=frame.body.is_on_ground,
                    terminal_position=list(frame.body.position),
                    history=history,
                )
                append_jsonl(directory / "b05-jump-cancellations.jsonl", result)
                return result
            if decision.state in {
                JumpUpState.BLOCKED, JumpUpState.FAILED,
                JumpUpState.NEEDS_INFORMATION, JumpUpState.UNSUPPORTED,
                JumpUpState.COMPLETE,
            }:
                raise RuntimeError(
                    f"B05 {phase.value} cancellation ended as {decision.state.value}/"
                    f"{decision.reason_code}"
                )
            result = step(decision.movement, look=decision.look, request=air_request)
            input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        raise TimeoutError(f"B05 {phase.value} cancellation exceeded 40 frames")

    cancellation_cases = [
        run_cancel_case(phase)
        for phase in (
            JumpUpState.PREPARE,
            JumpUpState.REQUEST_TAKEOFF,
            JumpUpState.AIRBORNE,
            JumpUpState.VERIFY_LANDING,
        )
    ]

    def look_at_cell(position: BlockPos, material: str) -> None:
        target_x, target_y, target_z = (position[0] + .5, position[1] + .5, position[2] + .5)
        for _ in range(6):
            dx = target_x - frame.body.position[0]
            dy = target_y - (frame.body.position[1] + 1.62)
            dz = target_z - frame.body.position[2]
            target_yaw = math.degrees(math.atan2(-dx, dz))
            target_pitch = -math.degrees(math.atan2(dy, max(1.0e-6, math.hypot(dx, dz))))
            yaw = math.degrees(frame.body.yaw_radians)
            pitch = math.degrees(frame.body.pitch_radians)
            yaw_delta = (target_yaw - yaw + 180.0) % 360.0 - 180.0
            step(look=LookV1(yaw_delta, target_pitch - pitch), request=air_request)
            fact = frame.world.cell(position)
            if (fact.knowledge is CellKnowledge.BLOCK and fact.block is not None
                    and fact.block.material_key == material):
                return
        raise RuntimeError(f"B05 rejection fixture was not observed: {position}/{material}")

    rejection_cases: list[dict] = []
    north_end = (center_cell[0], feet_y + 1, center_cell[2] + 1)

    teleport(center_x, center_z, 0.0)
    step(MovementV1(forward=1), request=air_request)
    fast = JumpUpController(profile)
    fast.start(center_cell, north_end, frame)
    decision = fast.decide(frame)
    control_times_ns.append(decision.control_time_ns)
    rejection_cases.append(dict(case="entry_speed", state=decision.state.value,
                                reason=decision.reason_code))

    teleport(center_x, center_z, 0.0)
    ceiling = (center_cell[0], feet_y + 2, center_cell[2])
    fixture_writer((ceiling,), "minecraft:grass_block")
    look_at_cell(ceiling, "minecraft:grass_block")
    low_ceiling = JumpUpController(profile)
    low_ceiling.start(center_cell, north_end, frame)
    decision = low_ceiling.decide(frame)
    control_times_ns.append(decision.control_time_ns)
    rejection_cases.append(dict(case="low_ceiling", state=decision.state.value,
                                reason=decision.reason_code))
    fixture_writer((ceiling,), "minecraft:air")
    for _ in range(2):
        step(request=air_request)

    teleport(center_x, center_z, 0.0)
    target_support = (center_cell[0], feet_y, center_cell[2] + 1)
    fixture_writer((target_support,), "minecraft:stone")
    look_at_cell(target_support, "minecraft:stone")
    unsupported = JumpUpController(profile)
    unsupported.start(center_cell, north_end, frame)
    decision = unsupported.decide(frame)
    control_times_ns.append(decision.control_time_ns)
    rejection_cases.append(dict(case="unsupported_material", state=decision.state.value,
                                reason=decision.reason_code))
    fixture_writer((target_support,), "minecraft:grass_block")
    look_at_cell(target_support, "minecraft:grass_block")

    remote_start = (center_cell[0] + 40, feet_y, center_cell[2])
    unknown = JumpUpController(profile)
    unknown.start(remote_start, (remote_start[0], feet_y + 1, remote_start[2] + 1), frame)
    decision = unknown.decide(frame)
    control_times_ns.append(decision.control_time_ns)
    rejection_cases.append(dict(case="unknown_geometry", state=decision.state.value,
                                reason=decision.reason_code,
                                missing_count=len(decision.missing_cells)))

    teleport(center_x, center_z, 0.0)
    no_takeoff = JumpUpController(profile)
    no_takeoff.start(center_cell, north_end, frame)
    decision = no_takeoff.decide(frame)
    control_times_ns.append(decision.control_time_ns)
    if decision.state is not JumpUpState.REQUEST_TAKEOFF:
        raise RuntimeError("B05 no-takeoff rejection did not request takeoff")
    for _ in range(profile.maximum_takeoff_wait_ticks + 1):
        step(request=air_request)
        decision = no_takeoff.decide(frame, input_confirmed=True)
        control_times_ns.append(decision.control_time_ns)
        if decision.state is JumpUpState.FAILED:
            break
    rejection_cases.append(dict(case="takeoff_not_observed", state=decision.state.value,
                                reason=decision.reason_code))

    ordered_control = sorted(control_times_ns)
    def percentile(fraction: float) -> int:
        return ordered_control[max(0, math.ceil(len(ordered_control) * fraction) - 1)]

    evidence = dict(
        schema_version="mc2p.b05-jump-acceptance.v1",
        profile_id=profile.profile_id,
        center_cell=list(center_cell),
        trial_count=len(trials),
        success_count=sum(trial["airborne"] and trial["jump_pulses"] == 1
                          and trial["landing_error"] <= profile.landing_horizontal_radius_blocks
                          for trial in trials),
        maximum_landing_error=max(trial["landing_error"] for trial in trials),
        maximum_entry_speed=max(trial["entry_speed"] for trial in trials),
        control_sample_count=len(control_times_ns),
        control_p95_ns=percentile(.95),
        control_p99_ns=percentile(.99),
        maximum_control_ns=max(ordered_control),
        cancellation_cases=cancellation_cases,
        rejection_cases=rejection_cases,
        trials=trials,
    )
    write_json_atomic(directory / "b05-jump-acceptance.json", evidence)
    checks = [
        dict(name="b05_one_hundred_valid_jump_up_trials",
             passed=evidence["trial_count"] == evidence["success_count"] == 100),
        dict(name="b05_four_cardinal_directions_and_entry_variants",
             passed={trial["direction_index"] for trial in trials} == {0, 1, 2, 3}
             and any(trial["moving_entry"] for trial in trials)
             and any(trial["requested_lateral_offset"] != 0 for trial in trials)),
        dict(name="b05_every_trial_has_one_pulse_and_real_airborne",
             passed=all(trial["jump_pulses"] == 1 and trial["airborne"] for trial in trials)),
        dict(name="b05_batch_control_is_bounded",
             passed=evidence["control_p95_ns"] <= 8_000_000
             and evidence["control_p99_ns"] <= 15_000_000
             and evidence["maximum_control_ns"] < 30_000_000),
        dict(name="b05_four_action_phases_cancel_safely",
             passed=len(cancellation_cases) == 4
             and all(case["terminal_state"] == JumpUpState.CANCELLED.value
                     and case["terminal_on_ground"] for case in cancellation_cases)),
        dict(name="b05_invalid_or_unproven_actions_do_not_start",
             passed={case["case"] for case in rejection_cases} == {
                 "entry_speed", "low_ceiling", "unsupported_material",
                 "unknown_geometry", "takeoff_not_observed",
             }
             and all(case["state"] in {
                 JumpUpState.UNSUPPORTED.value, JumpUpState.BLOCKED.value,
                 JumpUpState.NEEDS_INFORMATION.value, JumpUpState.FAILED.value,
             } for case in rejection_cases)),
    ]
    return evidence, rows, checks
