"""Representative real-Fabric acceptance for continuous-height movement."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import random
import time
from typing import Callable

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import OrderedIntentV1, ordered_intent_id
from mc2p.contracts.observation_request_v3 import (
    MAX_AIR_QUERY_POSITIONS, ObservationRequestV3,
)
from mc2p.contracts.task import (
    ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0,
)
from mc2p.motion_nav.movement_transition import (
    GoalState, GoalSupport, MovementMode,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.navigation_session import (
    NavigationSession, NavigationSessionProfiles,
)
from mc2p.motion_nav.motion_worker import MotionSolverWorker
from mc2p.motion_nav.online_motion import (
    ProjectionStatus, project_movement_command,
)
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, StateBuildStatus,
)
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.world_model import Aabb, CellKnowledge
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from scripts.control_probe_core import append_jsonl, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
FULL_MATRIX_MANIFEST = (
    ROOT / "tests/sim/manifests/continuous-height-full-matrix.json"
)
FEET_Y = 100
_DIRECTIONS = (
    (1, 0, -90.0),
    (0, 1, 0.0),
    (-1, 0, 90.0),
    (0, -1, 180.0),
)
_STATE_ASSUMPTIONS = dict(
    jumping_cooldown_ticks=0,
    movement_speed_attribute=.1,
    step_height_blocks=.6,
    gravity_attribute=.08,
    jump_strength_attribute=.42,
)


def continuous_height_trial_plan() -> tuple[dict, ...]:
    rows: list[dict] = []
    for direction_index in range(4):
        rows.append({
            "trial_id": f"low-height-stairs-{direction_index}",
            "kind": "low_height_stairs",
            "direction_index": direction_index,
        })
    for direction_index in range(4):
        rows.append({
            "trial_id": f"stair-descent-{direction_index}",
            "kind": "stair_descent",
            "direction_index": direction_index,
            "drop_blocks": 4,
        })
    for direction_index, drop_blocks in enumerate((1, 2, 3, 5)):
        rows.append({
            "trial_id": f"direct-drop-{drop_blocks}",
            "kind": "direct_drop",
            "direction_index": direction_index,
            "drop_blocks": drop_blocks,
        })
    return tuple(rows)


def _matrix_trial_geometry(
    family: str,
    direction_index: int,
    speed_band: str,
    family_index: int,
) -> dict:
    """Build one reusable real-game fixture for a frozen M3 row."""
    direction = ("east", "south", "west", "north")[direction_index]
    opposite = {"east": "west", "south": "north",
                "west": "east", "north": "south"}[direction]
    blocks: dict[tuple[int, int, int], str] = {}

    def full(u: int, feet_y: int, v: int = 0,
             material: str = "minecraft:stone") -> None:
        blocks[(u, feet_y - 1, v)] = material

    def decorate(u: int, y: int, material: str, v: int = 0) -> None:
        blocks[(u, y, v)] = material

    start_feet = FEET_Y
    goal_feet = FEET_Y
    transition_u = 1
    goal_u = 4
    kind = "low_height_route"
    drop_blocks = None
    max_ticks = 240

    if family == "slab_up_down":
        transition_u, goal_u, goal_feet = 2, 6, FEET_Y + 2
        for u, feet in enumerate((100, 100, 100, 101, 101, 102, 102)):
            full(u, feet)
        decorate(2, FEET_Y, "minecraft:smooth_stone_slab[type=bottom]")
        decorate(4, FEET_Y + 1,
                 "minecraft:smooth_stone_slab[type=bottom]")
    elif family == "slab_down_up":
        transition_u, goal_u = 2, 6
        start_feet, goal_feet = FEET_Y + 2, FEET_Y
        for u, feet in enumerate((102, 102, 101, 101, 100, 100, 100)):
            full(u, feet)
        decorate(2, FEET_Y + 1,
                 "minecraft:smooth_stone_slab[type=bottom]")
        decorate(4, FEET_Y,
                 "minecraft:smooth_stone_slab[type=bottom]")
    elif family in {"stairs_up", "stairs_down"}:
        transition_u, goal_u = 2, 6
        ascending = family == "stairs_up"
        start_feet = FEET_Y if ascending else FEET_Y + 2
        goal_feet = FEET_Y + 2 if ascending else FEET_Y
        feet_rows = ((100, 100, 100, 101, 101, 102, 102)
                     if ascending else
                     (102, 102, 101, 101, 100, 100, 100))
        for u, feet in enumerate(feet_rows):
            full(u, feet)
        stair_facing = direction if ascending else opposite
        first_y = FEET_Y if ascending else FEET_Y + 1
        second_y = FEET_Y + 1 if ascending else FEET_Y
        decorate(
            2, first_y,
            f"minecraft:oak_stairs[facing={stair_facing},half=bottom,shape=straight]",
        )
        decorate(
            4, second_y,
            f"minecraft:oak_stairs[facing={stair_facing},half=bottom,shape=straight]",
        )
    elif family == "dirt_path_alternating":
        transition_u, goal_u = 1, 8
        for u in range(9):
            full(u, FEET_Y, material=(
                "minecraft:stone" if u % 2 == 0 else "minecraft:dirt_path"
            ))
    elif family in {"carpet_alternating", "snow_layers_2",
                    "snow_layers_4", "snow_layers_5"}:
        transition_u, goal_u = 1, 4
        for u in range(5):
            full(u, FEET_Y)
        if family == "carpet_alternating":
            material = "minecraft:white_carpet"
        else:
            material = f"minecraft:snow[layers={family.rsplit('_', 1)[1]}]"
        decorate(1, FEET_Y, material)
        decorate(3, FEET_Y, material)
    elif family == "single_bottom_slab":
        transition_u, goal_u = 2, 4
        for u in range(5):
            full(u, FEET_Y)
        decorate(2, FEET_Y, "minecraft:smooth_stone_slab[type=bottom]")
    elif family == "low_height_then_brake":
        transition_u, goal_u, goal_feet = 1, 3, FEET_Y + 1
        full(0, FEET_Y)
        full(1, FEET_Y)
        decorate(1, FEET_Y, "minecraft:smooth_stone_slab[type=bottom]")
        full(2, FEET_Y + 1)
        full(3, FEET_Y + 1)
    elif family == "jump_up_1":
        kind = "jump_up"
        transition_u, goal_u, goal_feet = 2, 5, FEET_Y + 1
        for u, feet in enumerate((100, 100, 101, 101, 101, 101)):
            full(u, feet)
    elif family == "step_down_1":
        kind = "step_down"
        transition_u, goal_u = 2, 5
        start_feet, goal_feet = FEET_Y + 1, FEET_Y
        for u, feet in enumerate((101, 101, 100, 100, 100, 100)):
            full(u, feet)
    elif family.startswith("stair_descent_"):
        kind = "stair_descent"
        levels = int(family.rsplit("_", 1)[1])
        transition_u = 2
        start_feet = FEET_Y + levels
        feet_rows = (start_feet, start_feet) + tuple(
            start_feet - index for index in range(1, levels + 1)
        ) + (FEET_Y,)
        goal_u = len(feet_rows) - 1
        max_ticks = 600 if levels == 8 else 240
        for u, feet in enumerate(feet_rows):
            full(u, feet)
    elif family.startswith("direct_drop_"):
        kind = "direct_drop"
        drop_token = family.removeprefix("direct_drop_").split("_", 1)[0]
        drop_blocks = int(drop_token)
        transition_u, goal_u = 3, 5
        goal_feet = FEET_Y - drop_blocks
        for u in range(0, 3):
            full(u, FEET_Y)
        for u in range(3, 7):
            full(u, goal_feet)
    else:
        raise ValueError(f"unknown continuous-height family: {family}")

    # Full-block actions receive their requested entry speed through ordinary
    # player inputs on a three-block runway.  Small-height routes need no
    # synthetic speed preparation; their differing approach lengths remain a
    # useful timing variation, but do not claim to prove an entry-speed band.
    approach_cells = (
        {"low": 1, "medium": 2, "high": 4}[speed_band]
        if kind == "low_height_route" else 4
    )
    start_u = transition_u - approach_cells
    for u in range(start_u, transition_u):
        if kind == "direct_drop":
            full(u, start_feet)
        else:
            full(u, start_feet)

    slot = family_index * 4 + direction_index
    origin_x = (slot % 10) * 20
    origin_z = (slot // 10) * 20

    def world_cell(position: tuple[int, int, int]) -> tuple[int, int, int]:
        u, y, v = position
        x_offset, z_offset = _transform(direction_index, u, v)
        return origin_x + x_offset, y, origin_z + z_offset

    support_blocks = tuple(sorted(
        (world_cell(position), material)
        for position, material in blocks.items()
    ))

    def world_point(u: int, feet_y: float, v: int = 0) -> tuple[float, float, float]:
        x_offset, z_offset = _transform(direction_index, u, v)
        return origin_x + x_offset + .5, feet_y, origin_z + z_offset + .5

    start = world_point(start_u, float(start_feet))
    goal = world_point(goal_u, float(goal_feet))
    middle_u = (start_u + goal_u) // 2
    lower_x, lower_z = _transform(direction_index, middle_u, -6)
    upper_x, upper_z = _transform(direction_index, middle_u, 6)
    lower_world_x = origin_x + lower_x + .5
    lower_world_z = origin_z + lower_z + .5
    upper_world_x = origin_x + upper_x + .5
    upper_world_z = origin_z + upper_z + .5
    center_x = (start[0] + goal[0]) / 2
    center_z = (start[2] + goal[2]) / 2
    lower_yaw = math.degrees(math.atan2(
        -(center_x - lower_world_x), center_z - lower_world_z,
    ))
    upper_yaw = math.degrees(math.atan2(
        -(center_x - upper_world_x), center_z - upper_world_z,
    ))
    lower_y = float(goal_feet if kind == "direct_drop" else min(
        start_feet, goal_feet,
    ))
    geometry = {
        "kind": kind,
        "drop_blocks": drop_blocks,
        "support_blocks": support_blocks,
        "start_position": start,
        "goal_position": goal,
        "observer_pose": (
            lower_world_x, lower_y, lower_world_z, lower_yaw, 30.0,
        ),
        "upper_observer_pose": (
            upper_world_x, float(max(start_feet, goal_feet)),
            upper_world_z, upper_yaw, -25.0,
        ),
        "entry_progress_blocks": transition_u - start_u - .5,
        "max_ticks": max_ticks,
        "origin": (origin_x, origin_z),
    }
    if kind != "low_height_route":
        geometry["entry_position"] = world_point(
            transition_u - 1, float(start_feet),
        )
    return geometry


def continuous_height_fabric_matrix_plan() -> tuple[dict, ...]:
    """Expand the frozen M3 manifest before any real-game result is read."""
    manifest = json.loads(FULL_MATRIX_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != (
            "mc2p.continuous-height-full-matrix.v1"):
        raise ValueError("unsupported continuous-height matrix manifest")
    families = (
        *(str(item) for item in manifest["small_height_families"]),
        *(str(item) for item in manifest["height_action_families"]),
    )
    directions = tuple(str(item) for item in manifest["directions"])
    speed_bands = tuple(manifest["entry_speed_bands"])
    repetitions = int(manifest["fabric_repetitions_per_direction"])
    seed_start = int(manifest["seed_start"])
    seed_count = int(manifest["seed_end"]) - seed_start + 1
    rows: list[dict] = []
    for family_index, family in enumerate(families):
        for condition_index, condition in enumerate(("normal", "late")):
            for direction_index, direction in enumerate(directions):
                for repetition in range(repetitions):
                    # Five repetitions per direction and two conditions make
                    # forty real trials per family.  Rotate across that whole
                    # sequence so every full-block family retains at least ten
                    # low, medium and high entry samples.
                    speed = speed_bands[
                        (condition_index * len(directions) * repetitions
                         + direction_index * repetitions + repetition)
                        % len(speed_bands)
                    ]
                    seed_offset = (
                        family_index * 40 + condition_index * 20
                        + direction_index * repetitions + repetition
                    ) % seed_count
                    row = {
                        "trial_id": (
                            f"m3-{family}-{condition}-{direction}-"
                            f"{repetition + 1}"
                        ),
                        "family": family,
                        "condition": condition,
                        "direction_index": direction_index,
                        "direction": direction,
                        "repetition": repetition + 1,
                        "speed_band": str(speed["id"]),
                        "entry_speed_minimum": float(speed["minimum"]),
                        "entry_speed_maximum": float(speed["maximum"]),
                        "target_entry_speed": float(speed["target"]),
                        "seed": seed_start + seed_offset,
                        "late_probability": (
                            float(manifest["late_probability"])
                            if condition == "late" else 0.0
                        ),
                        "injection": (
                            "random_late_verified_input"
                            if condition == "late" else None
                        ),
                    }
                    row.update(_matrix_trial_geometry(
                        family, direction_index, str(speed["id"]),
                        family_index,
                    ))
                    row["expected_terminal"] = (
                        ("success", "failed", "cancelled")
                        if condition == "late" else "success"
                    )
                    rows.append(row)
    return tuple(rows)


def select_continuous_height_fabric_matrix_shard(
    plan: tuple[dict, ...], shard_index: int, shard_count: int,
) -> tuple[dict, ...]:
    if shard_count <= 0 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid continuous-height matrix shard")
    return tuple(
        trial for index, trial in enumerate(plan)
        if index % shard_count == shard_index
    )


def continuous_height_execution_plan(
    *, review20_only: bool, full_matrix: bool,
    shard_index: int, shard_count: int,
) -> tuple[dict, ...]:
    if review20_only and full_matrix:
        raise ValueError("review-20 and the full matrix are separate runs")
    if review20_only:
        return navigation_coordination_review20_plan()
    if full_matrix:
        return select_continuous_height_fabric_matrix_shard(
            continuous_height_fabric_matrix_plan(), shard_index, shard_count,
        )
    if shard_index != 0 or shard_count != 1:
        raise ValueError("matrix shards require the full matrix")
    return continuous_height_trial_plan() + navigation_coordination_hardening_plan()


def navigation_coordination_hardening_plan() -> tuple[dict, ...]:
    """Freeze the three real-game gates reopened by coordination review 19."""
    removal_trials = tuple({
        "trial_id": f"landing-support-removed-lead-{lead}",
        "kind": "direct_drop",
        "direction_index": 1,
        "drop_blocks": 2,
        "origin": (32 + (4 - lead) * 16, 128),
        "injection": "remove_landing_support_at_lead",
        "removal_lead_ticks": lead,
        # The first real-Fabric boundary run showed that a removal four
        # client frames before departure is still stoppable.  At three frames
        # the changed block only reaches the controller after the body has
        # left the edge.  Keep the later rows as bounded no-return samples;
        # they must land on the fixture catch floor and must never report
        # task success.
        "expected_terminal": (
            "failed" if lead == 4 else ("failed", "cancelled")
        ),
        "expected_reason": (
            "landing_support_missing" if lead == 4 else None
        ),
        "expected_safe_stop": lead == 4,
    } for lead in (4, 3, 2, 1))
    return ({
            "trial_id": "runup-step-down",
            "kind": "stair_descent",
            "direction_index": 0,
            "fixture": "runup_step_down",
            "runup_blocks": 4,
            "origin": (0, 128),
            "length": 7,
            "injection": None,
            "expected_terminal": "success",
        },) + removal_trials + (
        {
            "trial_id": "fixed-one-tick-late-drop",
            "kind": "direct_drop",
            "direction_index": 2,
            "drop_blocks": 2,
            "origin": (96, 128),
            "injection": "late_first_verified_input",
            "expected_terminal": "success",
        },
        {
            "trial_id": "constant-one-tick-late-drop",
            "kind": "direct_drop",
            "direction_index": 3,
            "drop_blocks": 1,
            "origin": (112, 128),
            "injection": "late_every_verified_input",
            "expected_terminal": ("success", "failed", "cancelled"),
        },
    )


def navigation_coordination_review20_plan() -> tuple[dict, ...]:
    """Freeze the bounded real-game interruption sample for review 20."""
    phases = (
        "approach", "edge", "submitted_unapplied",
        "leave_edge", "airborne", "landed",
    )
    rows = []
    index = 0
    for drop_blocks in (2, 5):
        for phase_index, phase in enumerate(phases):
            for interruption in ("revise_goal", "cancel"):
                rows.append({
                    "trial_id": (
                        f"review20-drop-{drop_blocks}-{phase}-"
                        f"{interruption.replace('_', '-')}"
                    ),
                    "kind": "direct_drop",
                    "direction_index": index % 4,
                    "drop_blocks": drop_blocks,
                    "landing_exit_blocks": 2,
                    "observer_lateral_blocks": -5,
                    # Keep the first fixture in the already-loaded spawn area.
                    # Later fixtures advance by one nearby strip, so the server
                    # has loaded each target chunk before setblock runs.
                    "origin": (index * 12, 0),
                    "injection": "review20_interrupt",
                    "interruption": interruption,
                    "interrupt_phase": phase,
                    # Each drop/phase pair covers one ordinary interruption and
                    # one interruption followed by a real client-tick delay.
                    "late_after_interrupt": interruption == "cancel",
                    "expected_terminal": (
                        "success" if interruption == "revise_goal"
                        else "cancelled"
                    ),
                })
                index += 1
    return tuple(rows)


def _transform(direction_index: int, u: int, v: int = 0) -> tuple[int, int]:
    dx, dz, _ = _DIRECTIONS[direction_index]
    right_x, right_z = -dz, dx
    return dx * u + right_x * v, dz * u + right_z * v


def _origin(trial: dict) -> tuple[int, int]:
    if "origin" in trial:
        return tuple(trial["origin"])
    kind_row = {
        "low_height_stairs": 0,
        "stair_descent": 1,
        "direct_drop": 2,
    }[trial["kind"]]
    return trial["direction_index"] * 32, kind_row * 32


def _supports(trial: dict) -> tuple[tuple[tuple[int, int, int], str], ...]:
    if "support_blocks" in trial:
        return tuple(trial["support_blocks"])
    direction = trial["direction_index"]
    if trial.get("fixture") == "runup_step_down":
        runup = trial["runup_blocks"]
        local = tuple(
            ((u, FEET_Y - 1, 0), "minecraft:stone")
            for u in range(runup)
        ) + tuple(
            ((u, FEET_Y - 2, 0), "minecraft:stone")
            for u in range(runup, trial["length"] + 1)
        )
    elif trial["kind"] == "low_height_stairs":
        local = (
            ((0, FEET_Y - 1, 0), "minecraft:stone"),
            ((1, FEET_Y, 0), "minecraft:smooth_stone_slab[type=bottom]"),
            ((2, FEET_Y, 0), "minecraft:stone"),
            ((3, FEET_Y + 1, 0), "minecraft:smooth_stone_slab[type=bottom]"),
            ((4, FEET_Y + 1, 0), "minecraft:stone"),
        )
    elif trial["kind"] == "stair_descent":
        local = tuple(
            ((u, FEET_Y - 1 - u, 0), "minecraft:stone")
            for u in range(5)
        )
    else:
        drop = trial["drop_blocks"]
        exit_blocks = trial.get("landing_exit_blocks", 0)
        local = (
            ((0, FEET_Y - 1, 0), "minecraft:stone"),
        ) + tuple(
            ((u, FEET_Y - 1 - drop, 0), "minecraft:stone")
            for u in range(1, 2 + exit_blocks)
        )
    origin_x, origin_z = _origin(trial)
    transformed = []
    for (u, y, v), material in local:
        offset_x, offset_z = _transform(direction, u, v)
        transformed.append(
            ((origin_x + offset_x, y, origin_z + offset_z), material)
        )
    return tuple(transformed)


def _start_and_goal(trial: dict) -> tuple[
        tuple[float, float, float], tuple[float, float, float]]:
    if "start_position" in trial:
        return tuple(trial["start_position"]), tuple(trial["goal_position"])
    supports = _supports(trial)
    (sx, sy, sz), _ = supports[0]
    (gx, gy, gz), goal_material = supports[-1]
    goal_top = gy + (.5 if "slab" in goal_material else 1.0)
    return (sx + .5, sy + 1.0, sz + .5), (gx + .5, goal_top, gz + .5)


def _air_positions(trial: dict) -> tuple[tuple[int, int, int], ...]:
    supports = _supports(trial)
    occupied = {position for position, _ in supports}
    xs = tuple(position[0] for position in occupied)
    ys = tuple(position[1] for position in occupied)
    zs = tuple(position[2] for position in occupied)
    # Include one column around the route and the cells beneath isolated
    # supports.  The surface graph scans that bounded neighborhood to prove
    # which support is the reachable top surface.  The side vantage exposes
    # these air cells legally before planning begins.
    return tuple(sorted(
        (x, y, z)
        for x in range(min(xs) - 1, max(xs) + 2)
        for y in range(min(ys) - 1, max(ys) + 4)
        for z in range(min(zs) - 1, max(zs) + 2)
        if (x, y, z) not in occupied
    ))


def _damage_budget(trial: dict) -> TaskDamageBudget:
    if (trial["kind"] == "direct_drop"
            and trial.get("drop_blocks") == 5):
        return TaskDamageBudget("allow_two_points", 2.0)
    return TaskDamageBudget()


def _observer(trial: dict) -> tuple[float, float, float, float, float]:
    if "observer_pose" in trial:
        return tuple(trial["observer_pose"])
    direction = trial["direction_index"]
    length = trial.get(
        "length", 4 if trial["kind"] != "direct_drop" else 1,
    )
    origin_x, origin_z = _origin(trial)
    observer_x, observer_z = _transform(
        direction, length // 2, trial.get("observer_lateral_blocks", -6),
    )
    center_x, center_z = _transform(direction, length // 2, 0)
    ox, oz = origin_x + observer_x, origin_z + observer_z
    cx, cz = origin_x + center_x, origin_z + center_z
    yaw = math.degrees(math.atan2(-(cx + .5 - (ox + .5)),
                                  cz + .5 - (oz + .5)))
    if trial["kind"] == "direct_drop":
        # Observe from landing height.  With the 120-degree vertical field,
        # this exposes both the cells below the landing support and the upper
        # clearance above the start without relying on one-pixel slivers.
        return (
            ox + .5, float(FEET_Y - trial["drop_blocks"]), oz + .5,
            yaw, 0.0,
        )
    return ox + .5, float(FEET_Y), oz + .5, yaw, 40.0


def _upper_observer(
    trial: dict,
) -> tuple[float, float, float, float, float]:
    if "upper_observer_pose" in trial:
        return tuple(trial["upper_observer_pose"])
    direction = trial["direction_index"]
    length = trial.get(
        "length", 4 if trial["kind"] != "direct_drop" else 1,
    )
    origin_x, origin_z = _origin(trial)
    observer_x, observer_z = _transform(
        direction, length // 2,
        abs(trial.get("observer_lateral_blocks", -6)),
    )
    center_x, center_z = _transform(direction, length // 2, 0)
    ox, oz = origin_x + observer_x, origin_z + observer_z
    cx, cz = origin_x + center_x, origin_z + center_z
    yaw = math.degrees(math.atan2(-(cx + .5 - (ox + .5)),
                                  cz + .5 - (oz + .5)))
    return ox + .5, float(FEET_Y), oz + .5, yaw, -20.0


def _observer_supports(trial: dict) -> tuple[tuple[int, int, int], ...]:
    poses = (_observer(trial), _upper_observer(trial))
    return tuple(dict.fromkeys(
        (math.floor(x), math.floor(y) - 1, math.floor(z))
        for x, y, z, _, _ in poses
    ))


def _safety_catch_supports(
    trial: dict,
) -> tuple[tuple[int, int, int], ...]:
    """Return a physical catch floor for no-return support-removal probes.

    The catch floor is deliberately outside the route fixture and does not
    become a candidate goal.  It only keeps a late, physically unavoidable
    fall bounded so the formal controller can report its terminal result
    instead of losing the test client in the void.
    """
    if trial.get("injection") != "remove_landing_support_at_lead":
        return ()
    (landing_x, landing_y, landing_z), _ = _supports(trial)[-1]
    catch_y = landing_y - 4
    return tuple(
        (landing_x + dx, catch_y, landing_z + dz)
        for dx in range(-1, 2)
        for dz in range(-1, 2)
    )


def _observer_teleport_command(trial: dict, *, upper: bool) -> str:
    x, y, z, yaw, pitch = (
        _upper_observer(trial) if upper else _observer(trial)
    )
    return (
        f"tp MC2PProbe {x:.6f} {y:.6f} {z:.6f} "
        f"{yaw:.6f} {pitch:.6f}"
    )


def _fixture_observation_reposition(trial: dict, tick: int) -> str:
    """Alternate legal side views so both route and head clearance are seen."""
    return _observer_teleport_command(trial, upper=tick % 40 == 20)


def _fixture_commands(trial: dict) -> tuple[str, ...]:
    supports = _supports(trial)
    occupied = tuple(position for position, _ in supports)
    xs = tuple(position[0] for position in occupied)
    ys = tuple(position[1] for position in occupied)
    zs = tuple(position[2] for position in occupied)
    ox, oy, oz, yaw, pitch = _observer(trial)
    observer_supports = _observer_supports(trial)
    catch_supports = _safety_catch_supports(trial)
    clear_min_x = min(min(xs) - 2, *(p[0] - 2 for p in observer_supports))
    clear_max_x = max(max(xs) + 2, *(p[0] + 2 for p in observer_supports))
    clear_min_z = min(min(zs) - 2, *(p[2] - 2 for p in observer_supports))
    clear_max_z = max(max(zs) + 2, *(p[2] + 2 for p in observer_supports))
    clear_max_y = max(y for _, y, _ in _air_positions(trial))
    commands = []
    if "family" in trial:
        commands.append(
            f"forceload add {clear_min_x} {clear_min_z} "
            f"{clear_max_x} {clear_max_z}"
        )
    commands.extend([
        "difficulty peaceful",
        "time set midnight",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "weather clear",
        "kill @e[type=!minecraft:player]",
        "gamemode survival MC2PProbe",
        "effect clear MC2PProbe",
        "effect give MC2PProbe minecraft:instant_health 1 10 true",
        "gamerule naturalRegeneration false",
        (f"fill {clear_min_x} "
         f"{min((min(ys) - 2, *(p[1] - 1 for p in catch_supports)))} "
         f"{clear_min_z} "
         f"{clear_max_x} {clear_max_y} {clear_max_z} minecraft:air replace"),
    ])
    commands.extend(
        f"setblock {x} {y} {z} {material} replace"
        for (x, y, z), material in supports
    )
    commands.extend(
        f"setblock {x} {y} {z} minecraft:stone replace"
        for x, y, z in observer_supports
    )
    commands.extend(
        f"setblock {x} {y} {z} minecraft:stone replace"
        for x, y, z in catch_supports
    )
    commands.append(
        f"tp MC2PProbe {ox:.6f} {oy:.6f} {oz:.6f} {yaw:.6f} {pitch:.6f}"
    )
    return tuple(commands)


def _start_commands(trial: dict) -> tuple[str, ...]:
    start, _ = _start_and_goal(trial)
    yaw = _DIRECTIONS[trial["direction_index"]][2]
    commands = [
        f"setblock {x} {y} {z} minecraft:air replace"
        for x, y, z in _observer_supports(trial)
    ]
    commands.append(
        f"tp MC2PProbe {start[0]:.6f} {start[1]:.6f} {start[2]:.6f} "
        f"{yaw:.6f} 0.0"
    )
    return tuple(commands)


def _task(trial_id: str, deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0(
        "continuous-height-" + trial_id,
        "continuous_height_navigation",
        "{}",
        (SuccessCriterionV0(
            "goal_reached", ComparisonOperatorV0.EQUAL, 1, "boolean",
        ),),
        100, deadline_ns, True, 0.0,
    )


def _goal(trial: dict) -> GoalState:
    _, (x, y, z) = _start_and_goal(trial)
    return _goal_at(trial, (x, y, z))


def _revised_goal_position(trial: dict) -> tuple[float, float, float]:
    """Use a real replacement target on the first landing support."""
    (x, y, z), material = _supports(trial)[1]
    top = y + (.5 if "slab" in material else 1.0)
    return x + .5, top, z + .5


def _revised_goal(trial: dict) -> GoalState:
    return _goal_at(trial, _revised_goal_position(trial))


def _goal_at(
    trial: dict, position: tuple[float, float, float],
) -> GoalState:
    x, y, z = position
    budget = _damage_budget(trial)
    return GoalState(
        Aabb(x - .20, y - .08, z - .20, x + .20, y + .08, z + .20),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
        risk_policy_id=budget.risk_policy_id,
    )


def _chunks(values: tuple[tuple[int, int, int], ...]):
    for index in range(0, len(values), MAX_AIR_QUERY_POSITIONS):
        yield values[index:index + MAX_AIR_QUERY_POSITIONS]


def _self_health(runtime) -> float:
    own = runtime.observation.self_state.value
    if own is None:
        raise RuntimeError("continuous-height probe has no player state")
    return float(own.health_points + own.absorption_points)


def _diagnostic_row(
    runtime,
    backend,
    episode: str,
    trial_id: str,
    *,
    pipeline_diagnostic: Callable[[], dict] | None = None,
) -> dict:
    frame = runtime.navigation_observation_adapter.latest_frame
    row = {
        "episode_id": episode,
        "trial_id": trial_id,
        "observation_sequence_id": runtime.observation.sequence_id,
        "diagnostics": backend.last_diagnostics,
        "position": None if frame is None else list(frame.body.position),
    }
    if pipeline_diagnostic is not None:
        row["observation_pipeline"] = pipeline_diagnostic()
    return row


def _entry_preparation_decision(frame, trial: dict) -> MovementV1 | None:
    """Choose one legal key snapshot that approaches the frozen entry state.

    ``None`` means the body is already on the entry support, moving in the
    requested speed band.  This is the same bounded one-tick look-ahead used
    by the established B10 moving-entry Fabric probe; it neither writes player
    velocity nor bypasses Runtime arbitration.
    """
    entry = tuple(trial["entry_position"])
    dx, dz, _ = _DIRECTIONS[trial["direction_index"]]
    along = (
        (frame.body.position[0] - entry[0]) * dx
        + (frame.body.position[2] - entry[2]) * dz
    )
    forward_speed = (
        frame.body.velocity_blocks_per_second[0] * dx
        + frame.body.velocity_blocks_per_second[2] * dz
    )
    minimum = float(trial["entry_speed_minimum"])
    maximum = float(trial["entry_speed_maximum"])
    if (-.18 <= along <= .05 and minimum - 1.0e-6 <= forward_speed
            <= maximum + 1.0e-6 and frame.body.is_on_ground):
        return None
    if along > .07:
        raise RuntimeError(
            f"{trial['trial_id']} passed its entry before reaching "
            f"{trial['speed_band']} speed: along={along}, "
            f"speed={forward_speed}"
        )
    built = build_physics_state(
        frame, JAVA_1_21_RULESET, _STATE_ASSUMPTIONS,
    )
    if built.status is not StateBuildStatus.READY or built.state is None:
        raise RuntimeError(
            f"{trial['trial_id']} entry preparation state is incomplete: "
            f"{built}"
        )
    physics_world = PhysicsWorldView(frame.world, JAVA_1_21_RULESET)
    target_speed = float(trial["target_entry_speed"])
    target_along = -.05
    ranked: list[tuple[float, int, MovementV1]] = []
    controls = (
        MovementV1(),
        MovementV1(forward=1),
        MovementV1(forward=1, sprint=True),
        MovementV1(forward=-1),
    )
    position_weight = 5.0 if along > -.5 else 1.5
    for order, movement in enumerate(controls):
        projected = project_movement_command(
            built.state, movement,
            movement_yaw_radians=built.state.yaw_radians,
        )
        if (projected.status is not ProjectionStatus.READY
                or projected.tick_input is None):
            continue
        calculated = physics_step(
            built.state, projected.tick_input,
            physics_world, JAVA_1_21_RULESET,
        )
        if (calculated.status is not CalculationStatus.OK
                or calculated.next_state is None):
            continue
        next_state = calculated.next_state
        next_along = (
            (next_state.position[0] - entry[0]) * dx
            + (next_state.position[2] - entry[2]) * dz
        )
        next_speed = 20.0 * (
            next_state.velocity_blocks_per_tick[0] * dx
            + next_state.velocity_blocks_per_tick[2] * dz
        )
        score = (
            abs(next_along - target_along) * position_weight
            + abs(next_speed - target_speed) * .8
        )
        if next_along > .06:
            score += 100.0 + (next_along - .06) * 100.0
        if next_speed < -.01:
            score += 20.0
        ranked.append((score, order, movement))
    if not ranked:
        raise RuntimeError(
            f"{trial['trial_id']} has no calculable entry-preparation input"
        )
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][2]


def _prepare_full_block_entry_speed(
    runtime,
    task: TaskIntentV0,
    profile: BehaviorProfileV0,
    deadline_ns: int,
    trial: dict,
    air_chunks: tuple[tuple[tuple[int, int, int], ...], ...],
    record_diagnostic: Callable[[], None],
) -> dict:
    """Reach one frozen full-block entry state through formal player input."""
    source = runtime.register_ordered_source("continuous-height-entry")
    sequence = 0
    try:
        for tick in range(80):
            frame = runtime.navigation_observation_adapter.latest_frame
            if frame is None:
                raise RuntimeError("entry preparation has no navigation frame")
            movement = _entry_preparation_decision(frame, trial)
            if movement is None:
                speed = math.hypot(
                    frame.body.velocity_blocks_per_second[0],
                    frame.body.velocity_blocks_per_second[2],
                )
                return {
                    "ticks": tick,
                    "position": list(frame.body.position),
                    "speed_blocks_per_second": speed,
                    "movement_tick_id": frame.body.movement_tick_id,
                }
            runtime.cancel_source(source.source_id)
            sequence += 1
            now = time.perf_counter_ns()
            intent = ActionIntentV1(
                ordered_intent_id(source, sequence),
                source.source_id,
                runtime.observation.episode_id,
                runtime.observation.sequence_id,
                ActionPriorityV0.TASK,
                now,
                min(deadline_ns, now + 750_000_000),
                movement=movement,
                valid_for_ticks=1,
            )
            runtime.submit_ordered_intent(
                OrderedIntentV1(source, sequence, intent)
            )
            result = runtime.step(
                task, profile,
                min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                observation_request=ObservationRequestV3(
                    "navigation_v1", air_chunks[tick % len(air_chunks)],
                ),
            )
            record_diagnostic()
            if result.report.failure is not None:
                raise RuntimeError(
                    f"{trial['trial_id']} entry preparation failed: "
                    f"{result.report.failure.message}"
                )
            if (result.decision is None
                    or result.decision.action.movement != movement):
                raise RuntimeError(
                    f"{trial['trial_id']} entry input lost arbitration"
                )
        raise RuntimeError(
            f"{trial['trial_id']} did not reach its entry speed within 80 ticks"
        )
    finally:
        if runtime.has_ordered_source(source):
            runtime.cancel_source(source.source_id)
            runtime.unregister_ordered_source(source)


def _review20_phase_matches(
    phase: str,
    session: NavigationSession,
    frame,
    start: tuple[float, float, float],
    goal: tuple[float, float, float],
) -> bool:
    diagnostics = session.diagnostics
    if phase == "approach":
        return (
            diagnostics.action_kind == "WalkSegment"
            and frame.body.is_on_ground
        )
    if phase == "edge":
        return "landing_edge_probe" in diagnostics.controller_ids
    if phase == "submitted_unapplied":
        return session.report.reason == "awaiting_verified_motion"
    if phase == "leave_edge":
        return (
            diagnostics.action_kind == "ControlledDropSegment"
            and frame.body.is_on_ground
            and math.dist(frame.body.position, start) > .12
        )
    if phase == "airborne":
        return (
            diagnostics.action_kind == "ControlledDropSegment"
            and not frame.body.is_on_ground
        )
    if phase == "landed":
        return (
            diagnostics.action_kind == "ControlledDropSegment"
            and frame.body.is_on_ground
            and abs(frame.body.position[1] - goal[1]) <= .10
        )
    raise ValueError(f"unknown review-20 interruption phase: {phase}")


def run_continuous_height_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
    pipeline_diagnostic: Callable[[], dict] | None = None,
    *,
    review20_only: bool = False,
    full_matrix: bool = False,
    shard_index: int = 0,
    shard_count: int = 1,
) -> tuple[dict, list[dict], list[dict]]:
    """Run the selected real-game matrix through the formal navigation driver."""
    profiles = NavigationSessionProfiles.load(CONFIG)
    profile = BehaviorProfileV0()
    rows: list[dict] = []
    diagnostics: list[dict] = []

    def record_diagnostic(trial_id: str) -> None:
        row = _diagnostic_row(
            runtime,
            backend,
            episode,
            trial_id,
            pipeline_diagnostic=pipeline_diagnostic,
        )
        diagnostics.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    record_diagnostic("continuous-height-reset")
    with PlannerWorker() as planner, MotionSolverWorker(max_pending=4) as motion:
        departure_baselines: dict[int, int] = {}
        representative_plan = (() if (review20_only or full_matrix)
                               else continuous_height_trial_plan())
        hardening_plan = (() if (review20_only or full_matrix)
                          else navigation_coordination_hardening_plan())
        review20_plan = (navigation_coordination_review20_plan()
                         if review20_only else ())
        matrix_plan = (
            select_continuous_height_fabric_matrix_shard(
                continuous_height_fabric_matrix_plan(),
                shard_index, shard_count,
            ) if full_matrix else ()
        )
        execution_plan = continuous_height_execution_plan(
            review20_only=review20_only,
            full_matrix=full_matrix,
            shard_index=shard_index,
            shard_count=shard_count,
        )
        for trial in execution_plan:
            trial_id = trial["trial_id"]
            fixture_writer(_fixture_commands(trial), trial)
            task = _task(trial_id, deadline_ns)
            air = _air_positions(trial)
            air_chunks = tuple(_chunks(air))
            start, goal_position = _start_and_goal(trial)
            supports = tuple(position for position, _ in _supports(trial))

            # First observe the fixture from a legal side vantage.  The test
            # never writes WorldKnowledge directly.
            ready = False
            for tick in range(80):
                if tick in {20, 40, 60}:
                    fixture_writer((
                        _fixture_observation_reposition(trial, tick),
                    ), trial)
                request = ObservationRequestV3(
                    "navigation_v1", air_chunks[tick % len(air_chunks)],
                )
                result = runtime.step(
                    task, profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                    observation_request=request,
                )
                record_diagnostic(trial_id)
                if result.report.failure is not None:
                    raise RuntimeError(
                        f"{trial_id} fixture observation failed: "
                        f"{result.report.failure.message}"
                    )
                frame = runtime.navigation_observation_adapter.latest_frame
                known_supports = (
                    frame is not None and all(
                        frame.world.cell(position).knowledge is CellKnowledge.BLOCK
                        for position in supports
                    )
                )
                known_air = 0 if frame is None else sum(
                    frame.world.cell(position).knowledge is CellKnowledge.AIR
                    for position in air
                )
                if known_supports and known_air == len(air):
                    ready = True
                    break
            if not ready:
                assert frame is not None
                unresolved_supports = tuple(
                    (position, frame.world.cell(position).knowledge.value)
                    for position in supports
                    if frame.world.cell(position).knowledge is not CellKnowledge.BLOCK
                )
                unresolved_air = tuple(
                    (position, frame.world.cell(position).knowledge.value)
                    for position in air
                    if frame.world.cell(position).knowledge is not CellKnowledge.AIR
                )
                raise RuntimeError(
                    f"{trial_id} fixture did not become observable; "
                    f"supports={unresolved_supports[:12]!r}; "
                    f"air={unresolved_air[:12]!r}"
                )

            fixture_writer(_start_commands(trial), trial)
            settled = False
            for tick in range(24):
                request = ObservationRequestV3(
                    "navigation_v1", air_chunks[tick % len(air_chunks)],
                )
                result = runtime.step(
                    task, profile,
                    min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                    observation_request=request,
                )
                record_diagnostic(trial_id)
                frame = runtime.navigation_observation_adapter.latest_frame
                if (frame is not None
                        and math.dist(frame.body.position, start) <= .04
                        and frame.body.is_on_ground
                        and math.hypot(
                            frame.body.velocity_blocks_per_second[0],
                            frame.body.velocity_blocks_per_second[2],
                        ) <= .04):
                    settled = True
                    break
            if not settled:
                raise RuntimeError(f"{trial_id} start teleport did not settle")

            entry_preparation = None
            if full_matrix and trial["kind"] != "low_height_route":
                entry_preparation = _prepare_full_block_entry_speed(
                    runtime,
                    task,
                    profile,
                    deadline_ns,
                    trial,
                    air_chunks,
                    lambda: record_diagnostic(trial_id),
                )

            initial_health = _self_health(runtime)
            session = NavigationSession(
                trial_id + "/session", profiles,
                planner_worker=planner, owns_planner_worker=False,
                motion_worker=motion,
                observation_adapter=runtime.navigation_observation_adapter,
            )
            driver = RuntimeNavigationDriver(
                runtime, session,
                observation_request=ObservationRequestV3("navigation_v1"),
            )
            budget = _damage_budget(trial)
            samples: list[dict] = []
            admitted_actions: tuple[str, ...] = ()
            expected_goal_position = goal_position
            injection = trial.get("injection")
            injection_applied = injection is None
            injection_attempts = 0
            input_delay_ticks: int | None = None
            input_delay_tick_samples: list[int] = []
            delay_rng = random.Random(trial.get("seed", 0))
            first_airborne_tick: int | None = None
            measured_entry_speed: float | None = (
                None if entry_preparation is None
                else float(entry_preparation["speed_blocks_per_second"])
            )
            interruption_phase_observed: str | None = None
            removal_target_tick: int | None = None
            if injection == "remove_landing_support_at_lead":
                baseline = departure_baselines.get(trial["drop_blocks"])
                if baseline is None:
                    raise RuntimeError(
                        f"{trial_id} has no same-drop departure baseline"
                    )
                removal_target_tick = baseline - trial["removal_lead_ticks"]
            started_ns = time.perf_counter_ns()
            driver.start(
                trial_id + "/goal", 1, _goal(trial), started_ns,
                damage_budget=budget,
            )
            try:
                for tick in range(int(trial.get("max_ticks", 240))):
                    before_frame = runtime.navigation_observation_adapter.latest_frame
                    before_tick = (
                        None if before_frame is None
                        else before_frame.body.movement_tick_id
                    )
                    awaiting_motion = (
                        session.report.reason == "awaiting_verified_motion"
                    )
                    interrupt_this_frame = (
                        injection == "review20_interrupt"
                        and not injection_applied
                        and _review20_phase_matches(
                            trial["interrupt_phase"], session,
                            before_frame, start, goal_position,
                        )
                    )
                    if interrupt_this_frame:
                        interruption_phase_observed = trial["interrupt_phase"]
                        injection_attempts += 1
                        injection_applied = True
                        if trial["interruption"] == "revise_goal":
                            expected_goal_position = _revised_goal_position(
                                trial,
                            )
                            driver.replace_goal(
                                trial_id + "/goal", 2, _revised_goal(trial),
                                time.perf_counter_ns(), damage_budget=budget,
                            )
                    if (injection == "remove_landing_support_at_lead"
                            and not injection_applied
                            and tick == removal_target_tick):
                        landing_position, _ = _supports(trial)[-1]
                        fixture_writer((
                            (f"setblock {landing_position[0]} "
                             f"{landing_position[1]} {landing_position[2]} "
                             "minecraft:air replace"),
                        ), trial)
                        injection_applied = True
                        injection_attempts += 1
                    delay_this_frame = (
                        (
                            injection == "late_first_verified_input"
                            and input_delay_ticks is None
                            and awaiting_motion
                        )
                        or (
                            injection == "late_every_verified_input"
                            and (
                                awaiting_motion
                                or session.diagnostics.action_kind
                                    == "ControlledDropSegment"
                            )
                        )
                        or (
                            interrupt_this_frame
                            and trial["late_after_interrupt"]
                        )
                        or (
                            injection == "random_late_verified_input"
                            and session._active_route is not None
                            and delay_rng.random()
                                < float(trial["late_probability"])
                        )
                    )
                    if delay_this_frame:
                        # The independent client continues its real movement
                        # ticks while the controller is late.  A 55 ms pause is
                        # deliberately just over one 20 Hz game tick; the
                        # receipt below proves the actual tick displacement.
                        time.sleep(.055)
                        injection_attempts += 1
                    if (interrupt_this_frame
                            and trial["interruption"] == "cancel"):
                        result = driver.stop(
                            profile, "review20_interrupt_cancel",
                        )
                    else:
                        result = driver.tick(
                            profile,
                            min(deadline_ns,
                                time.perf_counter_ns() + 500_000_000),
                        )
                    record_diagnostic(trial_id)
                    frame = runtime.navigation_observation_adapter.latest_frame
                    route = session._active_route
                    if route is not None:
                        admitted_actions = tuple(
                            type(action).__name__
                            for action in route.action_route.actions
                        )
                    movement = (
                        None if result.decision is None
                        else asdict(result.decision.action.movement)
                    )
                    if (delay_this_frame and result.decision is not None
                            and result.decision.action.movement != MovementV1()
                            and result.backend_result is not None
                            and before_tick is not None):
                        owned = tuple(
                            item for item in
                            result.backend_result.receipt.input_applications
                            if item.request_sequence_id
                            == result.decision.action.request_sequence_id
                        )
                        if owned:
                            observed_delay = (
                                min(item.movement_tick_id for item in owned)
                                - (before_tick + 1)
                            )
                            input_delay_ticks = observed_delay
                            input_delay_tick_samples.append(observed_delay)
                            if injection == "late_first_verified_input":
                                injection_applied = observed_delay == 1
                            elif injection == "random_late_verified_input":
                                injection_applied = True
                    if (first_airborne_tick is None
                            and not frame.body.is_on_ground):
                        first_airborne_tick = tick
                    if (measured_entry_speed is None
                            and "entry_progress_blocks" in trial):
                        dx, dz, _ = _DIRECTIONS[trial["direction_index"]]
                        progress = (
                            (frame.body.position[0] - start[0]) * dx
                            + (frame.body.position[2] - start[2]) * dz
                        )
                        if progress >= float(trial["entry_progress_blocks"]):
                            measured_entry_speed = math.hypot(
                                frame.body.velocity_blocks_per_second[0],
                                frame.body.velocity_blocks_per_second[2],
                            )
                    sample = {
                        "trial_id": trial_id,
                        "tick": tick,
                        "session_state": session.report.state.value,
                        "reason": session.report.reason,
                        "position": list(frame.body.position),
                        "velocity_blocks_per_second": list(
                            frame.body.velocity_blocks_per_second
                        ),
                        "is_on_ground": frame.body.is_on_ground,
                        "movement": movement,
                        "admitted_actions": list(admitted_actions),
                        "injection": injection,
                        "injection_attempts": injection_attempts,
                        "input_delay_ticks": input_delay_ticks,
                        "interrupt_phase": trial.get("interrupt_phase"),
                        "interruption_phase_observed": (
                            interruption_phase_observed
                        ),
                    }
                    samples.append(sample)
                    append_jsonl(
                        directory / "continuous-height-samples.jsonl", sample,
                    )
                    if driver.state in {"success", "failed", "cancelled"}:
                        break
                final_health = _self_health(runtime)
                actual_damage = max(0.0, initial_health - final_health)
                final_position = tuple(frame.body.position)
                expected_terminal = trial.get("expected_terminal", "success")
                terminal_matches = (
                    driver.state in expected_terminal
                    if type(expected_terminal) is tuple
                    else driver.state == expected_terminal
                )
                violations: list[str] = []
                if not terminal_matches:
                    violations.append(
                        f"unexpected_terminal:{driver.state}:{driver.reason}:"
                        f"expected={expected_terminal}"
                    )
                expected_reason = trial.get("expected_reason")
                if expected_reason is not None and driver.reason != expected_reason:
                    violations.append(
                        f"unexpected_reason:{driver.reason}:"
                        f"expected={expected_reason}"
                    )
                if driver.state == "success":
                    if math.dist(final_position, expected_goal_position) > .35:
                        violations.append(
                            f"success_outside_goal:{final_position}"
                        )
                elif not frame.body.is_on_ground:
                    raise RuntimeError(
                        f"{trial_id} did not end on stable ground: "
                        f"{final_position}"
                    )
                elif (driver.state == "failed"
                      and injection == "remove_landing_support_at_lead"
                      and actual_damage == 0.0
                      and abs(final_position[1] - start[1]) > .01):
                    violations.append(
                        "unsafe_failure_support:"
                        f"{final_position}"
                    )
                if (actual_damage > budget.maximum_expected_damage_points + 1e-6
                        and not (
                            injection == "remove_landing_support_at_lead"
                            and not trial.get("expected_safe_stop", False)
                            and driver.state in {"failed", "cancelled"}
                        )):
                    violations.append(
                        f"damage_budget_exceeded:{actual_damage}"
                    )
                if injection == "late_every_verified_input":
                    injection_applied = (
                        bool(input_delay_tick_samples)
                        and all(delay == 1 for delay in input_delay_tick_samples)
                    )
                if (injection is None and trial["kind"] == "direct_drop"
                        and first_airborne_tick is not None):
                    departure_baselines.setdefault(
                        trial["drop_blocks"], first_airborne_tick,
                    )
                row = {
                    **trial,
                    "episode_id": episode,
                    "passed": not violations,
                    "violations": violations,
                    "task_succeeded": driver.state == "success",
                    "ticks": len(samples),
                    "elapsed_ns": time.perf_counter_ns() - started_ns,
                    "actions": list(admitted_actions),
                    "initial_health_points": initial_health,
                    "final_health_points": final_health,
                    "actual_damage_points": actual_damage,
                    "damage_budget_exceeded": (
                        actual_damage
                        > budget.maximum_expected_damage_points + 1e-6
                    ),
                    "terminal_state": driver.state,
                    "terminal_reason": driver.reason,
                    "injection_applied": injection_applied,
                    "injection_attempts": injection_attempts,
                    "input_delay_ticks": input_delay_ticks,
                    "input_delay_tick_samples": input_delay_tick_samples,
                    "first_airborne_tick": first_airborne_tick,
                    "measured_entry_speed": measured_entry_speed,
                    "entry_preparation": entry_preparation,
                    "entry_speed_band_matched": (
                        None if (measured_entry_speed is None
                                 or trial["kind"] == "low_height_route")
                        else float(trial.get("entry_speed_minimum", 0.0))
                        <= measured_entry_speed
                        <= float(trial.get("entry_speed_maximum", math.inf))
                    ),
                    "removal_target_tick": removal_target_tick,
                    "interruption_phase_observed": (
                        interruption_phase_observed
                    ),
                    "damage_budget": asdict(budget),
                    "final_position": list(final_position),
                    "goal_position": list(expected_goal_position),
                }
                rows.append(row)
                append_jsonl(directory / "continuous-height-trials.jsonl", row)
            finally:
                if driver.source is not None:
                    if driver.state in {"success", "failed", "cancelled"}:
                        driver.release("continuous_height_trial_complete")
                    else:
                        driver.stop(profile, "continuous_height_trial_cleanup")
                session.close()

    summary = {
        "schema_version": "mc2p.continuous-height-fabric.v1",
        "episode_id": episode,
        "trial_count": len(rows),
        "passed_count": sum(bool(row["passed"]) for row in rows),
        "kinds": sorted({row["kind"] for row in rows}),
        "damage_trial_count": sum(
            row["damage_budget"]["maximum_expected_damage_points"] > 0
            for row in rows
        ),
        "representative_trial_count": len(representative_plan),
        "representative_passed_count": sum(
            bool(row["passed"])
            for row in rows if row["trial_id"] in {
                trial["trial_id"] for trial in representative_plan
            }
        ),
        "hardening_trial_count": len(hardening_plan),
        "hardening_passed_count": sum(
            bool(row["passed"])
            for row in rows if row["trial_id"] in {
                trial["trial_id"] for trial in hardening_plan
            }
        ),
        "review20_trial_count": len(review20_plan),
        "review20_passed_count": sum(
            bool(row["passed"])
            for row in rows if row["trial_id"] in {
                trial["trial_id"] for trial in review20_plan
            }
        ),
        "matrix_trial_count": len(matrix_plan),
        "matrix_result_count": sum(
            row["trial_id"] in {
                trial["trial_id"] for trial in matrix_plan
            } for row in rows
        ),
        "matrix_task_success_count": sum(
            bool(row["task_succeeded"])
            for row in rows if row["trial_id"] in {
                trial["trial_id"] for trial in matrix_plan
            }
        ),
        "matrix_shard_index": shard_index if full_matrix else None,
        "matrix_shard_count": shard_count if full_matrix else None,
    }
    write_json_atomic(directory / "continuous-height-summary.json", summary)
    hardening_rows = tuple(
        row for row in rows if row["trial_id"] in {
            trial["trial_id"] for trial in hardening_plan
        }
    )
    review20_rows = tuple(
        row for row in rows if row["trial_id"] in {
            trial["trial_id"] for trial in review20_plan
        }
    )
    checks = ([] if (review20_only or full_matrix) else [
        {
            "name": "continuous_height_representative_matrix",
            "passed": (
                summary["representative_passed_count"]
                == summary["representative_trial_count"] == 12
            ),
            "details": summary,
        },
        {
            "name": "navigation_coordination_hardening_matrix",
            "passed": (
                summary["hardening_passed_count"]
                == summary["hardening_trial_count"] == 7
                and all(row["injection_applied"] for row in hardening_rows)
                and next(
                    row for row in hardening_rows
                    if row["trial_id"] == "fixed-one-tick-late-drop"
                )["input_delay_ticks"] == 1
                and all(
                    (
                        row["terminal_state"] == "failed"
                        and row["terminal_reason"]
                            == "landing_support_missing"
                        and row["actual_damage_points"] == 0.0
                        and not row["damage_budget_exceeded"]
                    ) if row["expected_safe_stop"] else (
                        row["terminal_state"] in {"failed", "cancelled"}
                        and (
                            row["damage_budget_exceeded"]
                            or (
                                row["terminal_reason"]
                                    == "landing_support_missing"
                                and row["actual_damage_points"] == 0.0
                            )
                        )
                    )
                    for row in hardening_rows
                    if row.get("removal_lead_ticks") is not None
                )
                and bool((constant_late := next(
                    row for row in hardening_rows
                    if row["trial_id"] == "constant-one-tick-late-drop"
                ))["input_delay_tick_samples"])
                and all(
                    delay == 1
                    for delay in constant_late["input_delay_tick_samples"]
                )
            ),
            "details": {
                "trial_count": len(hardening_rows),
                "passed_count": sum(bool(row["passed"])
                                    for row in hardening_rows),
                "trials": hardening_rows,
            },
        },
    ])
    if review20_only:
        checks.append({
            "name": "navigation_coordination_review20_matrix",
            "passed": (
                summary["review20_passed_count"]
                == summary["review20_trial_count"] == 24
                and all(row["injection_applied"] for row in review20_rows)
                and all(
                    row["interruption_phase_observed"]
                    == row["interrupt_phase"]
                    for row in review20_rows
                )
            ),
            "details": {
                "trial_count": len(review20_rows),
                "passed_count": sum(bool(row["passed"])
                                    for row in review20_rows),
                "trials": review20_rows,
            },
        })
    if full_matrix:
        matrix_ids = {trial["trial_id"] for trial in matrix_plan}
        matrix_rows = tuple(row for row in rows
                            if row["trial_id"] in matrix_ids)
        checks.append({
            "name": "continuous_height_full_matrix_shard",
            "passed": (
                len(matrix_rows) == len(matrix_plan)
                and len({row["trial_id"] for row in matrix_rows})
                    == len(matrix_plan)
                and all(bool(row["passed"]) for row in matrix_rows)
                and all(
                    row["entry_speed_band_matched"] is True
                    for row in matrix_rows
                    if (row["task_succeeded"]
                        and row["kind"] != "low_height_route")
                )
            ),
            "details": {
                "shard_index": shard_index,
                "shard_count": shard_count,
                "trial_count": len(matrix_plan),
                "task_success_count": sum(
                    bool(row["task_succeeded"]) for row in matrix_rows
                ),
                "bounded_failure_count": sum(
                    not bool(row["task_succeeded"]) for row in matrix_rows
                ),
                "entry_speed_match_count": sum(
                    row["entry_speed_band_matched"] is True
                    for row in matrix_rows
                ),
            },
        })
    return summary, diagnostics, checks
