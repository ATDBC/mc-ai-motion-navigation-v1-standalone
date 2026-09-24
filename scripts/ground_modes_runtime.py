"""Fabric B08 calibration and fixed-route checks for Sprint and Crouch."""
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
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.evidence.ground_calibration import (
    GroundMotionSample, calibrate_ground_motion,
)
from mc2p.motion_nav.ground_modes import load_ground_mode_profiles
from mc2p.motion_nav.ground_motion import GroundControl, PlanarBodyState, predict_ground
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fixed_route_runtime_core import receipt_confirms_input


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
_SOURCE = "b08-ground-modes"


def _planar(frame) -> PlanarBodyState:
    body = frame.body
    return PlanarBodyState(
        body.position[0], body.position[2],
        body.velocity_blocks_per_second[0], body.velocity_blocks_per_second[2],
        body.yaw_radians,
    )


def _errors(samples: tuple[GroundMotionSample, ...], profile) -> dict:
    positions, velocities = [], []
    for sample in samples:
        predicted = predict_ground(sample.before, (sample.control,), profile)[-1]
        positions.append(math.hypot(predicted.x - sample.after.x,
                                    predicted.z - sample.after.z))
        velocities.append(math.hypot(predicted.velocity_x - sample.after.velocity_x,
                                     predicted.velocity_z - sample.after.velocity_z))
    positions.sort(); velocities.sort()
    index = max(0, math.ceil(len(positions) * .95) - 1)
    return dict(
        samples=len(samples), position_p95_blocks=positions[index],
        position_max_blocks=max(positions),
        velocity_p95_blocks_per_second=velocities[index],
        velocity_max_blocks_per_second=max(velocities),
    )


