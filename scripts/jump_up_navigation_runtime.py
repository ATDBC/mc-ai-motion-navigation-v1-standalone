"""Fabric-only B05 Walk-JumpUp-Walk planning and execution probe."""
from __future__ import annotations

from dataclasses import asdict, replace
import math
from pathlib import Path
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.action_route import ActionRoute, JumpUpSegment, WalkSegment
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_modes import load_ground_mode_profiles
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, PlanningRequest, PlanningStatus,
    SnapshotBuildStatus,
)
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.movement_transition import (
    CancellationMode, MovementMode, MovementStateClass, MovementTransition,
    ResourceChange, ResourceState,
)
from mc2p.motion_nav.route_admission import (
    ActiveRouteTracker, AdmissionStatus, CorridorStatus, RouteAdmitter,
)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fixed_route_runtime_core import _motion_profile, receipt_confirms_input


ROOT = Path(__file__).resolve().parents[1]
_SOURCE = "b05-jump-route"


def run_jump_up_navigation_runtime(
        runtime, backend, episode: str, directory: Path, deadline_ns: int,
        fixture_writer: Callable[[tuple[BlockPos, ...], str], None],
        player_teleporter: Callable[[float, float, float, float, float], None],
) -> tuple[dict, list[dict], list[dict]]:
    task = TaskIntentV0(
        "b05-jump-route", "walk_jump_up_walk", "{}",
        (SuccessCriterionV0("jump_route_completed", ComparisonOperatorV0.EQUAL, 20, "count"),),
        400, deadline_ns, True, 0.0,
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
             request: ObservationRequestV3 | None = None, valid_for_ticks: int = 1):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"b05-route-{counter}", _SOURCE, episode,
            runtime.observation.sequence_id, ActionPriorityV0.TASK,
            now, min(deadline_ns, now + 750_000_000), movement=movement,
            look=look, valid_for_ticks=valid_for_ticks,
        ))
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B05 route Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B05 route movement bypassed the formal arbiter")
        frame = adapter.ingest(result.observation)
        diagnostic()
        return result

    def look_at(position: BlockPos, request: ObservationRequestV3) -> None:
        target_x, target_y, target_z = position[0] + .5, position[1] + 1.0, position[2] + .5
        dx = target_x - frame.body.position[0]
        dz = target_z - frame.body.position[2]
        target_yaw = math.degrees(math.atan2(-dx, dz))
        horizontal = max(.01, math.hypot(dx, dz))
        target_pitch = math.degrees(math.atan2(
            frame.body.position[1] + 1.62 - target_y, horizontal,
        ))
        yaw = math.degrees(frame.body.yaw_radians)
        pitch = math.degrees(frame.body.pitch_radians)
        yaw_delta = (target_yaw - yaw + 180.0) % 360.0 - 180.0
        step(look=LookV1(yaw_delta, target_pitch - pitch), request=request)

    diagnostic()
    feet_y = math.floor(frame.body.position[1] + 1e-6)
    x = math.floor(frame.body.position[0])
    start_z = math.floor(frame.body.position[2])
    start = (x, feet_y, start_z)
    jump_start = (x, feet_y, start_z + 2)
    jump_end = (x, feet_y + 1, start_z + 3)
    goal = (x, feet_y + 1, start_z + 6)
    local_xs = range(x - 1, x + 2)
    lower_support = tuple((local_x, feet_y - 1, z)
                          for local_x in local_xs
                          for z in range(start_z, start_z + 3))
    upper_support = tuple((local_x, feet_y, z)
                          for local_x in local_xs
                          for z in range(start_z + 3, start_z + 7))
    buried_air = tuple((local_x, feet_y - 1, z)
                       for local_x in local_xs
                       for z in range(start_z + 3, start_z + 7))
    clear_air = tuple(
        (local_x, y, z)
        for local_x in local_xs
        for z in range(start_z, start_z + 7)
        for y in range(feet_y, feet_y + 4)
        if (local_x, y, z) not in set(upper_support)
    )
    fixture_writer(lower_support + upper_support, "minecraft:grass_block")
    fixture_writer(buried_air + clear_air, "minecraft:air")
    air_request = ObservationRequestV3("navigation_v1", buried_air + clear_air)

    for support in lower_support + upper_support:
        for _ in range(3):
            look_at(support, air_request)
            if frame.world.cell(support).knowledge is CellKnowledge.BLOCK:
                break
        else:
            raise RuntimeError(f"B05 support was not legally observed: {support}")

    bounds = KnownMapBounds(x, x, feet_y, feet_y + 1,
                            start_z, start_z + 6, True, 1)
    builder = KnownMapSnapshotBuilder(frame.world, bounds)
    progress = builder.advance(frame.world, 512)
    if progress.status is not SnapshotBuildStatus.COMPLETE or progress.snapshot is None:
        raise RuntimeError("B05 multilevel snapshot did not complete")

    ground_profile = _motion_profile()
    jump_profile = load_jump_up_profile(
        ROOT / "config/motion-navigation/jump-up-v1.json"
    )
    request = PlanningRequest(
        1, f"{episode}-jump-route", "b05-upper-goal", 1,
        frame.session.value, start, goal,
    )
    with PlannerWorker() as worker:
        submitted = time.perf_counter_ns()
        worker.submit_snapshot(progress.snapshot, ground_profile, request, jump_profile)
        candidate = None
        maximum_poll_ns = 0
        waiting_polls = 0
        planning_deadline = time.perf_counter() + 3
        while candidate is None and time.perf_counter() < planning_deadline:
            before = time.perf_counter_ns()
            candidate = worker.poll_latest()
            maximum_poll_ns = max(maximum_poll_ns, time.perf_counter_ns() - before)
            if candidate is None:
                waiting_polls += 1
                time.sleep(.005)
        if candidate is None:
            raise TimeoutError("B05 background planner did not return")
        planning_ms = (time.perf_counter_ns() - submitted) / 1e6
    if candidate.status is not PlanningStatus.COMPLETE:
        raise RuntimeError(f"B05 planner returned {candidate.status.value}")
    if not any(node.node_id == jump_start for node in candidate.path):
        raise RuntimeError("B05 route did not enter the declared takeoff node")

    admitted = RouteAdmitter(maximum_corridor_blocks=12).admit(
        candidate, frame, expected_request_id=request.request_id,
        goal_id=request.goal_id, goal_revision=request.goal_revision,
        changed_cells=(),
    )
    if admitted.status is not AdmissionStatus.ACCEPTED or admitted.route is None:
        raise RuntimeError(f"B05 route rejected: {admitted.reason}")
    action_kinds = tuple(
        "walk" if type(action) is WalkSegment else "jump_up"
        for action in admitted.route.action_route.actions
    )
    if action_kinds != ("walk", "jump_up", "walk"):
        raise RuntimeError(f"B05 route actions are not Walk-JumpUp-Walk: {action_kinds}")

    environment = load_frozen_environment(
        ROOT / "config/motion-navigation/environment-v1.json"
    )
    catalog = BlockMotionCatalog.load(
        ROOT / "config/motion-navigation/block-motion-traits-v1.json",
        ROOT / "config/motion-navigation/vanilla-block-registry-1_21.json",
    )
    ground_modes = load_ground_mode_profiles(
        ROOT / "config/motion-navigation/ground-modes-b08-v1.json",
        environment=environment, catalog=catalog,
    )
    sprint = ground_modes.require(MovementMode.SPRINT)
    sprint_state = MovementStateClass(
        MovementMode.SPRINT, "standing", 0.0,
        sprint.motion.maximum_speed_blocks_per_second,
    )
    prefix = admitted.route.action_route.actions[0]
    jump_action = admitted.route.action_route.actions[1]
    assert type(prefix) is WalkSegment and type(jump_action) is JumpUpSegment
    sprint_transition = MovementTransition(
        f"{sprint.motion.profile_id}/b10-sprint-prefix",
        sprint.motion.environment_id,
        MovementMode.SPRINT, sprint_state, (sprint_state,),
        prefix.transition.duration_seconds, prefix.dependencies,
        ResourceChange(), CancellationMode.GROUND_STOP,
        CancellationMode.GROUND_STOP,
        trajectory_profile_id=sprint.motion.profile_id,
        minimum_entry_resources=ResourceState((
            ("food_points", float(sprint.minimum_food_points)),
        )),
    )
    sprint_route = ActionRoute(
        "b10-sprint-brake-jump",
        (replace(prefix, transition=sprint_transition), jump_action),
    )

    start_position = (start[0] + .5, float(start[1]), start[2] + .5)

    def teleport_start() -> None:
        nonlocal frame
        player_teleporter(*start_position, 0.0, 0.0)
        for _ in range(16):
            step(request=air_request)
            speed = math.hypot(
                frame.body.velocity_blocks_per_second[0],
                frame.body.velocity_blocks_per_second[2],
            )
            if (math.dist(frame.body.position, start_position) <= .03
                    and frame.body.is_on_ground and speed <= .03):
                return
        raise RuntimeError("B10 JumpUp route teleport did not settle")

    def execute_route(label: str, repetition: int, route: ActionRoute, *,
                      modes=None, target=None) -> dict:
        teleport_start()
        tracker = ActiveRouteTracker(
            admitted.route, candidate, maximum_corridor_blocks=12,
        ) if label == "walk-jump-walk" else None
        executor = ActionRouteExecutor(
            ground_profile, jump_profile, ground_modes=modes,
        )
        executor.start(route, frame)
        input_confirmed = True
        decisions: list[dict] = []
        observed_airborne = False
        jump_entry_speed = None
        for _ in range(320):
            decision = executor.decide(frame, input_confirmed=input_confirmed)
            if tracker is not None:
                corridor = tracker.update(0.0)
                if corridor.status is not CorridorStatus.READY:
                    raise RuntimeError("B05 active action corridor became invalid")
            observed_airborne = observed_airborne or not frame.body.is_on_ground
            if decision.movement.jump and jump_entry_speed is None:
                jump_entry_speed = math.hypot(
                    frame.body.velocity_blocks_per_second[0],
                    frame.body.velocity_blocks_per_second[2],
                )
            row = dict(
                group=label, repetition=repetition,
                sequence=frame.body.sequence_id,
                state=decision.state.value,
                action_index=decision.action_index,
                reason=decision.reason_code,
                control_time_ns=decision.control_time_ns,
                position=list(frame.body.position),
                velocity=list(frame.body.velocity_blocks_per_second),
                is_on_ground=frame.body.is_on_ground,
                movement=asdict(decision.movement),
                look=None if decision.look is None else asdict(decision.look),
                missing_cells=[list(cell) for cell in decision.missing_cells],
            )
            decisions.append(row)
            append_jsonl(directory / "b05-route-decisions.jsonl", row)
            if decision.state is ActionRouteState.COMPLETE:
                break
            if decision.state in {
                ActionRouteState.BLOCKED, ActionRouteState.FAILED,
                ActionRouteState.INPUT_LOST, ActionRouteState.UNSUPPORTED,
                ActionRouteState.NEEDS_INFORMATION,
            }:
                raise RuntimeError(
                    f"B10 {label} ended {decision.state.value}: "
                    f"{decision.reason_code}; missing={decision.missing_cells}"
                )
            observation_request = None
            if decision.missing_cells:
                observation_request, _ = adapter.air_request(decision.missing_cells)
            result = step(
                decision.movement, look=decision.look,
                request=observation_request,
                valid_for_ticks=decision.input_lease_ticks,
            )
            if tracker is not None:
                tracker.apply_changes(frame.changed_cells)
            input_confirmed = receipt_confirms_input(
                result.backend_result.receipt.status
            )
        else:
            raise TimeoutError(f"B10 {label} exceeded 320 frames")
        target_position = target or (
            goal[0] + .5, float(goal[1]), goal[2] + .5,
        )
        return dict(
            group=label, repetition=repetition, status=executor.state.value,
            observed_airborne=observed_airborne,
            jump_entry_speed_blocks_per_second=jump_entry_speed,
            final_error_blocks=math.dist(frame.body.position, target_position),
            final_speed_blocks_per_second=math.hypot(
                frame.body.velocity_blocks_per_second[0],
                frame.body.velocity_blocks_per_second[2],
            ),
            decisions=decisions,
        )

    continuity_trials = []
    for repetition in range(10):
        continuity_trials.append(execute_route(
            "walk-jump-walk", repetition,
            admitted.route.action_route,
        ))
    jump_target = (
        jump_end[0] + .5, float(jump_end[1]), jump_end[2] + .5,
    )
    for repetition in range(10):
        continuity_trials.append(execute_route(
            "sprint-brake-jump", repetition, sprint_route,
            modes=ground_modes, target=jump_target,
        ))

    decisions = [
        decision for trial in continuity_trials for decision in trial["decisions"]
    ]
    observed_airborne = all(
        trial["observed_airborne"] for trial in continuity_trials
    )
    final_error = max(trial["final_error_blocks"] for trial in continuity_trials)
    final_speed = max(
        trial["final_speed_blocks_per_second"] for trial in continuity_trials
    )
    evidence = dict(
        schema_version="mc2p.b05-jump-route-evidence.v1",
        bounds=asdict(bounds), start=list(start), jump_start=list(jump_start),
        jump_end=list(jump_end), goal=list(goal),
        planning_status=candidate.status.value, planning_ms=planning_ms,
        waiting_polls=waiting_polls, maximum_poll_ms=maximum_poll_ns / 1e6,
        expanded_nodes=candidate.expanded_nodes,
        graph_path=[list(node.node_id) for node in candidate.path],
        action_kinds=list(action_kinds), route_id=admitted.route.route_id,
        observed_airborne=observed_airborne,
        final_error_blocks=final_error,
        final_speed_blocks_per_second=final_speed,
        continuity_trials=continuity_trials,
        decisions=decisions,
    )
    write_json_atomic(directory / "b05-jump-route.json", evidence)
    checks = [
        dict(name="b05_background_planned_walk_jump_walk",
             passed=candidate.status is PlanningStatus.COMPLETE
             and action_kinds == ("walk", "jump_up", "walk")),
        dict(name="b05_route_observed_airborne_and_upper_landing",
             passed=observed_airborne and frame.body.is_on_ground
             and abs(frame.body.position[1] - goal[1]) <= .1),
        dict(name="b05_route_completed_at_goal",
             passed=all(
                 trial["status"] == ActionRouteState.COMPLETE.value
                 and trial["final_error_blocks"] <= .25
                 for trial in continuity_trials
             )),
        dict(name="b10_walk_and_sprint_jump_rows_repeat_ten_times",
             passed=(
                 len(continuity_trials) == 20
                 and sum(trial["group"] == "walk-jump-walk"
                         for trial in continuity_trials) == 10
                 and sum(trial["group"] == "sprint-brake-jump"
                         for trial in continuity_trials) == 10
                 and all(
                     trial["jump_entry_speed_blocks_per_second"] is not None
                     and trial["jump_entry_speed_blocks_per_second"]
                     <= jump_profile.maximum_entry_speed_blocks_per_second + 1e-6
                     for trial in continuity_trials
                 )
             )),
        dict(name="b05_control_and_poll_are_bounded",
             passed=maximum_poll_ns < 20_000_000
             and all(row["control_time_ns"] < 30_000_000 for row in decisions)),
    ]
    return evidence, rows, checks
