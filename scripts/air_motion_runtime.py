"""Fabric B09 calibration and acceptance for gap jumps and controlled drops."""
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
from mc2p.motion_nav.air_motion import (
    AirMotionController, AirMotionState, load_air_motion_profiles,
)
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SnapshotBuildStatus,
    SurfacePlanningRequest, SurfacePlanningStatus, build_surface_graph,
)
from mc2p.motion_nav.movement_transition import MovementMode, ResourceState
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.step_transition import load_step_profile
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import CellKnowledge
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fixed_route_runtime_core import receipt_confirms_input


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
_SOURCE = "b09-air-motion"
_DIRECTIONS = ((0, 1, 0.0), (1, 0, -90.0), (0, -1, 180.0), (-1, 0, 90.0))


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def run_air_motion_runtime(
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
    profiles = load_air_motion_profiles(
        CONFIG / "air-motions-b09-v1.json",
        environment=environment, catalog=catalog,
    )
    ground_profile = load_ground_motion_profile(
        CONFIG / "ordinary-ground-b07-v1.json",
        environment=environment, catalog=catalog,
    )
    step_profile = load_step_profile(
        CONFIG / "step-b07-v1.json", environment=environment,
    )
    jump_profile = load_jump_up_profile(
        CONFIG / "jump-up-b06-v1.json",
        environment=environment, catalog=catalog,
    )
    by_mode = {profile.mode: profile for profile in profiles}
    task = TaskIntentV0(
        "b09-air-motion", "gap_jump_and_controlled_drop", "{}",
        (SuccessCriterionV0("air_trials", ComparisonOperatorV0.EQUAL, 80, "count"),),
        5000, deadline_ns, True, 0.0,
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

    def step(movement=MovementV1(), *, look=None, request=None):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"b09-{counter}", _SOURCE, episode, runtime.observation.sequence_id,
            ActionPriorityV0.TASK, now, min(deadline_ns, now + 750_000_000),
            movement=movement, look=look, valid_for_ticks=1,
        ))
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B09 Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B09 movement bypassed the formal arbiter")
        frame = adapter.ingest(result.observation)
        diagnostic()
        return result

    diagnostic()
    feet_y = math.floor(frame.body.position[1] + 1.0e-6)
    origin_x = math.floor(frame.body.position[0])
    origin_z = math.floor(frame.body.position[2])
    gap_start = (origin_x, feet_y - 1, origin_z)
    drop_origin_x = origin_x + 8
    drop_start = (drop_origin_x, feet_y - 1, origin_z)
    gap_targets = tuple(
        (origin_x + dx * 2, feet_y - 1, origin_z + dz * 2)
        for dx, dz, _ in _DIRECTIONS
    )
    drop_targets = tuple(
        (drop_origin_x + dx, feet_y - 2, origin_z + dz)
        for dx, dz, _ in _DIRECTIONS
    )
    gap_route_x = origin_x + 14
    drop_route_x = origin_x + 20
    gap_route_supports = (
        (gap_route_x, feet_y - 1, origin_z - 1),
        (gap_route_x, feet_y - 1, origin_z),
        (gap_route_x, feet_y - 1, origin_z + 2),
        (gap_route_x, feet_y - 1, origin_z + 3),
    )
    drop_route_supports = (
        (drop_route_x, feet_y - 1, origin_z - 1),
        (drop_route_x, feet_y - 1, origin_z),
        (drop_route_x, feet_y - 2, origin_z + 1),
        (drop_route_x, feet_y - 2, origin_z + 2),
    )

    def volume_around(center_x: int) -> tuple[tuple[int, int, int], ...]:
        return tuple(
            (x, y, z)
            for x in range(center_x - 2, center_x + 3)
            for y in range(feet_y - 4, feet_y + 4)
            for z in range(origin_z - 2, origin_z + 4)
        )

    gap_volume = volume_around(origin_x)
    drop_volume = volume_around(drop_origin_x)
    gap_route_volume = volume_around(gap_route_x)
    drop_route_volume = volume_around(drop_route_x)
    volume = tuple(sorted(set(
        gap_volume + drop_volume + gap_route_volume + drop_route_volume
    )))
    supports = (
        gap_start, drop_start, *gap_targets, *drop_targets,
        *gap_route_supports, *drop_route_supports,
    )
    support_set = set(supports)
    fixture_writer(tuple(position for position in volume if position not in support_set),
                   "minecraft:air")
    fixture_writer(supports, "minecraft:grass_block")
    def air_request(region: tuple[tuple[int, int, int], ...]) -> ObservationRequestV3:
        return ObservationRequestV3("navigation_v1", tuple(
            position for position in region if position not in support_set
        ))

    gap_request = air_request(gap_volume)
    drop_request = air_request(drop_volume)
    gap_route_request = air_request(gap_route_volume)
    drop_route_request = air_request(drop_route_volume)

    def teleport(position: tuple[float, float, float], yaw: float,
                 request: ObservationRequestV3) -> None:
        nonlocal frame
        player_teleporter(*position, yaw, 0.0)
        for _ in range(16):
            step(request=request)
            yaw_error = abs((math.degrees(frame.body.yaw_radians) - yaw + 180) % 360 - 180)
            speed = math.hypot(
                frame.body.velocity_blocks_per_second[0],
                frame.body.velocity_blocks_per_second[2],
            )
            if (math.dist(frame.body.position, position) <= .03
                    and frame.body.is_on_ground and speed <= .03 and yaw_error <= 1.0):
                return
        raise RuntimeError("B09 teleport did not settle")

    def look_at_cell(position: tuple[int, int, int],
                     request: ObservationRequestV3) -> None:
        for _ in range(8):
            dx = position[0] + .5 - frame.body.position[0]
            dy = position[1] + .5 - (frame.body.position[1] + 1.62)
            dz = position[2] + .5 - frame.body.position[2]
            target_yaw = math.degrees(math.atan2(-dx, dz))
            target_pitch = -math.degrees(math.atan2(dy, max(1.0e-6, math.hypot(dx, dz))))
            current_yaw = math.degrees(frame.body.yaw_radians)
            current_pitch = math.degrees(frame.body.pitch_radians)
            step(
                look=LookV1(
                    (target_yaw - current_yaw + 180) % 360 - 180,
                    target_pitch - current_pitch,
                ),
                request=request,
            )
            fact = frame.world.cell(position)
            if fact.knowledge is CellKnowledge.BLOCK:
                return
        raise RuntimeError(f"B09 support was not formally observed: {position}")

    teleport((origin_x + .5, float(feet_y), origin_z + .5), 0.0, gap_request)
    for position in (gap_start, *gap_targets):
        look_at_cell(position, gap_request)
    teleport((drop_origin_x + .5, float(feet_y), origin_z + .5), 0.0, drop_request)
    for position in (drop_start, *drop_targets):
        look_at_cell(position, drop_request)
    teleport((gap_route_x + .5, float(feet_y), origin_z - .5), 0.0,
             gap_route_request)
    for position in gap_route_supports:
        teleport(
            (position[0] + .5, float(position[1] + 1), position[2] + .5),
            0.0, gap_route_request,
        )
        look_at_cell(position, gap_route_request)
    teleport((drop_route_x + .5, float(feet_y), origin_z - .5), 0.0,
             drop_route_request)
    for position in drop_route_supports:
        teleport(
            (position[0] + .5, float(position[1] + 1), position[2] + .5),
            0.0, drop_route_request,
        )
        look_at_cell(position, drop_route_request)
    for current_request in (
            gap_request, drop_request, gap_route_request, drop_route_request):
        step(request=current_request)

    def support_at(position: tuple[float, float, float]):
        result = query_support_surfaces(
            frame.world, math.floor(position[0]), math.floor(position[2]),
            position[1] - .1, position[1] + .1,
        )
        if result.status is not QueryStatus.FEASIBLE:
            raise RuntimeError(f"B09 support query failed: {position}/{result.status}")
        return min(result.surfaces,
                   key=lambda item: abs(item.position[1] - position[1]))

    trials: list[dict] = []
    control_times: list[int] = []

    def run_trial(mode: MovementMode, direction_index: int, repetition: int) -> None:
        dx, dz, yaw = _DIRECTIONS[direction_index]
        if mode is MovementMode.JUMP_GAP:
            start_position = (origin_x + .5, float(feet_y), origin_z + .5)
            end_position = (
                origin_x + dx * 2 + .5, float(feet_y), origin_z + dz * 2 + .5,
            )
        else:
            start_position = (drop_origin_x + .5, float(feet_y), origin_z + .5)
            end_position = (
                drop_origin_x + dx + .5, float(feet_y - 1), origin_z + dz + .5,
            )
        current_request = (
            gap_request if mode is MovementMode.JUMP_GAP else drop_request
        )
        teleport(start_position, yaw, current_request)
        start_surface, end_surface = support_at(start_position), support_at(end_position)
        profile = by_mode[mode]
        controller = AirMotionController(profile)
        controller.start(start_surface, end_surface, frame)
        samples = []
        input_confirmed = True
        for _ in range(48):
            decision = controller.decide(frame, input_confirmed=input_confirmed)
            control_times.append(decision.control_time_ns)
            sample = dict(
                sequence=frame.body.sequence_id,
                state=decision.state.value,
                reason=decision.reason_code,
                position=list(frame.body.position),
                velocity=list(frame.body.velocity_blocks_per_second),
                on_ground=frame.body.is_on_ground,
                horizontal_collision=frame.body.horizontal_collision,
                movement=asdict(decision.movement),
            )
            samples.append(sample)
            if decision.state in AirMotionController._TERMINAL:
                break
            result = step(
                decision.movement, look=decision.look, request=current_request,
            )
            input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        horizontal_error = math.hypot(
            frame.body.position[0] - end_position[0],
            frame.body.position[2] - end_position[2],
        )
        trial = dict(
            name=f"{mode.value}-{direction_index}-{repetition}",
            mode=mode.value, direction_index=direction_index,
            repetition=repetition, final_state=controller.state.value,
            start_position=list(start_position), target_position=list(end_position),
            final_position=list(frame.body.position),
            horizontal_error_blocks=horizontal_error,
            level_error_blocks=abs(frame.body.position[1] - end_position[1]),
            jump_pulses=sum(int(row["movement"]["jump"]) for row in samples),
            horizontal_collisions=sum(int(row["horizontal_collision"]) for row in samples),
            samples=samples,
        )
        trials.append(trial)
        append_jsonl(directory / "b09-air-trials.jsonl", trial)

    route_trials: list[dict] = []

    def run_planned_route(name: str, start_position: tuple[float, float, float],
                          end_position: tuple[float, float, float],
                          bounds: KnownMapBounds,
                          observation_request: ObservationRequestV3,
                          expected_actions: tuple[str, ...]) -> None:
        teleport(start_position, 0.0, observation_request)
        start_surface = support_at(start_position)
        end_surface = support_at(end_position)
        builder = KnownMapSnapshotBuilder(frame.world, bounds)
        progress = builder.advance(frame.world, 10_000)
        if (progress.status is not SnapshotBuildStatus.COMPLETE
                or progress.snapshot is None
                or not progress.snapshot.bounds.complete_scope):
            raise RuntimeError(
                f"B09 {name} snapshot incomplete: {progress.status.value}"
            )
        planning_request = SurfacePlanningRequest(
            frame.body.sequence_id, f"{episode}-{name}", f"goal-{name}", 1,
            frame.session.value, start_surface.node_id, end_surface.node_id,
            initial_resources=ResourceState((
                ("food_points", float(frame.body.food_points)),
            )),
        )
        debug_graph = build_surface_graph(
            progress.snapshot.world, progress.snapshot.bounds,
            ground_profile, step_profile, jump_profile,
            air_profiles=profiles,
        )
        write_json_atomic(directory / f"b09-{name}-graph.json", dict(
            complete_scope=debug_graph.complete_scope,
            has_unsupported=debug_graph.has_unsupported,
            requested_start=asdict(planning_request.start),
            requested_goal=asdict(planning_request.goal),
            nodes=[dict(id=asdict(node.node_id), position=list(node.position))
                   for node in debug_graph.nodes],
            edges=[dict(kind=type(edge).__name__, start=asdict(edge.start),
                        end=asdict(edge.end), cost_seconds=edge.cost_seconds)
                   for edge in debug_graph.edges],
        ))
        with PlannerWorker() as worker:
            worker.submit_surface_snapshot(
                progress.snapshot, ground_profile, step_profile, planning_request,
                jump_profile, air_profiles=profiles,
            )
            candidate = None
            planning_deadline = time.perf_counter() + 3.0
            while candidate is None and time.perf_counter() < planning_deadline:
                candidate = worker.poll_latest()
                if candidate is None:
                    time.sleep(.005)
        if candidate is None:
            raise TimeoutError(f"B09 {name} background planner did not return")
        if candidate.status is not SurfacePlanningStatus.COMPLETE:
            raise RuntimeError(
                f"B09 {name} planner returned {candidate.status.value}"
            )
        action_kinds = tuple(type(edge).__name__ for edge in candidate.segments)
        if action_kinds != expected_actions:
            raise RuntimeError(
                f"B09 {name} selected {action_kinds}, expected {expected_actions}"
            )
        admitted = RouteAdmitter().admit_surface(
            candidate, frame, expected_request_id=planning_request.request_id,
            goal_id=planning_request.goal_id,
            goal_revision=planning_request.goal_revision, changed_cells=(),
        )
        if admitted.status is not AdmissionStatus.ACCEPTED or admitted.route is None:
            raise RuntimeError(f"B09 {name} admission failed: {admitted.reason}")
        executor = ActionRouteExecutor(
            ground_profile, jump_profile, step_profile, air_profiles=profiles,
        )
        executor.start(admitted.route.action_route, frame)
        input_confirmed = True
        samples = []
        for _ in range(240):
            decision = executor.decide(frame, input_confirmed=input_confirmed)
            sample = dict(
                route=name,
                sequence=frame.body.sequence_id,
                state=decision.state.value,
                reason=decision.reason_code,
                action_index=decision.action_index,
                position=list(frame.body.position),
                velocity=list(frame.body.velocity_blocks_per_second),
                movement=asdict(decision.movement),
            )
            samples.append(sample)
            append_jsonl(directory / "b09-route-trace.jsonl", sample)
            if decision.state is ActionRouteState.COMPLETE:
                break
            if decision.state in {
                    ActionRouteState.CANCELLED, ActionRouteState.FAILED,
                    ActionRouteState.BLOCKED, ActionRouteState.NEEDS_INFORMATION,
                    ActionRouteState.UNSUPPORTED, ActionRouteState.INPUT_LOST}:
                raise RuntimeError(
                    f"B09 {name} execution ended {decision.state.value}: "
                    f"{decision.reason_code}"
                )
            result = step(
                decision.movement, look=decision.look,
                request=observation_request,
            )
            input_confirmed = receipt_confirms_input(
                result.backend_result.receipt.status
            )
        else:
            raise TimeoutError(f"B09 {name} execution exceeded 240 frames")
        final_error = math.hypot(
            frame.body.position[0] - end_position[0],
            frame.body.position[2] - end_position[2],
        )
        route_trials.append(dict(
            name=name, status=executor.state.value,
            action_kinds=list(action_kinds), final_error_blocks=final_error,
            samples=samples,
        ))

    run_planned_route(
        "walk-gap-walk",
        (gap_route_x + .5, float(feet_y), origin_z - .5),
        (gap_route_x + .5, float(feet_y), origin_z + 3.5),
        KnownMapBounds(
            gap_route_x, gap_route_x, feet_y, feet_y,
            origin_z - 1, origin_z + 3, True, 2,
        ),
        gap_route_request,
        ("SurfaceWalkEdge", "SurfaceJumpGapEdge", "SurfaceWalkEdge"),
    )
    run_planned_route(
        "walk-drop-walk",
        (drop_route_x + .5, float(feet_y), origin_z - .5),
        (drop_route_x + .5, float(feet_y - 1), origin_z + 2.5),
        KnownMapBounds(
            drop_route_x, drop_route_x, feet_y - 1, feet_y,
            origin_z - 1, origin_z + 2, True, 2,
        ),
        drop_route_request,
        ("SurfaceWalkEdge", "SurfaceControlledDropEdge", "SurfaceWalkEdge"),
    )

    interruption_trials: list[dict] = []

    def run_airborne_interruption(name: str, *, lose_input: bool) -> None:
        start_position = (origin_x + .5, float(feet_y), origin_z + .5)
        end_position = (origin_x + .5, float(feet_y), origin_z + 2.5)
        teleport(start_position, 0.0, gap_request)
        controller = AirMotionController(by_mode[MovementMode.JUMP_GAP])
        controller.start(support_at(start_position), support_at(end_position), frame)
        input_confirmed = True
        interrupted = False
        samples = []
        for _ in range(48):
            if not frame.body.is_on_ground and not interrupted:
                interrupted = True
                if lose_input:
                    input_confirmed = False
                else:
                    controller.cancel()
            decision = controller.decide(frame, input_confirmed=input_confirmed)
            input_confirmed = True
            samples.append(dict(
                sequence=frame.body.sequence_id, state=decision.state.value,
                reason=decision.reason_code, position=list(frame.body.position),
                movement=asdict(decision.movement),
            ))
            if decision.state in AirMotionController._TERMINAL:
                break
            result = step(
                decision.movement, look=decision.look, request=gap_request,
            )
            input_confirmed = receipt_confirms_input(
                result.backend_result.receipt.status
            )
        expected = (
            AirMotionState.INPUT_LOST if lose_input else AirMotionState.CANCELLED
        )
        interruption_trials.append(dict(
            name=name, interrupted=interrupted,
            final_state=controller.state.value,
            expected_state=expected.value,
            final_position=list(frame.body.position), samples=samples,
        ))

    run_airborne_interruption("cancel-after-takeoff", lose_input=False)
    run_airborne_interruption("input-loss-after-takeoff", lose_input=True)

    for mode in (MovementMode.JUMP_GAP, MovementMode.CONTROLLED_DROP):
        for direction_index in range(4):
            for repetition in range(10):
                run_trial(mode, direction_index, repetition)

    summary = dict(
        schema_version="mc2p.b09-air-motion.v1",
        profiles=[profile.profile_id for profile in profiles],
        trial_count=len(trials),
        success_count=sum(
            trial["final_state"] == AirMotionState.COMPLETE.value for trial in trials
        ),
        by_mode={mode.value: {
            "trials": sum(trial["mode"] == mode.value for trial in trials),
            "successes": sum(
                trial["mode"] == mode.value
                and trial["final_state"] == AirMotionState.COMPLETE.value
                for trial in trials
            ),
            "maximum_horizontal_error_blocks": max(
                trial["horizontal_error_blocks"]
                for trial in trials if trial["mode"] == mode.value
            ),
            "maximum_level_error_blocks": max(
                trial["level_error_blocks"]
                for trial in trials if trial["mode"] == mode.value
            ),
        } for mode in (MovementMode.JUMP_GAP, MovementMode.CONTROLLED_DROP)},
        control_time_ns={
            "samples": len(control_times), "p95": _percentile(control_times, .95),
            "p99": _percentile(control_times, .99), "maximum": max(control_times),
        },
        planned_routes=route_trials,
        interruption_trials=interruption_trials,
        trials=trials,
    )
    write_json_atomic(directory / "b09-air-motion.json", summary)
    checks = [
        dict(name="b09_eighty_cardinal_air_trials_complete",
             passed=summary["trial_count"] == summary["success_count"] == 80),
        dict(name="b09_gap_uses_one_jump_and_drop_uses_none", passed=all(
            trial["jump_pulses"] == (1 if trial["mode"] == "jump_gap" else 0)
            for trial in trials
        )),
        dict(name="b09_no_horizontal_collision", passed=all(
            trial["horizontal_collisions"] == 0 for trial in trials
        )),
        dict(name="b09_landing_within_profiles", passed=all(
            trial["horizontal_error_blocks"]
            <= by_mode[MovementMode(trial["mode"])].landing_horizontal_radius_blocks
            and trial["level_error_blocks"]
            <= by_mode[MovementMode(trial["mode"])].landing_level_tolerance_blocks
            for trial in trials
        )),
        dict(name="b09_control_budget", passed=(
            summary["control_time_ns"]["p95"] <= 8_000_000
            and summary["control_time_ns"]["p99"] <= 15_000_000
            and summary["control_time_ns"]["maximum"] < 30_000_000
        )),
        dict(name="b09_background_routes_execute", passed=(
            len(route_trials) == 2
            and all(trial["status"] == ActionRouteState.COMPLETE.value
                    for trial in route_trials)
        )),
        dict(name="b09_airborne_interruptions_land_safely", passed=(
            len(interruption_trials) == 2
            and all(trial["interrupted"] for trial in interruption_trials)
            and all(trial["final_state"] == trial["expected_state"]
                    for trial in interruption_trials)
        )),
    ]
    return summary, rows, checks