def run_ground_modes_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[tuple[int, int, int], ...], str], None],
    player_teleporter: Callable[[float, float, float, float, float], None],
) -> tuple[dict, list[dict], list[dict]]:
    environment = load_frozen_environment(CONFIG / "environment-v1.json")
    catalog = BlockMotionCatalog.load(
        CONFIG / "block-motion-traits-v1.json",
        CONFIG / "vanilla-block-registry-1_21.json",
    )
    profiles = load_ground_mode_profiles(
        CONFIG / "ground-modes-b08-v1.json", environment=environment, catalog=catalog,
    )
    task = TaskIntentV0(
        "b08-ground-modes", "ground_mode_calibration", "{}",
        (SuccessCriterionV0("mode_trials_passed", ComparisonOperatorV0.EQUAL, 10, "count"),),
        3000, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    adapter = NavigationObservationAdapter()
    frame = adapter.ingest(runtime.observation)
    rows: list[dict] = []
    counter = 0

    def diagnostic() -> None:
        row = dict(
            episode_id=episode, observation_sequence_id=runtime.observation.sequence_id,
            diagnostics=backend.last_diagnostics,
        )
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    def step(movement=MovementV1(), *, look=None, request=None):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"b08-{counter}", _SOURCE, episode, runtime.observation.sequence_id,
            ActionPriorityV0.TASK, now, min(deadline_ns, now + 750_000_000),
            movement=movement, look=look, valid_for_ticks=1,
        ))
        result = runtime.step(task, behavior, min(deadline_ns, now + 5_000_000_000),
                              observation_request=request)
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B08 Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B08 movement bypassed the formal arbiter")
        before = frame
        frame = adapter.ingest(result.observation)
        diagnostic()
        return before, frame, result

    diagnostic()
    feet_y = math.floor(frame.body.position[1] + 1.0e-6)
    center_x, center_z = math.floor(frame.body.position[0]), math.floor(frame.body.position[2])
    floor = tuple(
        (center_x + dx, feet_y - 1, center_z + dz)
        for dx in range(-2, 3) for dz in range(-3, 14)
    )
    air = tuple(
        (center_x + dx, y, center_z + dz)
        for dx in range(-3, 4) for y in range(feet_y, feet_y + 4)
        for dz in range(-4, 15)
    )
    fixture_writer(air, "minecraft:air")
    fixture_writer(floor, "minecraft:grass_block")
    air_request = ObservationRequestV3("navigation_v1", air[:512])

    def teleport() -> None:
        nonlocal frame
        player_teleporter(center_x + .5, float(feet_y), center_z + .5, 0.0, 70.0)
        for _ in range(12):
            step(request=air_request)
            if (math.hypot(frame.body.position[0] - center_x - .5,
                           frame.body.position[2] - center_z - .5) <= .02
                    and frame.body.is_on_ground
                    and math.hypot(frame.body.velocity_blocks_per_second[0],
                                   frame.body.velocity_blocks_per_second[2]) <= .1):
                return
        raise RuntimeError("B08 teleport did not settle")

    # Observe the complete short runway through the formal navigation profile.
    for yaw in (0.0, 90.0, 180.0, -90.0):
        delta = (yaw - math.degrees(frame.body.yaw_radians) + 180.0) % 360.0 - 180.0
        step(look=LookV1(delta, 70.0 - math.degrees(frame.body.pitch_radians)),
             request=air_request)

    all_samples: dict[MovementMode, list[tuple[int, GroundMotionSample]]] = {
        MovementMode.SPRINT: [], MovementMode.CROUCH: [], MovementMode.CRAWL: [],
    }
    trials = []
    cancellation_trials = []
    control_times: list[int] = []
    for mode in (MovementMode.SPRINT, MovementMode.CROUCH):
        profile = profiles.require(mode)
        for repetition in range(10):
            teleport()
            requested = MovementV1(forward=1, sprint=mode is MovementMode.SPRINT,
                                   sneak=mode is MovementMode.CROUCH)
            release = MovementV1(sneak=mode is MovementMode.CROUCH)
            observed_actual = False
            samples: list[GroundMotionSample] = []
            for movement in (requested,) * 16 + (release,) * 12:
                before, after, _ = step(movement)
                actual_before = (before.body.is_sprinting if mode is MovementMode.SPRINT
                                 else before.body.is_sneaking and before.body.pose == "crouching")
                actual_after = (after.body.is_sprinting if mode is MovementMode.SPRINT
                                else after.body.is_sneaking and after.body.pose == "crouching")
                observed_actual = observed_actual or actual_after
                if (actual_before and (actual_after or movement == release)
                        and before.body.is_on_ground and after.body.is_on_ground
                        and not before.body.horizontal_collision
                        and not after.body.horizontal_collision):
                    sample = GroundMotionSample(
                        _planar(before),
                        GroundControl(movement.forward, -movement.strafe,
                                      before.body.yaw_radians),
                        _planar(after),
                    )
                    samples.append(sample)
                    all_samples[mode].append((repetition, sample))
            if not observed_actual:
                raise RuntimeError(f"B08 never observed actual {mode.value}")

            teleport()
            # Start from an observed mode, so this run measures route tracking.
            # The direct phase above separately proves that the requested input
            # causes the actual client state transition.
            for _ in range(profile.confirmation_ticks + 2):
                activation = (MovementV1(forward=1, sprint=True)
                              if mode is MovementMode.SPRINT else MovementV1(sneak=True))
                step(activation)
                active = (frame.body.is_sprinting if mode is MovementMode.SPRINT
                          else frame.body.is_sneaking and frame.body.pose == "crouching")
                if active:
                    break
            if not active:
                raise RuntimeError(f"B08 could not preconfirm {mode.value} for route")
            route = FixedRoute(
                f"b08-{mode.value}-{repetition}",
                (RoutePoint(frame.body.position[0], frame.body.position[1], frame.body.position[2]),
                 RoutePoint(frame.body.position[0], frame.body.position[1],
                            frame.body.position[2] + 1.5)),
            )
            controller = FixedRouteController(profile.motion, mode_profile=profile)
            controller.start(route, frame)
            input_confirmed = True
            actual_frames = 0
            decision_history = []
            started = time.perf_counter_ns()
            for control_frame in range(1, 100):
                decision = controller.decide(frame, input_confirmed=input_confirmed)
                decision_history.append(dict(
                    state=decision.state.value, reason=decision.reason,
                    forward=decision.movement.forward, strafe=decision.movement.strafe,
                    sprint=decision.movement.sprint, sneak=decision.movement.sneak,
                    progress=decision.progress_blocks,
                    missing_count=len(decision.missing_cells),
                    missing_cells=decision.missing_cells[:16],
                ))
                control_times.append(decision.control_time_ns)
                actual_frames += int(
                    frame.body.is_sprinting if mode is MovementMode.SPRINT
                    else frame.body.is_sneaking and frame.body.pose == "crouching"
                )
                if decision.state is FixedRouteState.SUCCEEDED:
                    break
                if decision.state in {
                    FixedRouteState.BLOCKED, FixedRouteState.FAILED,
                    FixedRouteState.UNSUPPORTED, FixedRouteState.INPUT_LOST,
                }:
                    raise RuntimeError(
                        f"B08 {mode.value} route failed: {decision.state}/{decision.reason}; "
                        f"history={decision_history}"
                    )
                request = None
                if decision.missing_cells:
                    request, _ = adapter.air_request(decision.missing_cells, max_positions=512)
                route_look = LookV1(90.0, 0.0) if repetition == 0 and control_frame == 3 else None
                _, _, result = step(decision.movement, look=route_look, request=request)
                input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
            else:
                raise TimeoutError(
                    f"B08 {mode.value} route exceeded 100 frames; "
                    f"history={decision_history[-20:]}"
                )
            error = math.hypot(frame.body.position[0] - route.points[-1].x,
                               frame.body.position[2] - route.points[-1].z)
            trial = dict(
                mode=mode.value, repetition=repetition, samples=len(samples),
                actual_mode_frames=actual_frames, control_frames=control_frame,
                final_error_blocks=error,
                final_speed_blocks_per_second=math.hypot(
                    frame.body.velocity_blocks_per_second[0],
                    frame.body.velocity_blocks_per_second[2],
                ),
                elapsed_seconds=(time.perf_counter_ns() - started) / 1e9,
            )
            trials.append(trial)
            append_jsonl(directory / "b08-mode-trials.jsonl", trial)
            # Explicitly release Crouch before the next teleport.
            if mode is MovementMode.CROUCH:
                for _ in range(4):
                    step()
                    if not frame.body.is_sneaking and frame.body.pose == "standing":
                        break

        # One real cancellation and one input-loss decision per mode.  The
        # input-loss decision is intentionally not dispatched; it proves the
        # only output is neutral after confirmation is lost.
        teleport()
        powered = MovementV1(forward=1, sprint=mode is MovementMode.SPRINT,
                             sneak=mode is MovementMode.CROUCH)
        for _ in range(profile.confirmation_ticks + 8):
            step(powered)
            active = (frame.body.is_sprinting if mode is MovementMode.SPRINT
                      else frame.body.is_sneaking and frame.body.pose == "crouching")
            speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                               frame.body.velocity_blocks_per_second[2])
            if active and speed >= .25:
                break
        cancel_route = FixedRoute(
            f"b08-{mode.value}-cancel",
            (RoutePoint(frame.body.position[0], frame.body.position[1], frame.body.position[2]),
             RoutePoint(frame.body.position[0], frame.body.position[1],
                        frame.body.position[2] + 5.0)),
        )
        lost = FixedRouteController(profile.motion, mode_profile=profile)
        lost.start(cancel_route, frame)
        lost_decision = lost.decide(frame, input_confirmed=False)
        if (lost_decision.state is not FixedRouteState.INPUT_LOST
                or lost_decision.movement != MovementV1()):
            raise RuntimeError(f"B08 {mode.value} input loss was not fail-closed")
        cancelling = FixedRouteController(profile.motion, mode_profile=profile)
        cancelling.start(cancel_route, frame)
        cancelling.cancel()
        cancel_start = frame.body.position
        input_confirmed = True
        for cancel_frame in range(1, 41):
            decision = cancelling.decide(frame, input_confirmed=input_confirmed)
            _, _, result = step(decision.movement)
            input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
            if decision.state is FixedRouteState.CANCELLED:
                break
            if decision.state in {FixedRouteState.FAILED, FixedRouteState.UNSUPPORTED,
                                  FixedRouteState.INPUT_LOST}:
                raise RuntimeError(
                    f"B08 {mode.value} cancellation failed: "
                    f"{decision.state}/{decision.reason}"
                )
        else:
            raise TimeoutError(f"B08 {mode.value} cancellation exceeded 40 frames")
        cancellation_trials.append(dict(
            mode=mode.value, frames=cancel_frame,
            displacement_blocks=math.hypot(
                frame.body.position[0] - cancel_start[0],
                frame.body.position[2] - cancel_start[2],
            ),
            final_speed_blocks_per_second=math.hypot(
                frame.body.velocity_blocks_per_second[0],
                frame.body.velocity_blocks_per_second[2],
            ),
            input_loss_state=lost_decision.state.value,
        ))

    # Establish Crawl as a declared fixture precondition.  The actor does not
    # create or operate the ceiling: teleporting into the already-low passage
    # merely lets the game choose the only legal body pose.
    crawl_ceiling = tuple(
        (center_x + dx, feet_y + 1, center_z + dz)
        for dx in range(-1, 2) for dz in range(-1, 8)
    )
    fixture_writer(crawl_ceiling, "minecraft:grass_block")
    ceiling_set = set(crawl_ceiling)
    crawl_air_request = ObservationRequestV3(
        "navigation_v1", tuple(position for position in air
                               if position not in ceiling_set)[:512],
    )

    def teleport_crawl() -> None:
        nonlocal frame
        player_teleporter(center_x + .5, float(feet_y), center_z + .5, 0.0, 70.0)
        for _ in range(12):
            step(request=crawl_air_request)
            if (frame.body.pose == "swimming"
                    and not frame.body.is_submerged_in_water
                    and frame.body.is_on_ground
                    and math.hypot(frame.body.position[0] - center_x - .5,
                                   frame.body.position[2] - center_z - .5) <= .02
                    and math.hypot(frame.body.velocity_blocks_per_second[0],
                                   frame.body.velocity_blocks_per_second[2]) <= .1):
                return
        raise RuntimeError(
            "B08 Crawl fixture did not establish swimming pose; "
            f"pose={frame.body.pose}, is_swimming={frame.body.is_swimming}, "
            f"submerged={frame.body.is_submerged_in_water}"
        )

    teleport_crawl()
    crawl_precondition = dict(
        pose=frame.body.pose,
        is_swimming=frame.body.is_swimming,
        is_submerged_in_water=frame.body.is_submerged_in_water,
    )
    crawl_profile = profiles.require(MovementMode.CRAWL)
    for repetition in range(10):
        teleport_crawl()
        samples: list[GroundMotionSample] = []
        for movement in (MovementV1(forward=1),) * 16 + (MovementV1(),) * 12:
            before, after, _ = step(movement)
            actual_before = (before.body.pose == "swimming"
                             and not before.body.is_submerged_in_water)
            actual_after = (after.body.pose == "swimming"
                            and not after.body.is_submerged_in_water)
            if (actual_before and actual_after and before.body.is_on_ground
                    and after.body.is_on_ground and not before.body.horizontal_collision
                    and not after.body.horizontal_collision):
                sample = GroundMotionSample(
                    _planar(before),
                    GroundControl(movement.forward, -movement.strafe,
                                  before.body.yaw_radians),
                    _planar(after),
                )
                samples.append(sample)
                all_samples[MovementMode.CRAWL].append((repetition, sample))
        teleport_crawl()
        route = FixedRoute(
            f"b08-crawl-{repetition}",
            (RoutePoint(frame.body.position[0], frame.body.position[1], frame.body.position[2]),
             RoutePoint(frame.body.position[0], frame.body.position[1],
                        frame.body.position[2] + 1.5)),
        )
        controller = FixedRouteController(crawl_profile.motion, mode_profile=crawl_profile)
        controller.start(route, frame)
        actual_frames = 0
        input_confirmed = True
        started = time.perf_counter_ns()
        for control_frame in range(1, 100):
            decision = controller.decide(frame, input_confirmed=input_confirmed)
            control_times.append(decision.control_time_ns)
            actual_frames += int(frame.body.pose == "swimming"
                                 and not frame.body.is_submerged_in_water)
            if decision.state is FixedRouteState.SUCCEEDED:
                break
            if decision.state in {
                FixedRouteState.BLOCKED, FixedRouteState.FAILED,
                FixedRouteState.UNSUPPORTED, FixedRouteState.INPUT_LOST,
            }:
                raise RuntimeError(
                    f"B08 crawl route failed: {decision.state}/{decision.reason}; "
                    f"missing={decision.missing_cells[:16]}"
                )
            request = None
            if decision.missing_cells:
                request, _ = adapter.air_request(decision.missing_cells, max_positions=512)
            route_look = LookV1(90.0, 0.0) if repetition == 0 and control_frame == 3 else None
            _, _, result = step(decision.movement, look=route_look, request=request)
            input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        else:
            raise TimeoutError("B08 crawl route exceeded 100 frames")
        error = math.hypot(frame.body.position[0] - route.points[-1].x,
                           frame.body.position[2] - route.points[-1].z)
        trial = dict(
            mode=MovementMode.CRAWL.value, repetition=repetition, samples=len(samples),
            actual_mode_frames=actual_frames, control_frames=control_frame,
            final_error_blocks=error,
            final_speed_blocks_per_second=math.hypot(
                frame.body.velocity_blocks_per_second[0],
                frame.body.velocity_blocks_per_second[2],
            ),
            elapsed_seconds=(time.perf_counter_ns() - started) / 1e9,
        )
        trials.append(trial)
        append_jsonl(directory / "b08-mode-trials.jsonl", trial)

    teleport_crawl()
    for _ in range(8):
        step(MovementV1(forward=1))
        if math.hypot(frame.body.velocity_blocks_per_second[0],
                      frame.body.velocity_blocks_per_second[2]) >= .25:
            break
    cancel_route = FixedRoute(
        "b08-crawl-cancel",
        (RoutePoint(frame.body.position[0], frame.body.position[1], frame.body.position[2]),
         RoutePoint(frame.body.position[0], frame.body.position[1],
                    frame.body.position[2] + 5.0)),
    )
    lost = FixedRouteController(crawl_profile.motion, mode_profile=crawl_profile)
    lost.start(cancel_route, frame)
    lost_decision = lost.decide(frame, input_confirmed=False)
    if (lost_decision.state is not FixedRouteState.INPUT_LOST
            or lost_decision.movement != MovementV1()):
        raise RuntimeError("B08 crawl input loss was not fail-closed")
    cancelling = FixedRouteController(crawl_profile.motion, mode_profile=crawl_profile)
    cancelling.start(cancel_route, frame)
    cancelling.cancel()
    cancel_start = frame.body.position
    input_confirmed = True
    for cancel_frame in range(1, 41):
        decision = cancelling.decide(frame, input_confirmed=input_confirmed)
        _, _, result = step(decision.movement)
        input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        if decision.state is FixedRouteState.CANCELLED:
            break
        if decision.state in {FixedRouteState.FAILED, FixedRouteState.UNSUPPORTED,
                              FixedRouteState.INPUT_LOST}:
            raise RuntimeError(
                f"B08 crawl cancellation failed: {decision.state}/{decision.reason}"
            )
    else:
        raise TimeoutError("B08 crawl cancellation exceeded 40 frames")
    cancellation_trials.append(dict(
        mode=MovementMode.CRAWL.value, frames=cancel_frame,
        displacement_blocks=math.hypot(
            frame.body.position[0] - cancel_start[0],
            frame.body.position[2] - cancel_start[2],
        ),
        final_speed_blocks_per_second=math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        ),
        input_loss_state=lost_decision.state.value,
    ))

    # Removing the fixture ceiling gives the game legal standing clearance.
    # A Walk segment may start only after the formal observation confirms that
    # the actual pose has changed back to standing.
    fixture_writer(crawl_ceiling, "minecraft:air")
    ceiling_air_request = ObservationRequestV3("navigation_v1", crawl_ceiling)
    for exit_frame in range(1, 13):
        step(request=ceiling_air_request)
        if frame.body.pose == "standing":
            break
    if frame.body.pose != "standing":
        raise RuntimeError("B08 Crawl did not exit after standing clearance became available")
    exit_route = FixedRoute(
        "b08-crawl-exit-walk",
        (RoutePoint(frame.body.position[0], frame.body.position[1], frame.body.position[2]),
         RoutePoint(frame.body.position[0], frame.body.position[1],
                    frame.body.position[2] + 1.0)),
    )
    walk_profile = profiles.require(MovementMode.WALK)
    exit_controller = FixedRouteController(walk_profile.motion, mode_profile=walk_profile)
    exit_controller.start(exit_route, frame)
    input_confirmed = True
    for walk_frame in range(1, 81):
        decision = exit_controller.decide(frame, input_confirmed=input_confirmed)
        if decision.state is FixedRouteState.SUCCEEDED:
            break
        if decision.state in {FixedRouteState.BLOCKED, FixedRouteState.FAILED,
                              FixedRouteState.UNSUPPORTED, FixedRouteState.INPUT_LOST}:
            raise RuntimeError(
                f"B08 post-Crawl Walk failed: {decision.state}/{decision.reason}"
            )
        request = None
        if decision.missing_cells:
            request, _ = adapter.air_request(decision.missing_cells, max_positions=512)
        _, _, result = step(decision.movement, request=request)
        input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
    else:
        raise TimeoutError("B08 post-Crawl Walk exceeded 80 frames")
    crawl_exit = dict(
        confirmation_frames=exit_frame, pose=frame.body.pose,
        walk_frames=walk_frame,
        final_error_blocks=math.hypot(
            frame.body.position[0] - exit_route.points[-1].x,
            frame.body.position[2] - exit_route.points[-1].z,
        ),
    )

    calibrations = {}
    for mode, tagged in all_samples.items():
        calibration = tuple(sample for repetition, sample in tagged if repetition % 2 == 0)
        validation = tuple(sample for repetition, sample in tagged if repetition % 2 == 1)
        fitted = calibrate_ground_motion(
            calibration, tick_seconds=.05, validation_samples=validation,
        )
        configured = profiles.require(mode).motion
        calibrations[mode.value] = dict(
            fitted_profile={
                "acceleration_blocks_per_second2": fitted.profile.acceleration_blocks_per_second2,
                "velocity_retention_per_tick": fitted.profile.velocity_retention_per_tick,
                "maximum_speed_blocks_per_second": fitted.profile.maximum_speed_blocks_per_second,
            },
            configured_error=_errors(validation, configured),
            calibration_samples=len(calibration), validation_samples=len(validation),
        )

    ordered = sorted(control_times)
    summary = dict(
        schema_version="mc2p.b08-ground-modes.v1",
        trials=trials, calibrations=calibrations,
        cancellation_trials=cancellation_trials,
        crawl_precondition=crawl_precondition, crawl_exit=crawl_exit,
        control_time_p95_ms=ordered[math.ceil(len(ordered) * .95) - 1] / 1e6,
        control_time_p99_ms=ordered[math.ceil(len(ordered) * .99) - 1] / 1e6,
        control_time_max_ms=max(ordered) / 1e6,
    )
    checks = [
        dict(name="b08_three_modes_ten_routes_each", passed=(
            len(trials) == 30 and all(
                sum(t["mode"] == mode.value for t in trials) == 10
                for mode in (MovementMode.SPRINT, MovementMode.CROUCH, MovementMode.CRAWL)
            ))),
        dict(name="b08_actual_modes_observed", passed=all(t["actual_mode_frames"] > 0 for t in trials)),
        dict(name="b08_cancel_and_input_loss", passed=(
            len(cancellation_trials) == 3
            and all(t["final_speed_blocks_per_second"] <= .1
                    and t["input_loss_state"] == "input_lost"
                    for t in cancellation_trials))),
        dict(name="b08_routes_finish_precisely", passed=all(
            t["final_error_blocks"] <= .25 and t["final_speed_blocks_per_second"] <= .1
            for t in trials)),
        dict(name="b08_crawl_exit_continues_walk", passed=(
            crawl_exit["pose"] == "standing"
            and crawl_exit["final_error_blocks"] <= .25)),
        dict(name="b08_control_budget", passed=(
            summary["control_time_p99_ms"] <= 50 and summary["control_time_max_ms"] <= 100)),
    ]
    write_json_atomic(directory / "b08-ground-modes.json", summary)
    return summary, rows, checks
