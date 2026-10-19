"""Fixed-input D118 calculator and Fabric calibration runtime.

This module is diagnostic-only.  It does not add a movement profile or make a
planning decision.  Every trial is fully declared before it starts, and the
manifest hash covers every input tick.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Callable, Iterable, Mapping

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import (
    ComparisonOperatorV0,
    SuccessCriterionV0,
    TaskIntentV0,
)
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus,
    JAVA_1_21_RULESET,
    PhysicsState,
    TickInput,
)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import (
    BlockGeometry, CellKnowledge,
    ObservationStamp,
    WorldKnowledge,
    WorldSessionId,
)
from scripts.b10_gap_solver_runtime import (
    _bounded_fixture_feet_y, _wait_for_b10_teleport_settlement,
)
from scripts.control_probe_core import append_jsonl, write_json_atomic


_SOURCE = "d118-momentum-calibration"
_RUN = TickInput(1.0, 0.0, False, False, True, 0.0)
_JUMP = TickInput(1.0, 0.0, True, False, True, 0.0)
_FORWARD = TickInput(1.0, 0.0, False, False, False, 0.0)
_NEUTRAL = TickInput(0.0, 0.0, False, False, False, 0.0)
_BACK = TickInput(-1.0, 0.0, False, False, False, 0.0)
_SNEAK = TickInput(0.0, 0.0, False, True, False, 0.0)
_DIRECTIONS = (
    (0, 1, 0.0, "south"),
    (1, 0, -90.0, "east"),
    (0, -1, 180.0, "north"),
    (-1, 0, 90.0, "west"),
)
_INPUT_KEYS = ("forward", "strafe", "jump", "sneak", "sprint")


def fixture_command_row(
    *, trial_id: str, phase: str, material: str,
    origin: tuple[int, int], feet_y: int,
    positions: Iterable[tuple[int, int, int]],
) -> dict:
    if type(trial_id) is not str or not trial_id:
        raise ValueError("D118 fixture trial identity is invalid")
    if phase not in {"clear", "support"}:
        raise ValueError("D118 fixture phase is invalid")
    if (type(origin) is not tuple or len(origin) != 2
            or any(type(value) is not int for value in origin)
            or type(feet_y) is not int):
        raise ValueError("D118 fixture coordinates are invalid")
    return {
        "schema_version": "mc2p.d118-fixture-command.v1",
        "trial_id": trial_id,
        "phase": phase,
        "origin": list(origin),
        "feet_y": feet_y,
        "material": material,
        "positions": [list(position) for position in positions],
    }


class TimingKind(StrEnum):
    WORKER_DELIVERY = "worker_delivery"
    INPUT_STREAM_START = "input_stream_start"
    MID_STREAM_DEVIATION = "mid_stream_deviation"


@dataclass(frozen=True, slots=True)
class FixedCommand:
    forward: int
    strafe: int
    jump: bool
    sneak: bool
    sprint: bool
    stream_id: str
    phase: str

    def movement(self) -> MovementV1:
        return MovementV1(
            self.forward, self.strafe, self.jump, self.sneak, self.sprint,
        )


@dataclass(frozen=True, slots=True)
class CalibrationTrial:
    trial_id: str
    family: str
    direction_index: int
    repetition: int
    timing_kind: TimingKind
    start_local: tuple[float, float, float]
    first_gap_cells: int
    middle_platform_cells: int
    second_gap_cells: int
    commands: tuple[FixedCommand, ...]
    fallback: str | None = None


@dataclass(frozen=True, slots=True)
class CalibrationManifest:
    schema_version: str
    trials: tuple[CalibrationTrial, ...]
    sha256: str


def trial_fixture_rows(
    trial: CalibrationTrial,
    *,
    origin: tuple[int, int],
    feet_y: int,
) -> tuple[dict, dict]:
    """Declare the complete isolated physics volume for one fixed trial."""
    target_start = (
        trial.first_gap_cells + trial.middle_platform_cells
        + trial.second_gap_cells
    )
    local_support_z = (
        *range(-12, 0),
        *range(trial.first_gap_cells,
               trial.first_gap_cells + trial.middle_platform_cells),
        *range(target_start, target_start + 6),
    )
    support_set = set()
    volume = set()
    for local_z_block in range(-14, target_start + 9):
        for local_x_block in range(-2, 3):
            x, z = _local_to_world(
                origin, trial.direction_index,
                local_x_block + 0.5, local_z_block + 0.5,
            )
            block_x, block_z = math.floor(x), math.floor(z)
            # Twenty fallback ticks can descend far below the platform.
            # Keep that whole deterministic horizon declared AIR rather than
            # letting a negative control become NEEDS_WORLD.
            for y in range(feet_y - 24, feet_y + 5):
                volume.add((block_x, y, block_z))
            if (local_x_block in (-1, 0, 1)
                    and local_z_block in local_support_z):
                support_set.add((block_x, feet_y - 1, block_z))
    return (
        fixture_command_row(
            trial_id=trial.trial_id, phase="clear",
            origin=origin, feet_y=feet_y, material="minecraft:air",
            positions=tuple(sorted(volume - support_set)),
        ),
        fixture_command_row(
            trial_id=trial.trial_id, phase="support",
            origin=origin, feet_y=feet_y,
            material="minecraft:grass_block",
            positions=tuple(sorted(support_set)),
        ),
    )


def _commands(
    count: int,
    movement: TickInput,
    *,
    stream: str,
    phase: str,
) -> tuple[FixedCommand, ...]:
    return tuple(FixedCommand(
        int(movement.forward), int(movement.strafe), movement.jump,
        movement.sneak, movement.sprint, stream, phase,
    ) for _ in range(count))


def _trial_templates() -> tuple[CalibrationTrial, ...]:
    four_gap = (
        *_commands(17, _RUN, stream="jump-1", phase="run_up"),
        *_commands(1, _JUMP, stream="jump-1", phase="takeoff"),
        *_commands(11, _RUN, stream="jump-1", phase="flight"),
    )
    first_jump = (
        *_commands(14, _RUN, stream="jump-1", phase="run_up"),
        *_commands(1, _JUMP, stream="jump-1", phase="takeoff"),
        *_commands(11, _RUN, stream="jump-1", phase="flight_and_landing"),
    )
    second_jump = (
        *_commands(1, _JUMP, stream="jump-2", phase="takeoff"),
        *_commands(11, _RUN, stream="jump-2", phase="flight"),
    )
    return (
        CalibrationTrial(
            "four-gap-south-0", "four_gap_sprint_jump", 0, 0,
            TimingKind.INPUT_STREAM_START, (0.5, 64.0, -4.0),
            4, 6, 0, four_gap,
        ),
        CalibrationTrial(
            "landing-immediate-south-0", "landing_immediate_jump", 0, 0,
            TimingKind.INPUT_STREAM_START, (0.5, 64.0, -5.17),
            1, 1, 4, (*first_jump, *second_jump),
        ),
        CalibrationTrial(
            "landing-immediate-late1-south-0", "landing_immediate_jump", 0, 0,
            TimingKind.INPUT_STREAM_START, (0.5, 64.0, -5.17),
            1, 1, 4,
            (*first_jump, *_commands(
                1, _RUN, stream="jump-1", phase="stream_start_late_hold",
            ), *second_jump),
        ),
        CalibrationTrial(
            "edge-last-grounded-south-0", "last_grounded_edge_jump", 0, 0,
            TimingKind.MID_STREAM_DEVIATION, (0.5, 64.0, -4.0),
            4, 6, 0, four_gap,
        ),
        *tuple(CalibrationTrial(
            f"fallback-{name}-south-0", "momentum_landing_fallback", 0, 0,
            TimingKind.MID_STREAM_DEVIATION, (0.5, 64.0, -5.17),
            1, 1, 4,
            (*first_jump, *_commands(
                20, tail, stream="fallback", phase=f"fallback_{name}",
            )), fallback=name,
        ) for name, tail in (
            ("neutral", _NEUTRAL), ("back", _BACK), ("sneak", _SNEAK),
        )),
    )


def _transform_trial(
    template: CalibrationTrial,
    direction_index: int,
    repetition: int,
) -> CalibrationTrial:
    if direction_index not in range(4):
        raise ValueError("D118 direction must be in range(4)")
    direction = _DIRECTIONS[direction_index][3]
    base_name = template.trial_id.rsplit("-south-0", 1)[0]
    return replace(
        template,
        trial_id=f"{base_name}-{direction}-{repetition}",
        direction_index=direction_index,
        repetition=repetition,
    )


def _manifest_payload(manifest: CalibrationManifest) -> dict:
    return {
        "schema_version": manifest.schema_version,
        "trials": [asdict(trial) for trial in manifest.trials],
    }


def manifest_sha256(manifest: CalibrationManifest) -> str:
    encoded = json.dumps(
        _manifest_payload(manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_calibration_manifest(
    *,
    directions: tuple[int, ...] = (0, 1, 2, 3),
    repetitions: int = 3,
) -> CalibrationManifest:
    if (type(directions) is not tuple or not directions
            or any(type(value) is not int or value not in range(4)
                   for value in directions)
            or len(set(directions)) != len(directions)):
        raise ValueError("D118 directions must be unique direction indices")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("D118 repetitions must be positive")
    trials = tuple(
        _transform_trial(template, direction, repetition)
        for direction in directions
        for repetition in range(repetitions)
        for template in _trial_templates()
    )
    manifest = CalibrationManifest(
        "mc2p.d118-calibration-manifest.v1", trials, "",
    )
    return replace(manifest, sha256=manifest_sha256(manifest))


def runtime_receipt_row(
    *,
    trial: CalibrationTrial,
    command_index: int,
    episode_id: str,
    request_sequence_id: int,
    observation_sequence_id: int,
    generation_id: int,
    movement_tick_id: int,
    expected_movement: Mapping,
    applied_movement: Mapping,
) -> dict:
    return {
        "schema_version": "mc2p.d118-runtime-receipt.v1",
        "trial_id": trial.trial_id,
        "command_index": command_index,
        "timing_kind": trial.timing_kind.value,
        "episode_id": episode_id,
        "request_sequence_id": request_sequence_id,
        "observation_sequence_id": observation_sequence_id,
        "generation_id": generation_id,
        "movement_tick_id": movement_tick_id,
        "expected_movement": {
            key: expected_movement.get(key) for key in _INPUT_KEYS
        },
        "applied_movement": {
            key: applied_movement.get(key) for key in _INPUT_KEYS
        },
    }


def physics_sidecar_row(
    *, episode_id: str, request_sequence_id: int, movement_tick_id: int,
    actual_input: Mapping | None = None,
) -> dict:
    """Java-sidecar-shaped row used by component timeline tests."""
    state = {
        "position": {"x": 0.5, "y": 64.0, "z": 0.5},
        "velocity": {"x": 0.0, "y": 0.0, "z": 0.0},
        "yaw": 0.0, "pitch": 0.0, "on_ground": True,
        "horizontal_collision": False, "vertical_collision": True,
        "actual_sprinting": False, "actual_sneaking": False,
        "pose": "standing",
    }
    return {
        "schema_version": "mc2p.client-physics-tick.v1",
        "session_id": "session-1",
        "world_id": "world-1",
        "request_sequence_id": request_sequence_id,
        "episode_id": episode_id,
        "movement_tick_id": movement_tick_id,
        "time_event_sequence": 1,
        "client_ticks": movement_tick_id,
        "sampled_at_jvm_ns": 10,
        "input_state": "leased",
        "movement_yaw": 0.0,
        "movement_pitch": 0.0,
        "actual_input": ({
            "forward": 1.0, "strafe": 0.0, "jump": False,
            "sneak": False, "sprint": True,
        } if actual_input is None else {
            key: actual_input.get(key) for key in _INPUT_KEYS
        }),
        "pre_state": state,
        "post_state": dict(state),
        "contact_events": ["vertical_collision"],
    }


def ftick_sidecar_rows(
    *, episode_id: str, request_sequence_id: int,
    observation_sequence_id: int, generation_id: int, movement_tick_id: int,
) -> tuple[dict, ...]:
    common = {
        "schema_version": "mc2p.client-ftick-event.v1",
        "episode_id": episode_id,
        "request_sequence_id": request_sequence_id,
    }
    return (
        {**common, "event_sequence": 1, "kind": "CLIENT_FRAME_RECEIVED",
         "generation_id": None,
         "observation_sequence_id": observation_sequence_id,
         "movement_tick_id": None, "monotonic_ns": 10},
        {**common, "event_sequence": 2, "kind": "INPUT_SAMPLED",
         "generation_id": None, "observation_sequence_id": None,
         "movement_tick_id": movement_tick_id, "monotonic_ns": 20},
        {**common, "event_sequence": 3, "kind": "MOVEMENT_APPLIED",
         "generation_id": None, "observation_sequence_id": None,
         "movement_tick_id": movement_tick_id, "monotonic_ns": 30},
        {**common, "event_sequence": 4, "kind": "CLIENT_OBSERVATION_SENT",
         "generation_id": generation_id,
         "observation_sequence_id": observation_sequence_id,
         "movement_tick_id": movement_tick_id, "monotonic_ns": 40},
    )


def join_command_timeline(
    receipt: Mapping,
    physics_rows: Iterable[Mapping],
    ftick_rows: Iterable[Mapping],
) -> dict:
    request = receipt.get("request_sequence_id")
    episode = receipt.get("episode_id")
    observation = receipt.get("observation_sequence_id")
    generation = receipt.get("generation_id")
    movement_tick = receipt.get("movement_tick_id")
    request_physics = tuple(
        row for row in physics_rows
        if row.get("schema_version") == "mc2p.client-physics-tick.v1"
        and row.get("episode_id") == episode
        and row.get("request_sequence_id") == request
    )
    physics = tuple(
        row for row in request_physics
        if row.get("movement_tick_id") == movement_tick
    )
    if len(physics) != 1:
        raise ValueError("D118 command requires exactly one physics row")
    prior_physics = tuple(
        row for row in request_physics
        if type(row.get("movement_tick_id")) is int
        and row.get("movement_tick_id") < movement_tick
    )
    if prior_physics:
        raise ValueError("D118 request was sampled before its command application")
    tail_physics = tuple(sorted((
        row for row in request_physics
        if type(row.get("movement_tick_id")) is int
        and row.get("movement_tick_id") > movement_tick
    ), key=lambda row: row["movement_tick_id"]))
    neutral = {
        "forward": 0.0, "strafe": 0.0, "jump": False,
        "sneak": False, "sprint": False,
    }
    inactive_states = {"lease_exhausted", "expired", "disallowed"}
    for row in tail_physics:
        if row.get("input_state") not in inactive_states:
            raise ValueError("D118 tail application is leased")
        actual = row.get("actual_input")
        if (type(actual) is not dict
                or any(actual.get(key) != value
                       for key, value in neutral.items())):
            raise ValueError("D118 tail input is not neutral")
    events = tuple(row for row in ftick_rows
                   if row.get("schema_version") == "mc2p.client-ftick-event.v1"
                   and row.get("episode_id") == episode
                   and row.get("request_sequence_id") == request)
    frame_rows = tuple(
        row for row in events
        if row.get("kind") == "CLIENT_FRAME_RECEIVED"
    )
    sampled_rows = tuple(
        row for row in events
        if row.get("kind") == "INPUT_SAMPLED"
        and row.get("movement_tick_id") == movement_tick
    )
    applied_rows = tuple(
        row for row in events
        if row.get("kind") == "MOVEMENT_APPLIED"
        and row.get("movement_tick_id") == movement_tick
    )
    sent_rows = tuple(
        row for row in events
        if row.get("kind") == "CLIENT_OBSERVATION_SENT"
    )
    selected = (frame_rows, sampled_rows, applied_rows, sent_rows)
    if any(len(rows) != 1 for rows in selected):
        raise ValueError("D118 command requires one of every F-TICK event")
    by_kind = {
        "CLIENT_FRAME_RECEIVED": frame_rows[0],
        "INPUT_SAMPLED": sampled_rows[0],
        "MOVEMENT_APPLIED": applied_rows[0],
        "CLIENT_OBSERVATION_SENT": sent_rows[0],
    }
    tail_ticks = {row["movement_tick_id"] for row in tail_physics}
    if len(tail_ticks) != len(tail_physics):
        raise ValueError("D118 tail physics identity is duplicate")
    extra_movement_events = tuple(
        row for row in events
        if row.get("kind") in {"INPUT_SAMPLED", "MOVEMENT_APPLIED"}
        and row.get("movement_tick_id") != movement_tick
    )
    if any(row.get("movement_tick_id") not in tail_ticks
           for row in extra_movement_events):
        raise ValueError("D118 tail F-TICK event has no physics row")
    for tail_tick in tail_ticks:
        for kind in ("INPUT_SAMPLED", "MOVEMENT_APPLIED"):
            matches = tuple(
                row for row in extra_movement_events
                if row.get("kind") == kind
                and row.get("movement_tick_id") == tail_tick
            )
            if len(matches) != 1:
                raise ValueError("D118 tail application F-TICK identity differs")
        sampled_sequence = tuple(
            row["event_sequence"] for row in extra_movement_events
            if row.get("kind") == "INPUT_SAMPLED"
            and row.get("movement_tick_id") == tail_tick
        )[0]
        applied_sequence = tuple(
            row["event_sequence"] for row in extra_movement_events
            if row.get("kind") == "MOVEMENT_APPLIED"
            and row.get("movement_tick_id") == tail_tick
        )[0]
        if (type(sampled_sequence) is not int
                or type(applied_sequence) is not int
                or sampled_sequence >= applied_sequence):
            raise ValueError("D118 tail F-TICK event order differs")
    movement_rows = (
        physics[0], by_kind["INPUT_SAMPLED"],
        by_kind["MOVEMENT_APPLIED"],
        by_kind["CLIENT_OBSERVATION_SENT"],
    )
    if any(row.get("movement_tick_id") != movement_tick
           for row in movement_rows):
        raise ValueError("D118 command movement tick identity differs")
    if by_kind["CLIENT_FRAME_RECEIVED"].get("movement_tick_id") is not None:
        raise ValueError("D118 frame receipt unexpectedly names a movement tick")
    if (by_kind["CLIENT_FRAME_RECEIVED"].get("observation_sequence_id")
            != observation
            or by_kind["CLIENT_OBSERVATION_SENT"].get(
                "observation_sequence_id") != observation
            or by_kind["CLIENT_OBSERVATION_SENT"].get("generation_id")
            != generation):
        raise ValueError("D118 command observation identity differs")
    for kind in ("INPUT_SAMPLED", "MOVEMENT_APPLIED"):
        if by_kind[kind].get("observation_sequence_id") is not None:
            raise ValueError("D118 movement event unexpectedly names an observation")
    expected = receipt.get("expected_movement")
    applied = receipt.get("applied_movement")
    physical = physics[0].get("actual_input")
    if any(type(value) is not dict for value in (expected, applied, physical)):
        raise ValueError("D118 command input evidence is incomplete")
    for key in _INPUT_KEYS:
        if expected.get(key) != applied.get(key) or applied.get(key) != physical.get(key):
            raise ValueError(f"D118 command input differs at {key}")
    required = (
        "CLIENT_FRAME_RECEIVED", "INPUT_SAMPLED", "MOVEMENT_APPLIED",
        "CLIENT_OBSERVATION_SENT",
    )
    sequences = [by_kind[kind].get("event_sequence") for kind in required]
    if any(type(value) is not int for value in sequences) or sequences != sorted(sequences):
        raise ValueError("D118 F-TICK event order differs")
    return {
        "trial_id": receipt.get("trial_id"),
        "command_index": receipt.get("command_index"),
        "timing_kind": receipt.get("timing_kind"),
        "request_sequence_id": request,
        "episode_id": episode,
        "observation_sequence_id": observation,
        "generation_id": generation,
        "movement_tick_id": movement_tick,
        "tail_application_count": len(tail_physics),
        "tail_first_movement_tick_id": (
            tail_physics[0]["movement_tick_id"] if tail_physics else None
        ),
        "tail_last_movement_tick_id": (
            tail_physics[-1]["movement_tick_id"] if tail_physics else None
        ),
    }


class _Course:
    """The frozen straight-ahead strategy space used by D118-0."""

    START_BACK = -12

    def __init__(self, first_gap: int, middle: int, second_gap: int) -> None:
        self.middle = (first_gap, first_gap + middle)
        start = first_gap + middle + second_gap
        self.target = (start, start + 6)
        self.session = WorldSessionId("d118-calculator")
        stamp = ObservationStamp(self.session, 0, 0, "d118-calculator", 0)
        knowledge = WorldKnowledge(self.session)
        knowledge.confirm_air(stamp, tuple(
            (x, y, z)
            for x in range(-2, 3)
            for y in range(56, 72)
            for z in range(self.START_BACK - 2, self.target[1] + 3)
        ))
        blocks = {
            (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-1, 2)
            for z in (
                *range(self.START_BACK, 0),
                *range(*self.middle),
                *range(*self.target),
            )
        }
        knowledge.observe_blocks(stamp, blocks)
        self.world = PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET)

    def state(self, z: float) -> PhysicsState:
        return PhysicsState(
            JAVA_1_21_RULESET.ruleset_id,
            JAVA_1_21_RULESET.state_schema,
            self.session,
            0,
            (0.5, 64.0, z),
            (0.0, -0.0784000015258789, 0.0),
            0.0,
            0.0,
            "standing",
            0.6,
            1.8,
            True,
            False,
            True,
            False,
            False,
            0,
            0.0,
            0.1,
            0.6,
            0.08,
            0.42,
            20,
            5.0,
            "survival",
            (),
            False,
            False,
            False,
            False,
            False,
            False,
        )

    def advance(self, state: PhysicsState, tick_input: TickInput) -> PhysicsState | None:
        result = physics_step(state, tick_input, self.world, JAVA_1_21_RULESET)
        return result.next_state if result.status is CalculationStatus.OK else None

    def on(self, state: PhysicsState, span: tuple[int, int]) -> bool:
        return bool(
            state.on_ground
            and abs(state.position[1] - 64.0) < 1.0e-6
            and span[0] - 0.3 < state.position[2] < span[1] + 0.3
        )

    def fly(self, state: PhysicsState) -> PhysicsState | None:
        for _ in range(40):
            state = self.advance(state, _RUN)
            if state is None or state.position[1] < 62.0:
                return None
            if state.on_ground:
                return state
        return None


def _lands_after(course: _Course, start: float, run_ticks: int) -> tuple[
        PhysicsState, PhysicsState] | None:
    state = course.state(-start)
    for _ in range(run_ticks):
        state = course.advance(state, _RUN)
        if state is None or not state.on_ground:
            return None
    state = course.advance(state, _JUMP)
    if state is None or state.on_ground:
        return None
    before = state
    for _ in range(40):
        after = course.advance(before, _RUN)
        if after is None or after.position[1] < 62.0:
            return None
        if after.on_ground:
            return (before, after) if course.on(after, course.middle) else None
        before = after
    return None


def _second_jump_succeeds(
    course: _Course,
    before_landing: PhysicsState,
    sequence: tuple[TickInput, ...],
) -> bool:
    state = before_landing
    took_off = False
    for index, tick_input in enumerate(sequence):
        state = course.advance(state, tick_input)
        if state is None:
            return False
        if index >= 1 and not state.on_ground:
            took_off = True
            break
        if index >= 1 and not (state.on_ground and abs(state.position[1] - 64.0) < 1e-6):
            return False
    if not took_off:
        return False
    final = course.fly(state)
    return final is not None and course.on(final, course.target)


def _stops(
    course: _Course,
    before_landing: PhysicsState,
    prefix: tuple[TickInput, ...],
    tail: TickInput,
) -> bool:
    state = before_landing
    for tick_input in prefix:
        state = course.advance(state, tick_input)
        if state is None:
            return False
    for _ in range(30):
        state = course.advance(state, tail)
        if state is None or not course.on(state, course.middle):
            return False
        horizontal = math.hypot(
            state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2],
        )
        if horizontal < 0.003:
            return True
    return False


@lru_cache(maxsize=1)
def calculate_frozen_facts() -> dict:
    course = _Course(1, 1, 4)
    feasible = tolerant = 0
    low, high = course.middle
    for index in range(int(round((high - low + 0.58) / 0.01)) + 1):
        z = round(low - 0.29 + 0.01 * index, 2)
        good = []
        for jump_tick in range(14):
            state = course.state(z)
            for _ in range(jump_tick):
                state = course.advance(state, _RUN)
                if state is None or not state.on_ground:
                    break
            else:
                state = course.advance(state, _JUMP)
                landed = None if state is None or state.on_ground else course.fly(state)
                if landed is not None and course.on(landed, course.target):
                    good.append(jump_tick)
        if good:
            feasible += 1
            tolerant += int(any(value + 1 in good for value in good))

    trajectories = []
    start = 0.29
    while start <= 8.0:
        for jump_tick in range(40):
            landing = _lands_after(course, start, jump_tick)
            if landing is not None:
                trajectories.append(landing)
            elif jump_tick:
                # Once the run has already left the platform, later takeoff
                # phases from the same start cannot become grounded again.
                state = course.state(-start)
                for _ in range(jump_tick):
                    state = course.advance(state, _RUN)
                    if state is None or not state.on_ground:
                        break
                if state is None or not state.on_ground:
                    break
        start = round(start + 0.02, 2)

    on_time = [pair for pair in trajectories
               if _second_jump_succeeds(course, pair[0], (_RUN, _JUMP))]
    momentum_entry_speeds = tuple(
        20.0 * math.hypot(
            pair[1].velocity_blocks_per_tick[0],
            pair[1].velocity_blocks_per_tick[2],
        )
        for pair in on_time
    )
    profile_document = json.loads((
        Path(__file__).resolve().parents[1]
        / "config/motion-navigation/air-motions-b09-v1.json"
    ).read_text(encoding="utf-8"))
    legacy_gap_profile = next(
        profile for profile in profile_document["profiles"]
        if profile["mode"] == "jump_gap"
    )
    legacy_exit_maximum = float(
        legacy_gap_profile["landing"]["maximum_exit_speed_blocks_per_second"]
    )
    late = sum(_second_jump_succeeds(
        course, pair[0], (_RUN, _RUN, _JUMP),
    ) for pair in on_time)
    eligible = [pair for pair in trajectories
                if _second_jump_succeeds(course, pair[0],
                                         (_RUN, _FORWARD, _JUMP))]
    fallback = {
        "eligible_landings": len(eligible),
        "neutral_l1_safe": sum(_stops(
            course, pair[0], (_RUN,), _NEUTRAL,
        ) for pair in eligible),
        "back_l1_safe": sum(_stops(
            course, pair[0], (_RUN,), _BACK,
        ) for pair in eligible),
        "sneak_l1_safe": sum(_stops(
            course, pair[0], (_RUN,), _SNEAK,
        ) for pair in eligible),
        "sneak_l2_safe": sum(_stops(
            course, pair[0], (_RUN, _FORWARD), _SNEAK,
        ) for pair in eligible),
    }
    return {
        "standstill_four_gap": {
            "feasible": feasible,
            "input_stream_start_late_one_tolerant": tolerant,
        },
        "momentum_four_gap": {
            "landings": len(trajectories),
            "on_time": len(on_time),
            "input_stream_start_late_one_tolerant": late,
        },
        "entry_window_separation": {
            "legacy_exit_maximum_blocks_per_second": legacy_exit_maximum,
            "momentum_entry_minimum_blocks_per_second": min(
                momentum_entry_speeds,
            ),
            "momentum_entry_maximum_blocks_per_second": max(
                momentum_entry_speeds,
            ),
            "overlaps": legacy_exit_maximum >= min(momentum_entry_speeds),
        },
        "fallback": fallback,
    }


def _local_to_world(
    origin: tuple[int, int],
    direction_index: int,
    local_x: float,
    local_z: float,
) -> tuple[float, float]:
    dx, dz, _, _ = _DIRECTIONS[direction_index]
    lateral_x, lateral_z = dz, -dx
    return (
        origin[0] + local_z * dx + local_x * lateral_x,
        origin[1] + local_z * dz + local_x * lateral_z,
    )


def _assert_observed_fixture(
    frame,
    trial: CalibrationTrial,
    origin: tuple[int, int],
    feet_y: int,
) -> None:
    """Fail before tick zero unless the traversed fixture is known exactly."""
    body_cell = (
        math.floor(frame.body.position[0]), math.floor(frame.body.position[2]),
    )
    for position, expected in _fixture_fact_positions(
            trial, origin, feet_y).items():
        fact = frame.world.cell(position)
        body_proves_clear = (
            expected is CellKnowledge.AIR
            and (position[0], position[2]) == body_cell
            and feet_y <= position[1] < math.ceil(frame.body.body_box.max_y)
        )
        if body_proves_clear:
            continue
        if fact.knowledge is not expected:
            raise RuntimeError(
                "D118 tick-zero fixture fact differs: "
                f"{position} expected={expected.value} actual={fact.knowledge.value}"
            )
        if (expected is CellKnowledge.BLOCK
                and (fact.block is None
                     or fact.block.material_key != "minecraft:grass_block")):
            raise RuntimeError("D118 tick-zero support material differs")


def _fixture_fact_positions(
    trial: CalibrationTrial,
    origin: tuple[int, int],
    feet_y: int,
) -> dict[tuple[int, int, int], CellKnowledge]:
    target_start = (
        trial.first_gap_cells + trial.middle_platform_cells
        + trial.second_gap_cells
    )
    supported = {
        *range(-12, 0),
        *range(trial.first_gap_cells,
               trial.first_gap_cells + trial.middle_platform_cells),
        *range(target_start, target_start + 2),
    }
    first_z = math.floor(trial.start_local[2])
    result = {}
    for local_z in range(first_z, target_start + 2):
        x, z = _local_to_world(
            origin, trial.direction_index, 0.5, local_z + 0.5,
        )
        column = (math.floor(x), math.floor(z))
        expected = (
            CellKnowledge.BLOCK if local_z in supported else CellKnowledge.AIR
        )
        result[(column[0], feet_y - 1, column[1])] = expected
        for y in (feet_y, feet_y + 1):
            result[(column[0], y, column[1])] = CellKnowledge.AIR
    return result


def run_d118_momentum_calibration_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[..., None],
    player_teleporter: Callable[[float, float, float, float, float], None],
    *,
    smoke: bool = True,
    manifest: CalibrationManifest | None = None,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    observation_diagnostic: Callable[[], dict] | None = None,
) -> tuple[dict, list[dict], list[dict]]:
    """Run the immutable manifest through Runtime's sole input exit."""
    manifest = manifest or build_calibration_manifest(
        directions=(0,) if smoke else (0, 1, 2, 3), repetitions=1 if smoke else 3,
    )
    if manifest.sha256 != manifest_sha256(manifest):
        raise ValueError("D118 runtime received a changed fixed manifest")
    write_json_atomic(directory / "d118-input-manifest.json", {
        **_manifest_payload(manifest), "sha256": manifest.sha256,
    })
    task = TaskIntentV0(
        "d118-momentum-calibration", "fixed_input_calibration", "{}",
        (SuccessCriterionV0(
            "fixed_trials", ComparisonOperatorV0.EQUAL,
            len(manifest.trials), "count",
        ),),
        5000, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    adapter = NavigationObservationAdapter()
    frame = adapter.ingest(runtime.observation)
    diagnostic_rows = []
    if observation_diagnostic is not None:
        diagnostic_rows.append(observation_diagnostic())
    request = ObservationRequestV3("navigation_v1", ())
    counter = 0
    receipt_rows = []
    trials = []

    def step(command: FixedCommand):
        nonlocal counter, frame
        runtime.cancel_source(_SOURCE)
        counter += 1
        now = clock_ns()
        movement = command.movement()
        runtime.submit_intent(ActionIntentV1(
            f"d118-{counter}", _SOURCE, episode,
            runtime.observation.sequence_id, ActionPriorityV0.TASK,
            now, min(deadline_ns, now + 750_000_000), movement=movement,
            valid_for_ticks=1,
        ))
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=request,
        )
        if (result.observation is None or result.backend_result is None
                or result.decision is None):
            raise RuntimeError(f"D118 fixed input step failed: {result.report}")
        if result.decision.action.movement != movement:
            raise RuntimeError("D118 movement bypassed the formal Runtime arbiter")
        frame = adapter.ingest(result.observation)
        if observation_diagnostic is not None:
            diagnostic_rows.append(observation_diagnostic())
        return result

    neutral = FixedCommand(0, 0, False, False, False, "setup", "teleport_wait")
    setup_steps = 0
    while (type(frame.body.movement_tick_id) is not int
           or frame.body.movement_tick_id < 0):
        if setup_steps >= 5:
            raise RuntimeError(
                "D118 setup did not observe a valid movement tick"
            )
        step(neutral)
        setup_steps += 1
    feet_y = _bounded_fixture_feet_y(frame.body.position[1])
    base_origin = (math.floor(frame.body.position[0]) + 24,
                   math.floor(frame.body.position[2]) + 24)
    for trial in manifest.trials:
        if clock_ns() >= deadline_ns:
            raise TimeoutError("D118 calibration deadline exceeded")
        # Rebuild the complete declared volume at one fixed origin. Clearing it
        # first prevents a previous variant from leaving relevant geometry.
        origin = base_origin
        for fixture in trial_fixture_rows(
                trial, origin=origin, feet_y=feet_y):
            fixture_writer(
                tuple(tuple(position) for position in fixture["positions"]),
                fixture["material"], trial_id=fixture["trial_id"],
                phase=fixture["phase"], origin=origin, feet_y=feet_y,
            )
        local_x, _, local_z = trial.start_local
        start_x, start_z = _local_to_world(
            origin, trial.direction_index, local_x, local_z,
        )
        yaw = _DIRECTIONS[trial.direction_index][2]
        request = ObservationRequestV3(
            "navigation_v1",
            tuple(_fixture_fact_positions(trial, origin, feet_y)),
        )
        starting = frame.body
        player_teleporter(start_x, float(feet_y), start_z, yaw, 0.0)

        def advance_teleport():
            step(neutral)
            return frame.body

        _wait_for_b10_teleport_settlement(
            advance_teleport,
            starting_movement_tick=starting.movement_tick_id,
            starting_position=starting.position,
            starting_yaw_degrees=math.degrees(starting.yaw_radians),
            starting_on_ground=starting.is_on_ground,
            target_position=(start_x, float(feet_y), start_z),
            yaw_degrees=yaw,
            position_tolerance=0.03,
        )
        start_speed = math.hypot(
            frame.body.velocity_blocks_per_second[0],
            frame.body.velocity_blocks_per_second[2],
        )
        if not frame.body.is_on_ground or start_speed > 0.03:
            raise RuntimeError("D118 trial start is not grounded and still")
        _assert_observed_fixture(frame, trial, origin, feet_y)
        samples = []
        for command_index, command in enumerate(trial.commands):
            result = step(command)
            receipt = result.backend_result.receipt
            action = result.decision.action
            request_sequence = action.request_sequence_id
            applications = tuple(
                item for item in receipt.input_applications
                if item.request_sequence_id == request_sequence
            )
            if len(applications) != 1:
                raise RuntimeError("D118 Runtime receipt identity is not unique")
            application = applications[0]
            row = runtime_receipt_row(
                trial=trial,
                command_index=command_index,
                episode_id=episode,
                request_sequence_id=request_sequence,
                observation_sequence_id=action.observation_sequence_id,
                generation_id=receipt.generation_id,
                movement_tick_id=application.movement_tick_id,
                expected_movement=asdict(command.movement()),
                applied_movement={
                    key: getattr(application, key) for key in _INPUT_KEYS
                },
            )
            row.update(
                result_observation_sequence_id=frame.body.sequence_id,
                stream_id=command.stream_id,
                phase=command.phase,
                movement=asdict(command.movement()),
                position=list(frame.body.position),
                velocity_blocks_per_second=list(
                    frame.body.velocity_blocks_per_second
                ),
                on_ground=frame.body.is_on_ground,
            )
            receipt_rows.append(row)
            samples.append(row)
            append_jsonl(directory / "d118-runtime-receipts.jsonl", row)
        trials.append({
            "trial_id": trial.trial_id,
            "family": trial.family,
            "direction_index": trial.direction_index,
            "repetition": trial.repetition,
            "timing_kind": trial.timing_kind.value,
            "fallback": trial.fallback,
            "manifest_sha256": manifest.sha256,
            "samples": samples,
        })
        append_jsonl(directory / "d118-trials.jsonl", trials[-1])

    runtime.cancel_source(_SOURCE)
    summary = {
        "schema_version": "mc2p.d118-calibration-runtime.v1",
        "status": "recorded",
        "manifest_sha256": manifest.sha256,
        "trial_count": len(trials),
        "command_count": len(receipt_rows),
        "trials": trials,
        "timing_kinds": [kind.value for kind in TimingKind],
        "worker_delivery_scope": "reported_separately_not_injected",
        "input_timing_scope": (
            "serial Runtime requests measure the next available L+1 application; "
            "they do not pre-schedule input at landing tick L and do not inject "
            "a real transport late tick"
        ),
        "prescheduled_landing_input": False,
        "transport_late_tick_injected": False,
        "setup_neutral_steps": setup_steps,
    }
    write_json_atomic(directory / "d118-calibration-runtime.json", summary)
    checks = [
        {"name": "d118_fixed_manifest_hash_unchanged", "passed": all(
            row["manifest_sha256"] == manifest.sha256 for row in trials
        )},
        {"name": "d118_runtime_receipts_unique", "passed": (
            len(receipt_rows) == sum(
                len(trial.commands) for trial in manifest.trials
            )
            and len({(row["trial_id"], row["command_index"])
                     for row in receipt_rows}) == len(receipt_rows)
        )},
    ]
    return summary, diagnostic_rows, checks
