"""Fabric-only B04 known-map planning, admission and execution probe."""
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
from mc2p.motion_nav.fixed_route import (
    FixedRoute, FixedRouteController, FixedRouteState, RoutePoint,
)
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, PlanningRequest, PlanningStatus,
    SnapshotBuildStatus,
)
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.route_admission import (
    ActiveRouteTracker, AdmissionStatus, CorridorStatus, RouteAdmitter,
)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockPos, CellKnowledge
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fixed_route_runtime_core import _motion_profile, receipt_confirms_input


_SOURCE = "b04-known-map"


def run_known_map_navigation_runtime(
        runtime, backend, episode: str, directory: Path, deadline_ns: int,
        fixture_writer: Callable[[tuple[BlockPos, ...], str], None],
) -> tuple[dict, list[dict], list[dict]]:
    task = TaskIntentV0(
        "b04-known-map", "known_map_navigation", "{}",
        (SuccessCriterionV0("known_map_routes_completed", ComparisonOperatorV0.EQUAL,
                            4, "count"),),
        200, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    adapter = NavigationObservationAdapter()
    frame = adapter.ingest(runtime.observation)
    rows: list[dict] = []
    counter = 0

    def diagnostic() -> None:
        row = dict(episode_id=episode,
                   observation_sequence_id=runtime.observation.sequence_id,
                   diagnostics=backend.last_diagnostics)
        rows.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    def step(movement: MovementV1 = MovementV1(), *, look: LookV1 | None = None,
             request: ObservationRequestV3 | None = None, valid_for_ticks: int = 2):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"b04-{counter}", _SOURCE, episode, runtime.observation.sequence_id,
            ActionPriorityV0.TASK, now, min(deadline_ns, now + 750_000_000),
            movement=movement, look=look, valid_for_ticks=valid_for_ticks,
        ))
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B04 Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B04 movement bypassed the formal arbiter")
        frame = adapter.ingest(result.observation)
        diagnostic()
        return result

    def absolute_look(yaw_degrees: float, pitch_degrees: float,
                      request: ObservationRequestV3 | None = None) -> None:
        yaw = math.degrees(frame.body.yaw_radians)
        pitch = math.degrees(frame.body.pitch_radians)
        delta = (yaw_degrees - yaw + 180.0) % 360.0 - 180.0
        step(look=LookV1(delta, pitch_degrees - pitch), request=request,
             valid_for_ticks=1)

    diagnostic()
    feet_y = math.floor(frame.body.position[1] + 1e-6)
    start = (math.floor(frame.body.position[0]), feet_y,
             math.floor(frame.body.position[2]))
    end = (start[0], feet_y, start[2] + 8)
    bounds = KnownMapBounds(start[0] - 2, start[0] + 2, feet_y, feet_y,
                            start[2], start[2] + 8, True)
    prism_air = tuple(
        (x, y, z)
        for x in range(bounds.min_x, bounds.max_x + 1)
        for z in range(bounds.min_z, bounds.max_z + 1)
        for y in (feet_y, feet_y + 1)
    )
    air_request = ObservationRequestV3("navigation_v1", prism_air)

    # Four legal downward views establish the small floor.  The positive-only
    # query supplies air facts; it cannot disclose a hidden solid block.
    for yaw in (0.0, 90.0, 180.0, -90.0):
        absolute_look(yaw, 65.0, air_request)

    def survey_to(target_z: float, route_id: str) -> None:
        origin = frame.body.position
        route = FixedRoute(route_id, (
            RoutePoint(origin[0], origin[1], origin[2]),
            RoutePoint(origin[0], origin[1], target_z),
        ))
        controller = FixedRouteController(_motion_profile())
        controller.start(route, frame)
        travel_yaw = 0.0 if target_z >= origin[2] else 180.0
        absolute_look(travel_yaw, 65.0, air_request)
        confirmed = True
        for _ in range(220):
            decision = controller.decide(frame, input_confirmed=confirmed)
            if decision.state is FixedRouteState.SUCCEEDED:
                return
            if decision.state in {
                FixedRouteState.BLOCKED, FixedRouteState.FAILED,
                FixedRouteState.INPUT_LOST, FixedRouteState.UNSUPPORTED,
            }:
                raise RuntimeError(
                    f"B04 survey failed {decision.state.value}: {decision.reason}"
                )
            request = air_request
            if decision.missing_cells:
                request, _ = adapter.air_request(decision.missing_cells)
            result = step(decision.movement, request=request,
                          valid_for_ticks=decision.input_lease_ticks)
            confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        raise TimeoutError("B04 legal map survey exceeded 220 frames")

    survey_origin_z = frame.body.position[2]
    survey_to(survey_origin_z + 8.0, f"{episode}-survey-out")
    survey_to(survey_origin_z, f"{episode}-survey-back")
    for yaw in (0.0, 90.0, 180.0, -90.0):
        absolute_look(yaw, 65.0, air_request)

    def snapshot(*, require_complete_scope: bool = True):
        builder = KnownMapSnapshotBuilder(frame.world, bounds)
        progress = builder.advance(frame.world, 512)
        if progress.status is not SnapshotBuildStatus.COMPLETE:
            raise RuntimeError(
                f"B04 planning prism is not complete: {progress.status.value} "
                f"{progress.scanned_cells}/{progress.total_cells}"
            )
        if require_complete_scope and not progress.snapshot.bounds.complete_scope:
            raise RuntimeError("B04 planning prism still contains unknown cells")
        return progress.snapshot

    # Fail before changing the fixture if legal observation did not establish
    # the complete-map premise of this stage.
    snapshot()
    obstacle_z = start[2] + 4
    fixtures = {
        "open": (),
        "wall": ((start[0], feet_y, obstacle_z),
                 (start[0], feet_y + 1, obstacle_z)),
        "pit": ((start[0], feet_y - 1, obstacle_z),),
        "floating": ((start[0], feet_y + 1, obstacle_z),),
    }

    def synchronize(positions: tuple[BlockPos, ...], material: str) -> None:
        if not positions:
            return
        fixture_writer(positions, material)
        expected = CellKnowledge.AIR if material == "minecraft:air" else CellKnowledge.BLOCK
        requested = ObservationRequestV3("navigation_v1", positions) if expected is CellKnowledge.AIR else air_request
        target_x = sum(x + .5 for x, _, _ in positions) / len(positions)
        target_y = sum(y + .5 for _, y, _ in positions) / len(positions)
        target_z = sum(z + .5 for _, _, z in positions) / len(positions)
        for _ in range(8):
            dx = target_x - frame.body.position[0]
            dz = target_z - frame.body.position[2]
            target_yaw = math.degrees(math.atan2(-dx, dz))
            horizontal = math.hypot(dx, dz)
            target_pitch = math.degrees(math.atan2(
                frame.body.position[1] + 1.62 - target_y, max(horizontal, .01),
            ))
            absolute_look(target_yaw, target_pitch, requested)
            if all(frame.world.cell(position).knowledge is expected for position in positions):
                return
        raise RuntimeError(f"B04 fixture did not become {expected.value}: {positions}")

    scenarios: list[dict] = []
    current = start
    profile = _motion_profile()
    with PlannerWorker() as worker:
        for index, name in enumerate(("open", "wall", "floating", "pit"), 1):
            positions = fixtures[name]
            if name in {"wall", "floating"}:
                synchronize(positions, "minecraft:stone")
            elif name == "pit":
                synchronize(positions, "minecraft:air")

            target = end if current == start else start
            # Knowing that the immediate support block is air is sufficient
            # to reject the same-level pit cell.  The still-hidden substrate
            # may change the pit depth, but it cannot invalidate the known
            # safe detour that this scenario is meant to exercise.
            current_snapshot = snapshot(require_complete_scope=name != "pit")
            request = PlanningRequest(
                index, f"{episode}-{name}", f"goal-{name}", index,
                frame.session.value, current, target,
            )
            submitted = time.perf_counter_ns()
            worker.submit_snapshot(current_snapshot, profile, request)
            maximum_poll_ns = 0
            candidate = None
            waiting_polls = 0
            planning_deadline = time.perf_counter() + 3.0
            while candidate is None and time.perf_counter() < planning_deadline:
                before = time.perf_counter_ns()
                candidate = worker.poll_latest()
                maximum_poll_ns = max(maximum_poll_ns, time.perf_counter_ns() - before)
                if candidate is None:
                    waiting_polls += 1
                    time.sleep(.005)
            if candidate is None:
                raise TimeoutError(f"B04 {name} planner did not return in three seconds")
            planning_elapsed_ms = (time.perf_counter_ns() - submitted) / 1e6
            if candidate.status is not PlanningStatus.COMPLETE:
                raise RuntimeError(f"B04 {name} planner returned {candidate.status.value}")
            admitted = RouteAdmitter(maximum_corridor_blocks=12).admit(
                candidate, frame, expected_request_id=request.request_id,
                goal_id=request.goal_id, goal_revision=request.goal_revision,
                changed_cells=(),
            )
            if admitted.status is not AdmissionStatus.ACCEPTED or admitted.route is None:
                raise RuntimeError(f"B04 {name} route rejected: {admitted.reason}")
            tracker = ActiveRouteTracker(
                admitted.route, candidate, maximum_corridor_blocks=12,
            )
            controller = FixedRouteController(profile)
            controller.start(admitted.route.fixed_route, frame)
            input_confirmed = True
            decisions: list[dict] = []
            for _ in range(300):
                decision = controller.decide(frame, input_confirmed=input_confirmed)
                corridor = tracker.update(decision.progress_blocks)
                if corridor.status is not CorridorStatus.READY:
                    raise RuntimeError(f"B04 {name} active corridor became invalid")
                decisions.append(dict(
                    sequence=frame.body.sequence_id, state=decision.state.value,
                    reason=decision.reason, progress=decision.progress_blocks,
                    control_time_ns=decision.control_time_ns,
                    position=list(frame.body.position), movement=asdict(decision.movement),
                ))
                if decision.state is FixedRouteState.SUCCEEDED:
                    break
                if decision.state in {
                    FixedRouteState.BLOCKED, FixedRouteState.FAILED,
                    FixedRouteState.INPUT_LOST, FixedRouteState.UNSUPPORTED,
                }:
                    raise RuntimeError(
                        f"B04 {name} execution ended {decision.state.value}: {decision.reason}"
                    )
                missing_request = None
                if decision.missing_cells:
                    missing_request, _ = adapter.air_request(decision.missing_cells)
                result = step(
                    decision.movement, request=missing_request,
                    valid_for_ticks=decision.input_lease_ticks,
                )
                tracker.apply_changes(frame.changed_cells)
                input_confirmed = receipt_confirms_input(
                    result.backend_result.receipt.status)
            else:
                raise TimeoutError(f"B04 {name} execution exceeded 300 frames")

            final_error = math.hypot(
                frame.body.position[0] - (target[0] + .5),
                frame.body.position[2] - (target[2] + .5),
            )
            final_speed = math.hypot(
                frame.body.velocity_blocks_per_second[0],
                frame.body.velocity_blocks_per_second[2],
            )
            scenario = dict(
                name=name, request_id=request.request_id,
                planning_status=candidate.status.value,
                planning_ms=planning_elapsed_ms,
                waiting_polls=waiting_polls, maximum_poll_ms=maximum_poll_ns / 1e6,
                expanded_nodes=candidate.expanded_nodes,
                graph_path=[list(node.node_id) for node in candidate.path],
                fixed_route=[asdict(point) for point in admitted.route.fixed_route.points],
                route_id=admitted.route.route_id,
                final_error_blocks=final_error,
                final_speed_blocks_per_second=final_speed,
                decisions=decisions,
            )
            scenarios.append(scenario)
            append_jsonl(directory / "b04-scenarios.jsonl", scenario)
            current = target

            if name == "wall":
                synchronize(positions, "minecraft:air")
            elif name == "floating":
                synchronize(positions, "minecraft:air")

    evidence = dict(
        schema_version="mc2p.b04-known-map-evidence.v1",
        bounds=asdict(bounds), start=list(start), end=list(end), scenarios=scenarios,
    )
    write_json_atomic(directory / "b04-known-map.json", evidence)
    checks = [
        dict(name="b04_four_known_map_scenarios_completed",
             passed=len(scenarios) == 4 and all(
                 item["planning_status"] == PlanningStatus.COMPLETE.value
                 and item["final_error_blocks"] <= .25
                 and item["final_speed_blocks_per_second"] <= .1
                 for item in scenarios)),
        dict(name="b04_wall_pit_and_floating_detour",
             passed=all(any(node[0] != start[0] for node in item["graph_path"])
                        for item in scenarios if item["name"] != "open")),
        dict(name="b04_control_and_poll_are_bounded",
             passed=all(item["maximum_poll_ms"] < 20 and all(
                 decision["control_time_ns"] < 30_000_000
                 for decision in item["decisions"])
                        for item in scenarios)),
    ]
    return evidence, rows, checks
