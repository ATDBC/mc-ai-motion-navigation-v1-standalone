"""Fabric B07 calibration for observed half-block StepUp and StepDown."""
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
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.fixed_route import (
    FixedRoute, FixedRouteController, FixedRouteState, RoutePoint,
)
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.geometry import QueryStatus, sweep
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SnapshotBuildStatus,
    SurfacePlanningRequest, SurfacePlanningStatus, build_surface_graph,
    astar_surface_plan,
)
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.step_transition import (
    StepController, StepState, load_step_profile, query_step,
)
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import Aabb, BlockPos, CellKnowledge
from scripts.control_probe_core import append_jsonl, write_json_atomic
from scripts.fixed_route_runtime_core import receipt_confirms_input


ROOT = Path(__file__).resolve().parents[1]
_SOURCE = "b07-step-calibration"


def summarize_step_trials(trials: list[dict], control_times_ns: list[int]) -> dict:
    ordered = sorted(control_times_ns)
    percentile = lambda ratio: ordered[min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1)]
    return {
        "schema_version": "mc2p.b07-step-calibration.v1",
        "trial_count": len(trials),
        "completed": sum(trial["final_state"] == StepState.COMPLETE.value for trial in trials),
        "jump_pulses": sum(trial["jump_pulses"] for trial in trials),
        "horizontal_collisions": sum(trial["horizontal_collisions"] for trial in trials),
        "maximum_level_error_blocks": max(trial["level_error_blocks"] for trial in trials),
        "control_time_ns": {
            "sample_count": len(ordered),
            "p95": percentile(.95),
            "p99": percentile(.99),
            "maximum": max(ordered),
        },
    }


