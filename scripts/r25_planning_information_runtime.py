"""One profile-4 Fabric slice for R25 planning information ownership."""
from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import time
from typing import Callable

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import (
    ComparisonOperatorV0,
    SuccessCriterionV0,
    TaskIntentV0,
)
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_model import Aabb, CellKnowledge
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver, RuntimeNavigationDriverState
from scripts.control_probe_core import append_jsonl, write_json_atomic


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
FEET_Y = 100
_PATH_CLEARANCE = (0, FEET_Y, 6)
_STRUCTURAL_UNKNOWN = (1, FEET_Y, 3)


def _task(deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0(
        "r25-planning-information",
        "known_world_navigation",
        "{}",
        (SuccessCriterionV0(
            "goal_reached", ComparisonOperatorV0.EQUAL, 1, "count",
        ),),
        100,
        deadline_ns,
        True,
        0.0,
    )


def _fixture_commands() -> tuple[str, ...]:
    return (
        "difficulty peaceful",
        "time set midnight",
        "gamerule doDaylightCycle false",
        "gamerule doWeatherCycle false",
        "gamerule doMobSpawning false",
        "weather clear",
        "kill @e[type=!minecraft:player]",
        "gamemode creative MC2PProbe",
        f"fill -3 {FEET_Y - 2} -3 3 {FEET_Y + 4} 10 minecraft:air replace",
        f"fill -2 {FEET_Y - 1} -1 2 {FEET_Y - 1} 8 minecraft:stone replace",
        # The side wall hides a cell inside the planning prism but outside the
        # straight route.  The planner must not request it before the path
        # clearance fact that actually blocks progress.
        f"fill 1 {FEET_Y} 2 1 {FEET_Y + 2} 2 minecraft:stone replace",
        f"tp MC2PProbe 0.5 {FEET_Y:.1f} 0.5 0.0 68.0",
    )


def _start_command() -> tuple[str, ...]:
    return (f"tp MC2PProbe 0.5 {FEET_Y:.1f} 0.5 180.0 0.0",)


def run_r25_planning_information_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[str, ...]], None],
    *, trace_owns_diagnostics: bool = False,
) -> tuple[dict, list[dict], list[dict]]:
    """Resolve one path blocker while an unrelated hidden cell stays unknown."""
    fixture_writer(_fixture_commands())
    profile = BehaviorProfileV0()
    task = _task(deadline_ns)
    preparation = ObservationRequestV3(
        "navigation_v1",
        tuple((x, FEET_Y - 1, z) for x in range(-1, 2) for z in range(0, 7)),
    )
    diagnostic_rows: list[dict] = []
    frame_rows: list[dict] = []

    def diagnostic() -> None:
        row = {
            "episode_id": episode,
            "observation_sequence_id": runtime.observation.sequence_id,
            "diagnostics": backend.last_diagnostics,
        }
        diagnostic_rows.append(row)
        if not trace_owns_diagnostics:
            append_jsonl(directory / "diagnostics.jsonl", row)

    def step_neutral(request: ObservationRequestV3 | None = None) -> None:
        now = time.perf_counter_ns()
        result = runtime.step(
            task,
            profile,
            min(deadline_ns, now + 500_000_000),
            observation_request=request,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"R25 fixture observation failed: {result.report}")
        diagnostic()

    diagnostic()
    for _ in range(12):
        step_neutral(preparation)
        frame = runtime.navigation_observation_adapter.latest_frame
        if (frame is not None and all(
                frame.world.cell((0, FEET_Y - 1, z)).knowledge
                    is CellKnowledge.BLOCK
                for z in range(0, 7))):
            break
    else:
        raise RuntimeError("R25 fixture did not establish route supports")

    fixture_writer(_start_command())
    for _ in range(4):
        step_neutral()
    frame = runtime.navigation_observation_adapter.latest_frame
    if frame is None:
        raise RuntimeError("R25 fixture lost its navigation frame")
    if frame.world.cell(_PATH_CLEARANCE).knowledge is not CellKnowledge.UNKNOWN:
        raise RuntimeError("R25 path clearance was known before planning")
    if frame.world.cell(_STRUCTURAL_UNKNOWN).knowledge is not CellKnowledge.UNKNOWN:
        raise RuntimeError("R25 structural comparison cell was not hidden")
    if math.dist(frame.body.position, (0.5, float(FEET_Y), 0.5)) > 0.06:
        raise RuntimeError("R25 fixture did not settle at the start")

    profiles = replace(NavigationSessionProfiles.load(CONFIG), air=())
    session = NavigationSession(
        f"{episode}/r25-session",
        profiles,
        observation_adapter=runtime.navigation_observation_adapter,
    )
    driver = RuntimeNavigationDriver(runtime, session)
    goal = GoalState(
        Aabb(0.4, FEET_Y - 0.05, 6.35, 0.6, FEET_Y + 0.05, 6.65),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        0.6,
    )
    driver.start("r25-profile4-goal", 1, goal, time.perf_counter_ns())
    first_information_sequence = None
    clearance_acquired_sequence = None
    first_movement_sequence = None
    requested_positions: set[tuple[int, int, int]] = set()
    first_requested_sequence: dict[tuple[int, int, int], int] = {}
    try:
        for tick in range(240):
            before = session.report
            requested_positions.update(before.missing_cells)
            for position in before.missing_cells:
                first_requested_sequence.setdefault(
                    position, runtime.observation.sequence_id,
                )
            result = driver.tick(
                profile,
                min(deadline_ns, time.perf_counter_ns() + 500_000_000),
            )
            diagnostic()
            frame = runtime.navigation_observation_adapter.latest_frame
            if frame is None:
                raise RuntimeError("R25 driver lost its navigation frame")
            clearance = frame.world.cell(_PATH_CLEARANCE)
            structural = frame.world.cell(_STRUCTURAL_UNKNOWN)
            movement = None if result.decision is None else result.decision.action.movement
            moving = movement is not None and (
                movement.forward != 0
                or movement.strafe != 0
                or movement.jump
            )
            if before.missing_cells and first_information_sequence is None:
                first_information_sequence = frame.body.sequence_id
            if (clearance.knowledge is not CellKnowledge.UNKNOWN
                    and clearance_acquired_sequence is None):
                clearance_acquired_sequence = frame.body.sequence_id
            if moving and first_movement_sequence is None:
                first_movement_sequence = frame.body.sequence_id
            row = {
                "tick": tick,
                "observation_sequence": frame.body.sequence_id,
                "session_state": session.report.state.value,
                "session_reason": session.report.reason,
                "missing_cells": [list(position) for position in before.missing_cells],
                "path_clearance": clearance.knowledge.value,
                "path_clearance_stamp": (
                    None if clearance.stamp is None else clearance.stamp.sequence_id
                ),
                "structural_cell": structural.knowledge.value,
                "movement": moving,
                "planning_work_identity_valid": (
                    session.diagnostics.planning_work_identity_valid
                ),
                "planning_permit_identity_valid": (
                    session.diagnostics.planning_permit_identity_valid
                ),
                "planning_information_identity_valid": (
                    session.diagnostics.planning_information_identity_valid
                ),
            }
            frame_rows.append(row)
            append_jsonl(directory / "r25-planning-information-frames.jsonl", row)
            if driver.state in {RuntimeNavigationDriverState.SUCCESS, RuntimeNavigationDriverState.FAILED, RuntimeNavigationDriverState.CANCELLED}:
                break
        passed = (
            driver.state == RuntimeNavigationDriverState.SUCCESS
            and _PATH_CLEARANCE in requested_positions
            and (
                _STRUCTURAL_UNKNOWN not in first_requested_sequence
                or first_requested_sequence[_PATH_CLEARANCE]
                    < first_requested_sequence[_STRUCTURAL_UNKNOWN]
            )
            and clearance_acquired_sequence is not None
            and first_movement_sequence is not None
            and clearance_acquired_sequence <= first_movement_sequence
            and all(
                row["planning_permit_identity_valid"]
                and (
                    row["session_state"] != "planning"
                    or row["planning_work_identity_valid"]
                )
                and (
                    row["session_state"] != "needs_information"
                    or row["planning_information_identity_valid"]
                )
                for row in frame_rows
            )
        )
        summary = {
            "schema_version": "mc2p.r25-planning-information.v1",
            "passed": passed,
            "driver_state": driver.state.value,
            "driver_reason": driver.reason,
            "path_clearance": list(_PATH_CLEARANCE),
            "structural_unknown": list(_STRUCTURAL_UNKNOWN),
            "requested_positions": [list(position) for position in sorted(requested_positions)],
            "path_clearance_first_requested_sequence": (
                first_requested_sequence.get(_PATH_CLEARANCE)
            ),
            "structural_first_requested_sequence": (
                first_requested_sequence.get(_STRUCTURAL_UNKNOWN)
            ),
            "first_information_sequence": first_information_sequence,
            "clearance_acquired_sequence": clearance_acquired_sequence,
            "first_movement_sequence": first_movement_sequence,
            "final_sequence": (
                frame_rows[-1]["observation_sequence"] if frame_rows else None
            ),
        }
        write_json_atomic(directory / "r25-planning-information.json", summary)
        if not passed:
            raise RuntimeError(f"R25 planning-information probe failed: {summary}")
        if driver.source is not None and not driver.release("r27_frontier_handoff"):
            driver.stop(profile, "r27_frontier_handoff")
        session.close()
        from scripts.r27_successor_gap_runtime import run_successor_gap_case
        summary["r27_successor_gap"] = run_successor_gap_case(
            runtime, task, profile, NavigationSessionProfiles.load(CONFIG), directory,
            deadline_ns, fixture_writer, FEET_Y, diagnostic,
        )
        write_json_atomic(directory / "r25-planning-information.json", summary)
        return summary, diagnostic_rows, [
            {"name": "r25_profile4_planning_information", "passed": True},
            {"name": "r27_formal_successor_gap", "passed": summary["r27_successor_gap"]["passed"]},
        ]
    finally:
        if driver.state not in {RuntimeNavigationDriverState.SUCCESS, RuntimeNavigationDriverState.FAILED, RuntimeNavigationDriverState.CANCELLED, RuntimeNavigationDriverState.STOPPED}:
            driver.stop(profile, "r25_probe_cleanup")
        if driver.source is not None and driver.state in {
                RuntimeNavigationDriverState.SUCCESS, RuntimeNavigationDriverState.FAILED, RuntimeNavigationDriverState.CANCELLED, RuntimeNavigationDriverState.STOPPED}:
            driver.release("r25_probe_complete")
        session.close()
