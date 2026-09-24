"""Fabric validation that ordinary full-cube materials share B06 motion profiles."""
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
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import JumpUpController, JumpUpState, load_jump_up_profile
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockPos, CellFact, CellKnowledge
from scripts.control_probe_core import append_jsonl
from scripts.fixed_route_runtime_core import receipt_confirms_input


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
ORDINARY_MATERIALS = (
    "minecraft:dirt",
    "minecraft:glass",
    "minecraft:grass_block",
    "minecraft:oak_planks",
    "minecraft:stone",
)
_SOURCE = "b06-ordinary-material"


def cell_matches_material(cell: CellFact, material: str) -> bool:
    return (
        type(cell) is CellFact
        and cell.knowledge is CellKnowledge.BLOCK
        and cell.block is not None
        and cell.block.material_key == material
    )


def run_b06_ordinary_material_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[BlockPos, ...], str], None],
    player_teleporter: Callable[[float, float, float, float, float], None],
) -> tuple[dict, list[dict], list[dict]]:
    environment = load_frozen_environment(CONFIG / "environment-v1.json")
    catalog = BlockMotionCatalog.load(
        CONFIG / "block-motion-traits-v1.json",
        CONFIG / "vanilla-block-registry-1_21.json",
    )
    if ORDINARY_MATERIALS != tuple(sorted(catalog.materials_for_ground_model("ordinary-ground-v1"))):
        raise RuntimeError("B06 Fabric material list drifted from the motion catalog")
    ground = load_ground_motion_profile(
        CONFIG / "ordinary-ground-b06-v1.json", environment=environment, catalog=catalog,
    )
    jump = load_jump_up_profile(
        CONFIG / "jump-up-b06-v1.json", environment=environment, catalog=catalog,
    )
    task = TaskIntentV0(
        "b06-ordinary-material", "ordinary_material_equivalence", "{}",
        (SuccessCriterionV0("materials_passed", ComparisonOperatorV0.EQUAL,
                            len(ORDINARY_MATERIALS), "count"),),
        2000, deadline_ns, True, 0.0,
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

    def step(
        movement: MovementV1 = MovementV1(),
        *,
        look: LookV1 | None = None,
        request: ObservationRequestV3 | None = None,
        valid_for_ticks: int = 1,
    ):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = time.perf_counter_ns()
        runtime.submit_intent(ActionIntentV1(
            f"b06-material-{counter}", _SOURCE, episode,
            runtime.observation.sequence_id, ActionPriorityV0.TASK,
            now, min(deadline_ns, now + 750_000_000), movement=movement,
            look=look, valid_for_ticks=valid_for_ticks,
        ))
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B06 Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B06 movement bypassed the formal arbiter")
        frame = adapter.ingest(result.observation)
        diagnostic()
        return result

    diagnostic()
    feet_y = math.floor(frame.body.position[1] + 1.0e-6)
    center_x = math.floor(frame.body.position[0])
    center_z = math.floor(frame.body.position[2])
    floor = tuple(
        (center_x + dx, feet_y - 1, center_z + dz)
        for dx in range(-1, 2) for dz in range(-1, 4)
    )
    platform = (center_x + 1, feet_y, center_z)
    occupied_solids = set(floor) | {platform}
    air = tuple(
        (center_x + dx, y, center_z + dz)
        for dx in range(-2, 3) for dz in range(-2, 5)
        for y in range(feet_y, feet_y + 4)
        if (center_x + dx, y, center_z + dz) not in occupied_solids
    )
    air_request = ObservationRequestV3("navigation_v1", air)
    start_x, start_z = center_x + 0.5, center_z + 0.5

    def teleport(x: float, z: float, yaw: float) -> None:
        nonlocal frame
        player_teleporter(x, float(feet_y), z, yaw, 0.0)
        for _ in range(12):
            step(request=air_request)
            if (math.hypot(frame.body.position[0] - x, frame.body.position[2] - z) <= 0.02
                    and abs(frame.body.position[1] - feet_y) <= 0.02
                    and frame.body.is_on_ground):
                return
        raise RuntimeError("B06 material teleport did not settle")

    def scan_fixture(material: str) -> None:
        for yaw in (0.0, -90.0, 90.0, 180.0):
            for _ in range(2):
                current_yaw = math.degrees(frame.body.yaw_radians)
                current_pitch = math.degrees(frame.body.pitch_radians)
                yaw_delta = (yaw - current_yaw + 180.0) % 360.0 - 180.0
                step(look=LookV1(yaw_delta, 70.0 - current_pitch), request=air_request)
        required = ((center_x, feet_y - 1, center_z),
                    (center_x, feet_y - 1, center_z + 1), platform)
        for position in required:
            cell = frame.world.cell(position)
            if not cell_matches_material(cell, material):
                raise RuntimeError(f"B06 material support was not formally observed: {position}")

    results: list[dict] = []
    control_times_ns: list[int] = []
    for material in ORDINARY_MATERIALS:
        fixture_writer(air, "minecraft:air")
        fixture_writer(floor + (platform,), material)
        teleport(start_x, start_z, 0.0)
        scan_fixture(material)

        walk_route = FixedRoute(
            f"b06-walk-{material.split(':', 1)[1]}",
            (RoutePoint(start_x, float(feet_y), start_z),
             RoutePoint(start_x, float(feet_y), start_z + 1.25)),
        )
        walk_controller = FixedRouteController(ground)
        walk_controller.start(walk_route, frame)
        input_confirmed = True
        walk_frames = 0
        for walk_frames in range(1, 81):
            decision = walk_controller.decide(frame, input_confirmed=input_confirmed)
            control_times_ns.append(decision.control_time_ns)
            if decision.state is FixedRouteState.SUCCEEDED:
                break
            if decision.state in {
                FixedRouteState.BLOCKED, FixedRouteState.FAILED,
                FixedRouteState.INPUT_LOST, FixedRouteState.UNSUPPORTED,
            }:
                raise RuntimeError(
                    f"B06 {material} walk failed: {decision.state.value}/{decision.reason}"
                )
            request = air_request
            if decision.missing_cells:
                request, _ = adapter.air_request(decision.missing_cells)
            result = step(
                decision.movement, request=request,
                valid_for_ticks=decision.input_lease_ticks,
            )
            input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        else:
            raise TimeoutError(f"B06 {material} walk exceeded 80 frames")
        walk_end_position = frame.body.position

        teleport(start_x, start_z, -90.0)
        scan_fixture(material)
        start_node = (center_x, feet_y, center_z)
        end_node = (center_x + 1, feet_y + 1, center_z)
        jump_controller = JumpUpController(jump)
        jump_controller.start(start_node, end_node, frame)
        input_confirmed = True
        airborne = False
        jump_frames = 0
        jump_history: list[dict] = []
        for jump_frames in range(1, 51):
            decision = jump_controller.decide(frame, input_confirmed=input_confirmed)
            control_times_ns.append(decision.control_time_ns)
            airborne = airborne or not frame.body.is_on_ground
            jump_history.append(dict(
                state=decision.state.value,
                reason=decision.reason_code,
                position=list(frame.body.position),
                movement=asdict(decision.movement),
            ))
            if decision.state is JumpUpState.COMPLETE:
                break
            if decision.state in {
                JumpUpState.BLOCKED, JumpUpState.FAILED,
                JumpUpState.NEEDS_INFORMATION, JumpUpState.UNSUPPORTED,
            }:
                raise RuntimeError(
                    f"B06 {material} jump failed: {decision.state.value}/{decision.reason_code}"
                )
            result = step(decision.movement, look=decision.look, request=air_request)
            input_confirmed = receipt_confirms_input(result.backend_result.receipt.status)
        else:
            raise TimeoutError(f"B06 {material} jump exceeded 50 frames")
        result_row = dict(
            material=material,
            walk_frames=walk_frames,
            walk_end_position=list(walk_end_position),
            jump_frames=jump_frames,
            jump_airborne=airborne,
            landing_position=list(frame.body.position),
            jump_history=jump_history,
        )
        results.append(result_row)
        append_jsonl(directory / "b06-material-trials.jsonl", result_row)

    evidence = dict(
        schema_version="mc2p.b06-ordinary-material-evidence.v1",
        environment_id=environment.environment_id,
        ground_profile_id=ground.profile_id,
        jump_profile_id=jump.profile_id,
        materials=list(ORDINARY_MATERIALS),
        results=results,
        control_samples=len(control_times_ns),
        maximum_control_time_ns=max(control_times_ns, default=0),
    )
    checks = [
        dict(name="b06_all_ordinary_materials_walked_and_jumped",
             passed=len(results) == len(ORDINARY_MATERIALS)
             and all(row["jump_airborne"] for row in results)),
        dict(name="b06_profiles_bound_to_frozen_environment",
             passed=ground.environment_id == jump.environment_id == environment.environment_id),
    ]
    return evidence, rows, checks
