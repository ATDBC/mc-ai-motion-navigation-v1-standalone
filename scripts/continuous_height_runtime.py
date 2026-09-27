"""Representative real-Fabric acceptance for continuous-height movement."""
from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import time
from typing import Callable

from mc2p.contracts.behavior import BehaviorProfileV0
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
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.world_model import Aabb, CellKnowledge
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from scripts.control_probe_core import append_jsonl, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
FEET_Y = 100
_DIRECTIONS = (
    (1, 0, -90.0),
    (0, 1, 0.0),
    (-1, 0, 90.0),
    (0, -1, 180.0),
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


def _transform(direction_index: int, u: int, v: int = 0) -> tuple[int, int]:
    dx, dz, _ = _DIRECTIONS[direction_index]
    right_x, right_z = -dz, dx
    return dx * u + right_x * v, dz * u + right_z * v


def _origin(trial: dict) -> tuple[int, int]:
    kind_row = {
        "low_height_stairs": 0,
        "stair_descent": 1,
        "direct_drop": 2,
    }[trial["kind"]]
    return trial["direction_index"] * 32, kind_row * 32


def _supports(trial: dict) -> tuple[tuple[tuple[int, int, int], str], ...]:
    direction = trial["direction_index"]
    if trial["kind"] == "low_height_stairs":
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
        local = (
            ((0, FEET_Y - 1, 0), "minecraft:stone"),
            ((1, FEET_Y - 1 - drop, 0), "minecraft:stone"),
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
    if trial["kind"] == "direct_drop" and trial["drop_blocks"] == 5:
        return TaskDamageBudget("allow_two_points", 2.0)
    return TaskDamageBudget()


def _observer(trial: dict) -> tuple[float, float, float, float, float]:
    direction = trial["direction_index"]
    length = 4 if trial["kind"] != "direct_drop" else 1
    origin_x, origin_z = _origin(trial)
    observer_x, observer_z = _transform(direction, length // 2, -6)
    center_x, center_z = _transform(direction, length // 2, 0)
    ox, oz = origin_x + observer_x, origin_z + observer_z
    cx, cz = origin_x + center_x, origin_z + center_z
    yaw = math.degrees(math.atan2(-(cx + .5 - (ox + .5)),
                                  cz + .5 - (oz + .5)))
    return ox + .5, float(FEET_Y), oz + .5, yaw, 25.0


def _fixture_commands(trial: dict) -> tuple[str, ...]:
    supports = _supports(trial)
    occupied = tuple(position for position, _ in supports)
    xs = tuple(position[0] for position in occupied)
    ys = tuple(position[1] for position in occupied)
    zs = tuple(position[2] for position in occupied)
    ox, oy, oz, yaw, pitch = _observer(trial)
    observer_support = (math.floor(ox), FEET_Y - 1, math.floor(oz))
    clear_min_x = min(min(xs) - 2, observer_support[0] - 2)
    clear_max_x = max(max(xs) + 2, observer_support[0] + 2)
    clear_min_z = min(min(zs) - 2, observer_support[2] - 2)
    clear_max_z = max(max(zs) + 2, observer_support[2] + 2)
    clear_max_y = max(y for _, y, _ in _air_positions(trial))
    commands = [
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
        (f"fill {clear_min_x} {min(ys) - 2} {clear_min_z} "
         f"{clear_max_x} {clear_max_y} {clear_max_z} minecraft:air replace"),
    ]
    commands.extend(
        f"setblock {x} {y} {z} {material} replace"
        for (x, y, z), material in supports
    )
    commands.extend((
        (f"setblock {observer_support[0]} {observer_support[1]} "
         f"{observer_support[2]} minecraft:stone replace"),
        f"tp MC2PProbe {ox:.6f} {oy:.6f} {oz:.6f} {yaw:.6f} {pitch:.6f}",
    ))
    return tuple(commands)


def _start_commands(trial: dict) -> tuple[str, ...]:
    ox, _, oz, _, _ = _observer(trial)
    observer_support = (math.floor(ox), FEET_Y - 1, math.floor(oz))
    start, _ = _start_and_goal(trial)
    yaw = _DIRECTIONS[trial["direction_index"]][2]
    return (
        (f"setblock {observer_support[0]} {observer_support[1]} "
         f"{observer_support[2]} minecraft:air replace"),
        (f"tp MC2PProbe {start[0]:.6f} {start[1]:.6f} {start[2]:.6f} "
         f"{yaw:.6f} 0.0"),
    )


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


def run_continuous_height_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[str, ...], dict], None],
) -> tuple[dict, list[dict], list[dict]]:
    """Run a small real-game matrix through the formal navigation driver."""
    profiles = NavigationSessionProfiles.load(CONFIG)
    profile = BehaviorProfileV0()
    rows: list[dict] = []
    diagnostics: list[dict] = []

    def record_diagnostic(trial_id: str) -> None:
        frame = runtime.navigation_observation_adapter.latest_frame
        row = {
            "episode_id": episode,
            "trial_id": trial_id,
            "observation_sequence_id": runtime.observation.sequence_id,
            "diagnostics": backend.last_diagnostics,
            "position": None if frame is None else list(frame.body.position),
        }
        diagnostics.append(row)
        append_jsonl(directory / "diagnostics.jsonl", row)

    record_diagnostic("continuous-height-reset")
    with PlannerWorker() as planner, MotionSolverWorker(max_pending=4) as motion:
        for trial in continuous_height_trial_plan():
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
            started_ns = time.perf_counter_ns()
            driver.start(
                trial_id + "/goal", 1, _goal(trial), started_ns,
                damage_budget=budget,
            )
            try:
                for tick in range(240):
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
                    }
                    samples.append(sample)
                    append_jsonl(
                        directory / "continuous-height-samples.jsonl", sample,
                    )
                    if driver.state in {"success", "failed", "cancelled"}:
                        break
                if driver.state != "success":
                    raise RuntimeError(
                        f"{trial_id} ended as {driver.state}: {driver.reason}; "
                        f"session={session.report}"
                    )
                final_health = _self_health(runtime)
                actual_damage = max(0.0, initial_health - final_health)
                final_position = tuple(frame.body.position)
                if math.dist(final_position, goal_position) > .35:
                    raise RuntimeError(
                        f"{trial_id} completed outside goal: {final_position}"
                    )
                if actual_damage > budget.maximum_expected_damage_points + 1e-6:
                    raise RuntimeError(
                        f"{trial_id} exceeded damage budget: {actual_damage}"
                    )
                row = {
                    **trial,
                    "episode_id": episode,
                    "passed": True,
                    "ticks": len(samples),
                    "elapsed_ns": time.perf_counter_ns() - started_ns,
                    "actions": list(admitted_actions),
                    "initial_health_points": initial_health,
                    "final_health_points": final_health,
                    "actual_damage_points": actual_damage,
                    "damage_budget": asdict(budget),
                    "final_position": list(final_position),
                    "goal_position": list(goal_position),
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
    }
    write_json_atomic(directory / "continuous-height-summary.json", summary)
    checks = [{
        "name": "continuous_height_representative_matrix",
        "passed": summary["passed_count"] == summary["trial_count"] == 12,
        "details": summary,
    }]
    return summary, diagnostics, checks
