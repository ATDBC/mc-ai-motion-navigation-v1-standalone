"""Representative real-Fabric acceptance for continuous-height movement."""
from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import time
from typing import Callable

from mc2p.contracts.action_v1 import MovementV1
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
        (f"fill {clear_min_x} "
         f"{min((min(ys) - 2, *(p[1] - 1 for p in catch_supports)))} "
         f"{clear_min_z} "
         f"{clear_max_x} {clear_max_y} {clear_max_z} minecraft:air replace"),
    ]
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
) -> tuple[dict, list[dict], list[dict]]:
    """Run a small real-game matrix through the formal navigation driver."""
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
        representative_plan = (() if review20_only
                               else continuous_height_trial_plan())
        hardening_plan = (() if review20_only
                          else navigation_coordination_hardening_plan())
        review20_plan = (navigation_coordination_review20_plan()
                         if review20_only else ())
        for trial in representative_plan + hardening_plan + review20_plan:
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
            first_airborne_tick: int | None = None
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
                for tick in range(240):
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
                    if (first_airborne_tick is None
                            and not frame.body.is_on_ground):
                        first_airborne_tick = tick
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
                if not terminal_matches:
                    raise RuntimeError(
                        f"{trial_id} ended as {driver.state}: {driver.reason}; "
                        f"expected={expected_terminal}; session={session.report}"
                    )
                expected_reason = trial.get("expected_reason")
                if expected_reason is not None and driver.reason != expected_reason:
                    raise RuntimeError(
                        f"{trial_id} ended for {driver.reason}; "
                        f"expected={expected_reason}"
                    )
                if driver.state == "success":
                    if math.dist(final_position, expected_goal_position) > .35:
                        raise RuntimeError(
                            f"{trial_id} completed outside goal: {final_position}"
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
                    raise RuntimeError(
                        f"{trial_id} did not fail safely on its start support: "
                        f"{final_position}"
                    )
                if (actual_damage > budget.maximum_expected_damage_points + 1e-6
                        and not (
                            injection == "remove_landing_support_at_lead"
                            and not trial.get("expected_safe_stop", False)
                            and driver.state in {"failed", "cancelled"}
                        )):
                    raise RuntimeError(
                        f"{trial_id} exceeded damage budget: {actual_damage}"
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
                    "passed": True,
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
    checks = ([] if review20_only else [
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
    return summary, diagnostics, checks
