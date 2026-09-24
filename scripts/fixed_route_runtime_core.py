"""Fabric-only B03 fixed-route runtime scenario and evidence checks."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import (
    ActionIntentV1, ClickSlotV1, CloseScreenV1, LookV1, MovementV1, OpenInventoryV1,
)
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.ground_motion import GroundMotionProfile
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge
from scripts.control_probe_core import append_jsonl, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
_SOURCE = "b03-fixed-route"
_ACCEPTED_RECEIPTS = frozenset({"executed", "confirmed_local"})


def receipt_confirms_input(status: str) -> bool:
    return status in _ACCEPTED_RECEIPTS


def _two_cell_band(value: float) -> tuple[int, int]:
    cell = math.floor(value)
    return (cell - 1, cell) if value - cell < .5 else (cell, cell + 1)


def l_corridor_wall_positions(x: float, feet_y: float, z: float, sign: int) -> tuple[BlockPos, ...]:
    """Build the two-block-high boundary of a two-cell-wide L corridor."""
    if sign not in {-1, 1}:
        raise ValueError("L corridor direction must be -1 or 1")
    start_x = _two_cell_band(x)
    start_z = _two_cell_band(z)
    turn_z = _two_cell_band(z + sign * 3)
    target_x = _two_cell_band(x + sign * 3)
    vertical_z = range(min(*start_z, *turn_z), max(*start_z, *turn_z) + 1)
    horizontal_x = range(min(*start_x, *target_x), max(*start_x, *target_x) + 1)
    free = ({(cell_x, cell_z) for cell_x in start_x for cell_z in vertical_z}
            | {(cell_x, cell_z) for cell_x in horizontal_x for cell_z in turn_z})
    boundary = {
        neighbor
        for cell_x, cell_z in free
        for neighbor in ((cell_x - 1, cell_z), (cell_x + 1, cell_z),
                         (cell_x, cell_z - 1), (cell_x, cell_z + 1))
        if neighbor not in free
    }
    entry_z = min(start_z) - 1 if sign > 0 else max(start_z) + 1
    exit_x = max(target_x) + 1 if sign > 0 else min(target_x) - 1
    boundary.difference_update((cell_x, entry_z) for cell_x in start_x)
    boundary.difference_update((exit_x, cell_z) for cell_z in turn_z)
    base_y = math.floor(feet_y)
    return tuple(sorted(
        (cell_x, block_y, cell_z)
        for cell_x, cell_z in boundary
        for block_y in (base_y, base_y + 1)
    ))


def _motion_profile() -> GroundMotionProfile:
    value = json.loads((ROOT / "config/motion-navigation/ordinary-ground-v1.json").read_text("utf-8"))
    profile = value["profile"]
    return GroundMotionProfile(
        float(value["scope"]["tick_seconds"]),
        float(profile["acceleration_blocks_per_second2"]),
        float(profile["velocity_retention_per_tick"]),
        float(profile["maximum_speed_blocks_per_second"]),
        frozenset(value["scope"]["support_materials"]),
    )


def run_fixed_route_runtime(runtime, backend, episode: str, directory: Path,
                            deadline_ns: int,
                            fixture_writer: Callable[[tuple[BlockPos, ...], str], None] | None = None,
                            ) -> tuple[dict, list[dict], list[dict]]:
    """Run 25 fixed routes per JVM plus cancel and input-failure checks."""
    task = TaskIntentV0(
        "b03-fixed-route", "fixed_route_walk", "{}",
        (SuccessCriterionV0("fixed_route_completed", ComparisonOperatorV0.EQUAL, 1, "boolean"),),
        200, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    adapter = NavigationObservationAdapter()
    frame = adapter.ingest(runtime.observation)
    rows: list[dict] = []
    trials: list[dict] = []
    counter = 0

    def diagnostic() -> None:
        observation = runtime.observation
        row = dict(episode_id=episode, observation_sequence_id=observation.sequence_id,
                   diagnostics=backend.last_diagnostics)
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    def step(movement: MovementV1 = MovementV1(), *, look: LookV1 | None = None,
             operation=None,
             valid_for_ticks: int = 2,
             observation_request: ObservationRequestV3 | None = None):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        expires = min(deadline_ns, now + 750_000_000)
        runtime.submit_intent(ActionIntentV1(
            f"b03-{counter}", _SOURCE, episode, runtime.observation.sequence_id,
            ActionPriorityV0.TASK, now, expires, movement=movement, look=look,
            operation=operation,
            valid_for_ticks=valid_for_ticks,
        ))
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=observation_request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B03 Fabric step failed: {result.report}")
        receipt = result.backend_result.receipt
        if (result.report.status.value != "running"
                and receipt_confirms_input(receipt.status)):
            raise RuntimeError(f"B03 Fabric step ended unexpectedly: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B03 movement was not selected by the formal arbiter")
        frame = adapter.ingest(result.observation)
        diagnostic()
        return result

    diagnostic()

    def synchronize_fixture(positions: tuple[BlockPos, ...], material: str) -> int:
        if fixture_writer is None:
            raise RuntimeError("fixture synchronization requires a writer")
        fixture_writer(positions, material)
        expected = CellKnowledge.AIR if material == "minecraft:air" else CellKnowledge.BLOCK
        request = ObservationRequestV3("navigation_v1", positions)
        for _ in range(5):
            step(valid_for_ticks=1, observation_request=request)
            matched = sum(
                frame.world.cell(position).knowledge is expected for position in positions
            )
            # Air queries are intentionally positive-only: a requested solid
            # is not disclosed merely because the test named its coordinate.
            # One naturally visible wall cell proves the asynchronous server
            # write has reached the client; the later route scan observes the
            # corridor surfaces it can actually use.  Clearing is stricter:
            # every former wall cell must be positively confirmed as air.
            if ((expected is CellKnowledge.BLOCK and matched > 0)
                    or (expected is CellKnowledge.AIR and matched == len(positions))):
                return matched
        raise RuntimeError(f"B03 fixture did not become {expected.value} within five observations")

    # Each JVM runs every deterministic scenario five times. The independent
    # second JVM brings every scenario to ten real runs without sharing client state.
    specifications = (
        ("S00_straight", 0.0, 0.0, (0.00,), ((0.0, 0.0), (0.0, 3.5))),
        ("S01_wide_turn", 90.0, 20.0, (0.00,), ((0.0, 0.0), (0.0, 3.0), (3.0, 3.0))),
        ("S02_narrow_turn", -45.0, 15.0, (0.00,),
         ((0.0, 0.0), (0.0, 3.0), (3.0, 3.0))),
        ("S03_view_angle", 135.0, 55.0, (0.00,), ((0.0, 0.0), (3.0, 0.0))),
        ("S07_cross_track", -90.0, 45.0, (0.02, 0.05, 0.10, 0.05, 0.02),
         ((0.0, 0.0), (0.0, 3.5))),
    )
    current_yaw_degrees = math.degrees(frame.body.yaw_radians)
    current_pitch_degrees = math.degrees(frame.body.pitch_radians)
    expanded = []
    for repetition in range(5):
        sign = 1.0 if repetition % 2 == 0 else -1.0
        for scenario, desired_yaw, desired_pitch, offsets, base_shape in specifications:
            shape = tuple((dx * sign, dz * sign) for dx, dz in base_shape)
            offset = offsets[repetition % len(offsets)]
            expanded.append((scenario, repetition, desired_yaw, desired_pitch, offset, shape))
    for trial_index, (scenario, repetition, desired_yaw, desired_pitch, offset, shape) in enumerate(expanded):
        corridor_walls: tuple[BlockPos, ...] = ()
        observed_corridor_walls = 0
        if scenario == "S02_narrow_turn":
            if fixture_writer is None:
                raise RuntimeError("S02 requires a physical L-corridor fixture writer")
            corridor_walls = l_corridor_wall_positions(
                frame.body.position[0], frame.body.position[1], frame.body.position[2],
                1 if shape[-1][0] > 0 else -1,
            )
            observed_corridor_walls = synchronize_fixture(corridor_walls, "minecraft:stone")
        # B03 tests execution over a supplied route, so establish the route's
        # ordinary-ground facts before choosing an arbitrary execution view.
        # This avoids turning a fixed-route test into an active-perception test.
        for first, second in zip(shape, shape[1:]):
            dx, dz = second[0] - first[0], second[1] - first[1]
            scan_yaw = math.degrees(math.atan2(-dx, dz))
            yaw_delta = (scan_yaw - current_yaw_degrees + 180.0) % 360.0 - 180.0
            step(look=LookV1(yaw_delta, 55.0 - current_pitch_degrees), valid_for_ticks=1)
            current_yaw_degrees = math.degrees(frame.body.yaw_radians)
            current_pitch_degrees = math.degrees(frame.body.pitch_radians)
        delta = (desired_yaw - current_yaw_degrees + 180.0) % 360.0 - 180.0
        pitch_delta = desired_pitch - current_pitch_degrees
        step(look=LookV1(delta, pitch_delta), valid_for_ticks=1)
        current_yaw_degrees = math.degrees(frame.body.yaw_radians)
        current_pitch_degrees = math.degrees(frame.body.pitch_radians)
        origin_x, origin_y, origin_z = frame.body.position
        first_dx, first_dz = shape[1][0] - shape[0][0], shape[1][1] - shape[0][1]
        first_length = math.hypot(first_dx, first_dz)
        route_origin_x = origin_x + (-first_dz / first_length) * offset
        route_origin_z = origin_z + (first_dx / first_length) * offset
        route = FixedRoute(
            f"{episode}-{scenario}-{repetition}",
            tuple(RoutePoint(route_origin_x + dx, origin_y, route_origin_z + dz) for dx, dz in shape),
        )
        controller = FixedRouteController(_motion_profile())
        controller.start(route, frame)
        started_ns = time.perf_counter_ns()
        low_run = 0
        maximum_low_run = 0
        moving_started = False
        control_times = []
        information_frames = 0
        input_confirmed = True
        start_sequence = frame.body.sequence_id
        for _ in range(180):
            decision = controller.decide(frame, input_confirmed=input_confirmed)
            control_times.append(decision.control_time_ns)
            goal = route.points[-1]
            speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                               frame.body.velocity_blocks_per_second[2])
            goal_distance = math.hypot(frame.body.position[0] - goal.x,
                                       frame.body.position[2] - goal.z)
            if speed > .2:
                moving_started = True
                low_run = 0
            elif moving_started and goal_distance > .8:
                low_run += 1
                maximum_low_run = max(maximum_low_run, low_run)
            else:
                low_run = 0
            request = None
            if decision.missing_cells:
                information_frames += 1
                request, _ = adapter.air_request(decision.missing_cells)
            if decision.state is FixedRouteState.SUCCEEDED:
                break
            if decision.state is FixedRouteState.INPUT_LOST:
                safe = step(MovementV1(), valid_for_ticks=1)
                raise RuntimeError(
                    f"B03 route {trial_index} lost input confirmation; neutral receipt "
                    f"was {safe.backend_result.receipt.status}"
                )
            if decision.state in {
                FixedRouteState.BLOCKED, FixedRouteState.FAILED, FixedRouteState.UNSUPPORTED,
            }:
                raise RuntimeError(f"B03 route {trial_index} ended as {decision.state}: {decision.reason}")
            result = step(decision.movement, valid_for_ticks=decision.input_lease_ticks,
                          observation_request=request)
            input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        else:
            raise TimeoutError(f"B03 route {trial_index} exceeded 180 control frames")
        final_speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                                 frame.body.velocity_blocks_per_second[2])
        final_error = math.hypot(frame.body.position[0] - route.points[-1].x,
                                 frame.body.position[2] - route.points[-1].z)
        trial = dict(
            route_id=route.route_id,
            scenario=scenario,
            repetition=repetition,
            points=[asdict(point) for point in route.points],
            yaw_degrees=current_yaw_degrees,
            pitch_degrees=current_pitch_degrees,
            physical_corridor_wall_blocks=len(corridor_walls),
            observed_corridor_wall_blocks=observed_corridor_walls,
            initial_offset_blocks=offset,
            control_frames=frame.body.sequence_id - start_sequence,
            information_frames=information_frames,
            maximum_middle_low_speed_frames=maximum_low_run,
            final_error_blocks=final_error,
            final_speed_blocks_per_second=final_speed,
            elapsed_seconds=(time.perf_counter_ns() - started_ns) / 1e9,
            control_time_p95_ms=sorted(control_times)[math.ceil(len(control_times) * .95) - 1] / 1e6,
            control_time_p99_ms=sorted(control_times)[math.ceil(len(control_times) * .99) - 1] / 1e6,
            control_time_max_ms=max(control_times) / 1e6,
        )
        trials.append(trial)
        append_jsonl(directory / "b03-trials.jsonl", trial)
        if corridor_walls:
            # The server fixture and navigation knowledge change together so
            # later trials cannot retain obsolete stone.
            synchronize_fixture(corridor_walls, "minecraft:air")
            base_y = min(position[1] for position in corridor_walls)
            restored_ground = tuple(sorted({
                (x, base_y - 1, z) for x, _, z in corridor_walls
            }))
            fixture_writer(restored_ground, "minecraft:grass_block")  # type: ignore[misc]
            # Opaque temporary walls can turn covered grass into dirt. Restore
            # that fixture side effect before another ordinary-ground route.
            step(valid_for_ticks=1)

    # Produce a real rejected client receipt with a stale GUI session. The
    # controller must consume that receipt as unconfirmed before neutral input.
    step(operation=OpenInventoryV1(), valid_for_ticks=1)
    stale_gui = runtime.observation.gui.value
    step(operation=CloseScreenV1(), valid_for_ticks=1)
    step(operation=OpenInventoryV1(), valid_for_ticks=1)
    rejected_result = step(operation=ClickSlotV1(
        stale_gui.gui_session_id, stale_gui.sync_id, stale_gui.revision, 0, 0, "pickup",
    ), valid_for_ticks=1)
    x, y, z = frame.body.position
    confirmation_controller = FixedRouteController(_motion_profile())
    confirmation_controller.start(FixedRoute(f"{episode}-confirmation-loss", (
        RoutePoint(x, y, z), RoutePoint(x, y, z + 2.0))), frame)
    confirmation_decision = confirmation_controller.decide(
        frame,
        input_confirmed=receipt_confirms_input(rejected_result.backend_result.receipt.status),
    )
    confirmation_result = step(confirmation_decision.movement, valid_for_ticks=1)
    step(operation=CloseScreenV1(), valid_for_ticks=1)
    confirmation_loss = dict(
        rejected_receipt_status=rejected_result.backend_result.receipt.status,
        rejected_report_status=rejected_result.report.status.value,
        state=confirmation_decision.state.value,
        movement=asdict(confirmation_decision.movement),
        reason=confirmation_decision.reason,
        neutral_receipt_status=confirmation_result.backend_result.receipt.status,
    )

    # Normal cancellation is accepted immediately but completes only after the
    # observed body is actually stopped.
    x, y, z = frame.body.position
    controller = FixedRouteController(_motion_profile())
    controller.start(FixedRoute(f"{episode}-cancel", (
        RoutePoint(x, y, z), RoutePoint(x, y, z + 6.0))), frame)
    for _ in range(40):
        decision = controller.decide(frame)
        request = None
        if decision.missing_cells:
            request, _ = adapter.air_request(decision.missing_cells)
        step(decision.movement, observation_request=request)
        speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                           frame.body.velocity_blocks_per_second[2])
        if speed >= 1.0:
            break
    else:
        raise TimeoutError("B03 cancellation setup did not reach a moving state")
    cancel_start = tuple(frame.body.position)
    controller.cancel()
    cancellation_frames = 0
    used_active_brake = False
    for _ in range(40):
        decision = controller.decide(frame)
        cancellation_frames += 1
        used_active_brake |= decision.movement != MovementV1()
        if decision.state is FixedRouteState.CANCELLED:
            break
        step(decision.movement)
    else:
        raise TimeoutError("B03 cancellation did not stop in 40 frames")
    cancellation_speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                                    frame.body.velocity_blocks_per_second[2])
    cancellation_distance = math.dist(cancel_start, frame.body.position)

    # Renew a two-tick movement lease at speed, then stop all transport long
    # enough for the client watchdog to release it before the next request.
    for _ in range(10):
        step(MovementV1(forward=1), valid_for_ticks=2)
    lease_start = tuple(frame.body.position)
    time.sleep(.60)
    runtime.cancel_source(_SOURCE)
    step(MovementV1(), valid_for_ticks=1)
    lease_distance = math.dist(lease_start, frame.body.position)
    lease_speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                             frame.body.velocity_blocks_per_second[2])

    evidence = dict(
        schema_version="mc2p.b03-fixed-route-evidence.v1",
        trials=trials,
        cancellation=dict(frames=cancellation_frames, active_brake=used_active_brake,
                          stop_distance_blocks=cancellation_distance,
                          final_speed_blocks_per_second=cancellation_speed),
        input_stream_loss=dict(silence_seconds=.60, lease_ticks=2,
                               displacement_blocks=lease_distance,
                               final_speed_blocks_per_second=lease_speed),
        input_confirmation_loss=confirmation_loss,
    )
    write_json_atomic(directory / "b03-fixed-route.json", evidence)
    checks = [
        dict(name="b03_critical_scenarios_five_runs_per_jvm", passed=len(trials) == 25
             and all(sum(trial["scenario"] == scenario for trial in trials) == 5
                     for scenario, *_ in specifications) and all(
            trial["final_error_blocks"] <= .25 and trial["final_speed_blocks_per_second"] <= .1
            for trial in trials)),
        dict(name="b03_no_unnecessary_middle_low_speed", passed=all(
            trial["maximum_middle_low_speed_frames"] < 3
            for trial in trials if trial["scenario"] != "S02_narrow_turn")),
        dict(name="b03_narrow_turn_uses_physical_corridor", passed=all(
            trial["physical_corridor_wall_blocks"] > 0
            and trial["observed_corridor_wall_blocks"] > 0
            for trial in trials if trial["scenario"] == "S02_narrow_turn")),
        dict(name="b03_control_budget", passed=all(
            trial["control_time_p95_ms"] <= 8 and trial["control_time_p99_ms"] <= 15
            and trial["control_time_max_ms"] < 30 for trial in trials)),
        dict(name="b03_cancel_stops_observed_body", passed=cancellation_speed <= .1
             and cancellation_frames < 40),
        dict(name="b03_input_confirmation_loss_fails_closed", passed=
             confirmation_loss["state"] == FixedRouteState.INPUT_LOST.value
             and confirmation_loss["rejected_receipt_status"] == "rejected"
             and confirmation_loss["rejected_report_status"] == "failed"
             and confirmation_loss["movement"] == asdict(MovementV1())
             and receipt_confirms_input(confirmation_loss["neutral_receipt_status"])),
        dict(name="b03_client_lease_releases_on_stream_loss", passed=lease_distance <= .75
             and lease_speed <= .1),
    ]
    return evidence, rows, checks