def run_step_transition_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[BlockPos, ...], str], None],
    player_teleporter: Callable[[float, float, float, float, float], None],
) -> tuple[dict, list[dict], list[dict]]:
    task = TaskIntentV0(
        "b07-step-calibration", "half_block_step_calibration", "{}",
        (SuccessCriterionV0("valid_steps", ComparisonOperatorV0.EQUAL, 2, "count"),),
        300, deadline_ns, True, 0.0,
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

    def step(movement: MovementV1 = MovementV1(), *,
             look: LookV1 | None = None,
             request: ObservationRequestV3 | None = None):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"b07-step-{counter}", _SOURCE, episode,
            runtime.observation.sequence_id, ActionPriorityV0.TASK,
            now, min(deadline_ns, now + 750_000_000), movement=movement,
            look=look, valid_for_ticks=1,
        ))
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B07 Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B07 movement bypassed the formal arbiter")
        frame = adapter.ingest(result.observation)
        diagnostic()
        return result

    def look_at(yaw_degrees: float, pitch_degrees: float,
                request: ObservationRequestV3):
        yaw = math.degrees(frame.body.yaw_radians)
        pitch = math.degrees(frame.body.pitch_radians)
        delta = (yaw_degrees - yaw + 180.0) % 360.0 - 180.0
        return step(look=LookV1(delta, pitch_degrees - pitch), request=request)

    diagnostic()
    feet_y = math.floor(frame.body.position[1] + 1.0e-6)
    x = math.floor(frame.body.position[0])
    z = math.floor(frame.body.position[2])
    floor = (x, feet_y - 1, z)
    slab = (x, feet_y, z + 1)
    approach = (x, feet_y - 1, z - 1)
    upper = (x, feet_y, z + 2)
    exit_support = (x, feet_y, z + 3)
    volume = tuple(
        (cx, cy, cz)
        for cx in range(x - 1, x + 2)
        for cz in range(z - 1, z + 4)
        for cy in range(feet_y - 3, feet_y + 4)
    )
    route_supports = {approach, floor, slab, upper, exit_support}
    clear = tuple(position for position in volume if position not in route_supports)
    fixture_writer((approach, floor, upper, exit_support), "minecraft:grass_block")
    fixture_writer(clear, "minecraft:air")
    fixture_writer((slab,), "minecraft:smooth_stone_slab[type=bottom]")
    air_request = ObservationRequestV3("navigation_v1", volume)

    for _ in range(10):
        look_at(0.0, 65.0, air_request)
        if (frame.world.cell(floor).knowledge is CellKnowledge.BLOCK
                and frame.world.cell(slab).knowledge is CellKnowledge.BLOCK
                and all(frame.world.cell(position).knowledge is CellKnowledge.AIR
                        for position in clear)):
            break
    else:
        raise RuntimeError("B07 step fixture was not legally observed")

    environment = load_frozen_environment(
        ROOT / "config/motion-navigation/environment-v1.json"
    )
    profile = load_step_profile(
        ROOT / "config/motion-navigation/step-b07-v1.json",
        environment=environment,
    )
    catalog = BlockMotionCatalog.load(
        ROOT / "config/motion-navigation/block-motion-traits-v1.json",
        ROOT / "config/motion-navigation/vanilla-block-registry-1_21.json",
    )
    ground = load_ground_motion_profile(
        ROOT / "config/motion-navigation/ordinary-ground-b07-v1.json",
        environment=environment, catalog=catalog,
    )
    jump_profile = load_jump_up_profile(
        ROOT / "config/motion-navigation/jump-up-b06-v1.json",
        environment=environment, catalog=catalog,
    )
    floor_center = (x + .5, float(feet_y), z + .5)
    slab_center = (x + .5, float(feet_y) + .5, z + 1.5)
    trials = []
    walk_trials = []
    rejection_trials = []
    continuity_trials = []
    control_times_ns: list[int] = []

    def teleport(position: tuple[float, float, float], yaw: float) -> None:
        nonlocal frame
        player_teleporter(*position, yaw, 0.0)
        for _ in range(16):
            step(request=air_request)
            yaw_error = abs((math.degrees(frame.body.yaw_radians) - yaw + 180.0)
                            % 360.0 - 180.0)
            if (math.dist(frame.body.position, position) <= .03
                    and frame.body.is_on_ground
                    and math.hypot(frame.body.velocity_blocks_per_second[0],
                                   frame.body.velocity_blocks_per_second[2]) <= .03
                    and yaw_error <= 1.0):
                return
        raise RuntimeError("B07 step teleport did not settle")

    def surface_at(position: tuple[float, float, float]):
        result = query_support_surfaces(
            frame.world, math.floor(position[0]), math.floor(position[2]),
            position[1] - .1, position[1] + .1,
        )
        return min(result.surfaces, key=lambda item: abs(item.position[1] - position[1]))

    def run_step_trial(label, start_surface, end_surface, yaw=0.0) -> None:
        teleport(start_surface.position, yaw)
        controller = StepController(profile)
        controller.start(start_surface, end_surface, frame)
        samples = []
        for _ in range(profile.maximum_ticks + 4):
            decision = controller.decide(frame)
            control_times_ns.append(decision.control_time_ns)
            samples.append({
                "sequence": frame.body.sequence_id,
                "state": decision.state.value,
                "reason": decision.reason_code,
                "position": list(frame.body.position),
                "velocity_blocks_per_second": list(frame.body.velocity_blocks_per_second),
                "is_on_ground": frame.body.is_on_ground,
                "horizontal_collision": frame.body.horizontal_collision,
                "movement": asdict(decision.movement),
            })
            append_jsonl(directory / "b07-step-samples.jsonl", {
                "trial": label, **samples[-1],
            })
            if decision.state in {
                StepState.COMPLETE, StepState.FAILED, StepState.BLOCKED,
                StepState.NEEDS_INFORMATION, StepState.UNSUPPORTED,
                StepState.INPUT_LOST,
            }:
                break
            result = step(decision.movement, request=air_request)
            if not receipt_confirms_input(result.backend_result.receipt.status):
                controller.decide(frame, input_confirmed=False)
        level_error = abs(frame.body.position[1] - end_surface.position[1])
        trials.append({
            "name": label,
            "final_state": controller.state.value,
            "final_position": list(frame.body.position),
            "target_position": list(end_surface.position),
            "level_error_blocks": level_error,
            "horizontal_error_blocks": math.hypot(
                frame.body.position[0] - end_surface.position[0],
                frame.body.position[2] - end_surface.position[2],
            ),
            "jump_pulses": sum(bool(row["movement"]["jump"]) for row in samples),
            "horizontal_collisions": sum(bool(row["horizontal_collision"]) for row in samples),
            "sample_count": len(samples),
        })

    def run_walk_trial(label, start_position, end_position, yaw=0.0) -> None:
        teleport(start_position, yaw)
        controller = FixedRouteController(ground)
        controller.start(FixedRoute(
            f"b07-{label}",
            (RoutePoint(*start_position), RoutePoint(*end_position)),
        ), frame)
        input_confirmed = True
        samples = []
        for _ in range(80):
            decision = controller.decide(frame, input_confirmed=input_confirmed)
            control_times_ns.append(decision.control_time_ns)
            samples.append({
                "sequence": frame.body.sequence_id,
                "state": decision.state.value,
                "reason": decision.reason,
                "position": list(frame.body.position),
                "movement": asdict(decision.movement),
            })
            if decision.state is FixedRouteState.SUCCEEDED:
                break
            if decision.state in {
                FixedRouteState.BLOCKED, FixedRouteState.FAILED,
                FixedRouteState.INPUT_LOST, FixedRouteState.UNSUPPORTED,
            }:
                break
            result = step(decision.movement, request=air_request)
            input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        walk_trials.append({
            "name": label,
            "final_state": controller.state.value,
            "final_position": list(frame.body.position),
            "target_position": list(end_position),
            "horizontal_error_blocks": math.hypot(
                frame.body.position[0] - end_position[0],
                frame.body.position[2] - end_position[2],
            ),
            "sample_count": len(samples),
        })
        append_jsonl(directory / "b07-walk-samples.jsonl", {
            "trial": label, "samples": samples,
        })

    run_step_trial("up", surface_at(floor_center), surface_at(slab_center), 0.0)
    run_step_trial("down", surface_at(slab_center), surface_at(floor_center), 180.0)

    continuity_start_position = (x + .5, float(feet_y), z - .5)
    continuity_goal_position = (x + .5, float(feet_y + 1), z + 3.5)
    teleport(continuity_start_position, 0.0)
    continuity_bounds = KnownMapBounds(
        x, x, feet_y, feet_y + 1, z - 1, z + 3, True,
    )
    continuity_builder = KnownMapSnapshotBuilder(frame.world, continuity_bounds)
    continuity_snapshot = continuity_builder.advance(frame.world, 10_000)
    if (continuity_snapshot.status is not SnapshotBuildStatus.COMPLETE
            or continuity_snapshot.snapshot is None):
        raise RuntimeError("B10 Step continuity snapshot did not complete")
    continuity_graph = build_surface_graph(
        continuity_snapshot.snapshot.world, continuity_snapshot.snapshot.bounds,
        ground, profile,
    )
    continuity_start = surface_at((x + .5, float(feet_y), z - .5)).node_id
    continuity_goal = surface_at((x + .5, float(feet_y + 1), z + 3.5)).node_id
    continuity_request = SurfacePlanningRequest(
        1, f"{episode}-step-continuity", "b10-step-continuity-goal", 1,
        frame.session.value, continuity_start, continuity_goal,
    )
    continuity_candidate = astar_surface_plan(
        continuity_graph, continuity_request,
    )
    if continuity_candidate.status is not SurfacePlanningStatus.COMPLETE:
        raise RuntimeError(
            "B10 Step continuity route failed: "
            f"{continuity_candidate.status.value}"
        )
    continuity_admission = RouteAdmitter().admit_surface(
        continuity_candidate, frame,
        expected_request_id=continuity_request.request_id,
        goal_id=continuity_request.goal_id,
        goal_revision=continuity_request.goal_revision,
        changed_cells=(),
    )
    if (continuity_admission.status is not AdmissionStatus.ACCEPTED
            or continuity_admission.route is None):
        raise RuntimeError(
            f"B10 Step continuity admission failed: {continuity_admission.reason}"
        )
    continuity_kinds = tuple(
        type(action).__name__
        for action in continuity_admission.route.action_route.actions
    )
    if continuity_kinds != (
            "WalkSegment", "StepSegment", "StepSegment", "WalkSegment"):
        raise RuntimeError(
            f"B10 Step continuity selected {continuity_kinds}"
        )
    for repetition in range(10):
        teleport(continuity_start_position, 0.0)
        executor = ActionRouteExecutor(ground, jump_profile, profile)
        executor.start(continuity_admission.route.action_route, frame)
        input_confirmed = True
        samples = []
        for _ in range(120):
            decision = executor.decide(frame, input_confirmed=input_confirmed)
            samples.append({
                "sequence": frame.body.sequence_id,
                "state": decision.state.value,
                "reason": decision.reason_code,
                "action_index": decision.action_index,
                "movement": asdict(decision.movement),
                "position": list(frame.body.position),
            })
            if decision.state is ActionRouteState.COMPLETE:
                break
            if decision.state in {
                    ActionRouteState.BLOCKED, ActionRouteState.FAILED,
                    ActionRouteState.INPUT_LOST, ActionRouteState.UNSUPPORTED,
                    ActionRouteState.NEEDS_INFORMATION}:
                raise RuntimeError(
                    "B10 Step continuity failed: "
                    f"{decision.state.value}/{decision.reason_code}"
                )
            result = step(decision.movement, request=air_request)
            input_confirmed = receipt_confirms_input(
                result.backend_result.receipt.status
            )
        error = math.dist(frame.body.position, continuity_goal_position)
        trial = {
            "repetition": repetition,
            "status": executor.state.value,
            "final_error_blocks": error,
            "action_kinds": list(continuity_kinds),
            "samples": samples,
        }
        continuity_trials.append(trial)
        append_jsonl(directory / "b10-step-continuity.jsonl", trial)

    # Read the collision shapes from the real Fabric observation path.  The
    # component matrix uses the same query code, but it must not be the source
    # of truth for Minecraft block-state geometry.
    shape_cases = (
        ("lower_slab", ((slab, "minecraft:smooth_stone_slab[type=bottom]"),)),
        ("upper_slab", (((x, feet_y - 1, z + 1),
                         "minecraft:smooth_stone_slab[type=top]"),)),
        ("straight_stair", ((slab,
                             "minecraft:oak_stairs[facing=south,half=bottom,shape=straight,waterlogged=false]"),)),
        ("corner_stair", (((x, feet_y, z + 2),
                           "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]"),
                          (slab,
                           "minecraft:oak_stairs[facing=south,half=bottom,shape=inner_left,waterlogged=false]"))),
        ("carpet", (((x, feet_y - 1, z + 1), "minecraft:grass_block"),
                    (slab, "minecraft:white_carpet"))),
        ("snow_2", (((x, feet_y - 1, z + 1), "minecraft:grass_block"),
                    (slab, "minecraft:snow[layers=2]"))),
        ("snow_4", (((x, feet_y - 1, z + 1), "minecraft:grass_block"),
                    (slab, "minecraft:snow[layers=4]"))),
        ("snow_5", (((x, feet_y - 1, z + 1), "minecraft:grass_block"),
                    (slab, "minecraft:snow[layers=5]"))),
        ("snow_6", (((x, feet_y - 1, z + 1), "minecraft:grass_block"),
                    (slab, "minecraft:snow[layers=6]"))),
        ("dirt_path", (((x, feet_y - 1, z + 1), "minecraft:dirt_path"),)),
        ("fence", ((slab, "minecraft:oak_fence"),)),
        ("wall", ((slab, "minecraft:cobblestone_wall"),)),
        ("bars", ((slab, "minecraft:iron_bars"),)),
        ("trapdoor_closed", ((slab,
                              "minecraft:oak_trapdoor[facing=north,half=bottom,open=false,powered=false,waterlogged=false]"),)),
        ("trapdoor_open", ((slab,
                            "minecraft:oak_trapdoor[facing=north,half=bottom,open=true,powered=false,waterlogged=false]"),)),
        ("farmland", (((x, feet_y - 1, z + 1), "minecraft:farmland[moisture=0]"),)),
    )
    mutable = tuple(position for position in volume if position != floor)
    shape_observations = []
    for name, placements in shape_cases:
        fixture_writer(mutable, "minecraft:air")
        fixture_writer((floor,), "minecraft:grass_block")
        for position, material in placements:
            fixture_writer((position,), material)
        teleport(floor_center, 0.0)
        for yaw in (0.0, -90.0, 90.0, 180.0):
            look_at(yaw, 65.0, air_request)
        owner = slab if name == "corner_stair" else placements[-1][0]
        expected_state = next(material for position, material in placements
                              if position == owner)
        fact = frame.world.cell(owner)
        if (fact.knowledge is not CellKnowledge.BLOCK or fact.block is None
                or fact.block.material_key != expected_state.split("[", 1)[0]):
            raise RuntimeError(f"B07 shape was not formally observed: {name}/{owner}")
        surfaces = query_support_surfaces(
            frame.world, x, z + 1, feet_y - .25, feet_y + 2.1,
        )
        row = {
            "name": name,
            "owner": list(owner),
            "material": fact.block.material_key,
            "collision_kind": fact.block.collision_kind,
            "boxes": [list(box.as_tuple()) for box in fact.block.boxes],
            "surface_status": surfaces.status.value,
            "surface_heights": [surface.position[1] for surface in surfaces.surfaces],
            "surface_positions": [list(surface.position) for surface in surfaces.surfaces],
        }
        classification = catalog.classify(fact.block)
        row["trait_status"] = classification.status.value
        row["motion_effects"] = [effect.value for effect in classification.effects]
        if name in {"fence", "wall", "bars", "trapdoor_closed", "trapdoor_open"}:
            crossing = sweep(
                Aabb(x + .2, feet_y, z + .2, x + .8, feet_y + 1.8, z + .8),
                (0.0, 0.0, 1.5), frame.world,
            )
            row["crossing_status"] = crossing.status.value
            if name == "trapdoor_open":
                side_crossing = sweep(
                    Aabb(x - .8, feet_y, z + 1.1,
                         x - .2, feet_y + 1.8, z + 1.7),
                    (1.5, 0.0, 0.0), frame.world,
                )
                row["side_crossing_status"] = side_crossing.status.value
        shape_observations.append(row)
        append_jsonl(directory / "b07-shape-observations.jsonl", row)
        if name in {"straight_stair", "corner_stair"}:
            ordered_surfaces = sorted(surfaces.surfaces,
                                      key=lambda surface: surface.position[1])
            if len(ordered_surfaces) != 2:
                raise RuntimeError(f"B07 stair did not expose two treads: {name}")
            run_step_trial(f"{name}_up", ordered_surfaces[0], ordered_surfaces[1])
            run_step_trial(f"{name}_down", ordered_surfaces[1], ordered_surfaces[0], 180.0)
        elif name in {"carpet", "snow_2", "snow_4", "snow_5", "dirt_path"}:
            if len(surfaces.surfaces) != 1:
                raise RuntimeError(f"B07 surface fixture was ambiguous: {name}")
            base_surface = surface_at(floor_center)
            target_surface = surfaces.surfaces[0]
            run_step_trial(f"{name}_enter", base_surface, target_surface)
            run_step_trial(f"{name}_leave", target_surface, base_surface, 180.0)
        elif name == "upper_slab":
            if len(surfaces.surfaces) != 1:
                raise RuntimeError("B07 upper slab fixture was ambiguous")
            run_walk_trial("upper_slab_level", floor_center,
                           surfaces.surfaces[0].position)
        elif name == "snow_6":
            target_surface = surfaces.surfaces[0]
            result = query_step(frame.world, surface_at(floor_center),
                                target_surface, profile)
            rejection_trials.append({
                "name": "snow_6_height_boundary",
                "status": result.status.value,
                "reason": result.reason_code,
                "height_delta_blocks": result.height_delta_blocks,
            })

    # A continuous lower-slab lane uses ordinary Walk rather than one Step per
    # cell.  This catches accidental conversion of every fractional surface
    # into a stop-and-transition action.
    fixture_writer(mutable, "minecraft:air")
    fixture_writer((floor,), "minecraft:grass_block")
    lower_lane = ((x + 1, feet_y, z), (x + 1, feet_y, z + 1))
    fixture_writer(lower_lane, "minecraft:smooth_stone_slab[type=bottom]")
    teleport(floor_center, 0.0)
    for yaw in (0.0, -90.0, 90.0, 180.0):
        look_at(yaw, 65.0, air_request)
    lane_surfaces = tuple(
        query_support_surfaces(
            frame.world, column_x, column_z,
            feet_y + .4, feet_y + .6,
        ).surfaces[0]
        for column_x, _, column_z in lower_lane
    )
    run_walk_trial("continuous_lower_slab", lane_surfaces[0].position,
                   lane_surfaces[1].position)

    summary = summarize_step_trials(trials, control_times_ns)
    summary.update({
        "profile_id": profile.profile_id,
        "fixture": {"floor": list(floor), "slab": list(slab)},
        "trials": trials,
        "walk_trials": walk_trials,
        "continuity_trials": continuity_trials,
        "rejection_trials": rejection_trials,
        "shape_observations": shape_observations,
    })
    write_json_atomic(directory / "b07-step-calibration.json", summary)
    checks = [
        {"name": "b07_step_up_and_down_complete",
         "passed": summary["completed"] == summary["trial_count"]},
        {"name": "b07_step_never_uses_jump",
         "passed": summary["jump_pulses"] == 0},
        {"name": "b07_step_has_no_horizontal_collision",
         "passed": summary["horizontal_collisions"] == 0},
        {"name": "b07_step_level_is_observed",
         "passed": summary["maximum_level_error_blocks"] <= profile.target_level_tolerance_blocks},
        {"name": "b07_step_control_meets_budget",
         "passed": (summary["control_time_ns"]["p95"] <= 8_000_000
                    and summary["control_time_ns"]["p99"] <= 15_000_000
                    and summary["control_time_ns"]["maximum"] < 30_000_000)},
        {"name": "b07_representative_shapes_observed_from_fabric",
         "passed": len(shape_observations) == len(shape_cases)
         and all(row["collision_kind"] != "unsupported"
                 for row in shape_observations)},
        {"name": "b07_representative_shape_transitions_complete",
         "passed": bool(walk_trials)
         and all(row["final_state"] == FixedRouteState.SUCCEEDED.value
                 for row in walk_trials)},
        {"name": "b07_snow_height_boundary_is_rejected",
         "passed": len(rejection_trials) == 1
         and rejection_trials[0]["status"] == QueryStatus.UNSUPPORTED.value},
        {"name": "b07_obstacle_shapes_block_crossing",
         "passed": all(
             row.get("crossing_status") == QueryStatus.BLOCKED.value
             for row in shape_observations
             if row["name"] in {
                 "fence", "wall", "bars", "trapdoor_closed", "trapdoor_open",
             }
         )},
        {"name": "b07_open_trapdoor_passes_only_clear_side",
         "passed": next(
             row for row in shape_observations
             if row["name"] == "trapdoor_open"
         ).get("side_crossing_status") == QueryStatus.FEASIBLE.value},
        {"name": "b07_shape_motion_traits_match_declared_scope",
         "passed": all(
             row["trait_status"] == "supported"
             for row in shape_observations
             if row["name"] in {
                 "lower_slab", "upper_slab", "straight_stair", "corner_stair",
                 "carpet", "snow_2", "snow_4", "snow_5", "snow_6", "dirt_path",
             }
         ) and next(row for row in shape_observations
                    if row["name"] == "farmland")["trait_status"] == "deferred"},
        {"name": "b10_walk_step_step_walk_repeats_ten_times",
         "passed": len(continuity_trials) == 10
         and all(trial["status"] == ActionRouteState.COMPLETE.value
                 and trial["final_error_blocks"] <= .25
                 for trial in continuity_trials)},
    ]
    return summary, rows, checks
