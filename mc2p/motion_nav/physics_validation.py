"""Offline differential checks between B09-R and frozen Fabric tick evidence."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Iterable, Mapping

from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus, PhysicsRuleset, PhysicsState, TickInput,
)
from mc2p.motion_nav.world_model import (
    BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)


_POSE_HEIGHTS = {"standing": 1.8, "crouching": 1.5, "swimming": .6}
_CONTACT_EVENTS = frozenset({
    "left_ground", "landed", "horizontal_collision", "vertical_collision",
})


@dataclass(frozen=True, slots=True)
class TickDifference:
    movement_tick_id: int
    position_error: tuple[float, float, float]
    velocity_error: tuple[float, float, float]
    contacts_match: bool
    sprint_match: bool
    pose_match: bool


@dataclass(frozen=True, slots=True)
class ValidationReport:
    total_rows: int
    complete_rows: int
    incomplete_rows: int
    differences: tuple[TickDifference, ...]
    issues: tuple[str, ...]
    event_mismatch_rows: tuple[int, ...]
    position_error_max: float
    velocity_error_max: float
    position_error_percentiles: tuple[tuple[str, float], ...]
    velocity_error_percentiles: tuple[tuple[str, float], ...]
    assumptions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpenLoopValidationReport:
    sequence_count: int
    complete_sequences: int
    ticks_compared: int
    issues: tuple[str, ...]
    first_divergence: tuple[tuple[int, int], ...]
    position_error_max: float
    velocity_error_max: float
    position_error_percentiles: tuple[tuple[str, float], ...]
    velocity_error_percentiles: tuple[tuple[str, float], ...]
    position_tolerance: float
    velocity_tolerance: float
    sequence_breaks: tuple[tuple[int, tuple[str, ...]], ...]


def _vector(value: Mapping, name: str) -> tuple[float, float, float]:
    item = value[name]
    return float(item["x"]), float(item["y"]), float(item["z"])


def state_from_tick_evidence(row: Mapping, world: PhysicsWorldView,
                             ruleset: PhysicsRuleset) -> PhysicsState:
    pre = row["pre_state"]
    pose = str(pre["pose"])
    height = _POSE_HEIGHTS[pose]
    return PhysicsState(
        ruleset_id=ruleset.ruleset_id, state_schema=ruleset.state_schema,
        session=world.session, movement_tick_id=int(row["movement_tick_id"]) - 1,
        position=_vector(pre, "position"),
        velocity_blocks_per_tick=_vector(pre, "velocity"),
        yaw_radians=math.radians(float(pre["yaw"])),
        pitch_radians=math.radians(float(pre["pitch"])), pose=pose,
        body_width=.6, body_height=height, on_ground=bool(pre["on_ground"]),
        horizontal_collision=bool(pre["horizontal_collision"]),
        vertical_collision=bool(pre["vertical_collision"]),
        sprinting=bool(pre["actual_sprinting"]),
        sneaking=bool(pre["actual_sneaking"]), jumping_cooldown_ticks=0,
        fall_distance_blocks=0., movement_speed_attribute=.1,
        step_height_blocks=.6, gravity_attribute=.08,
        jump_strength_attribute=.42, food_points=20, saturation_points=5.,
        game_mode="survival", status_effects=(), swimming=False,
        submerged_in_water=False, climbing=False, fall_flying=False,
        flying=False, allow_flying=False, is_using_item=False,
    )


def input_from_tick_evidence(row: Mapping) -> TickInput:
    item = row["actual_input"]
    return TickInput(
        float(item["forward"]), float(item["strafe"]), bool(item["jump"]),
        bool(item["sneak"]), bool(item["sprint"]),
        math.radians(float(row["movement_yaw"])),
    )


def ordinary_flat_validation_world(
        rows: Iterable[Mapping], ruleset: PhysicsRuleset, *,
        low_ceiling: bool = False) -> PhysicsWorldView:
    """Build the declared flat fixture used by sealed B08 evidence."""
    rows = tuple(rows)
    if not rows:
        raise ValueError("physics evidence is empty")
    session = WorldSessionId("offline-ordinary-flat-validation")
    stamp = ObservationStamp(session, 0, 0, "declared-validation-fixture", 0)
    positions = [
        (float(state["position"]["x"]), float(state["position"]["y"]),
         float(state["position"]["z"]))
        for row in rows for state in (row["pre_state"], row["post_state"])
    ]
    min_x = math.floor(min(item[0] for item in positions)) - 3
    max_x = math.floor(max(item[0] for item in positions)) + 3
    min_z = math.floor(min(item[2] for item in positions)) - 3
    max_z = math.floor(max(item[2] for item in positions)) + 3
    floor_y_values = {math.floor(item[1] - .500001) for item in positions}
    if len(floor_y_values) != 1:
        raise ValueError("ordinary-flat fixture requires one player standing height")
    floor_y = floor_y_values.pop()
    knowledge = WorldKnowledge(session)
    cells = tuple(
        (x, y, z)
        for x in range(min_x, max_x + 1)
        for y in range(floor_y - 3, floor_y + 7)
        for z in range(min_z, max_z + 1)
    )
    knowledge.confirm_air(stamp, cells)
    blocks = {
        (x, floor_y, z): BlockGeometry.full_cube("minecraft:grass_block")
        for x in range(min_x, max_x + 1)
        for z in range(min_z, max_z + 1)
    }
    if low_ceiling:
        blocks.update({
            (x, floor_y + 2, z): BlockGeometry.full_cube("minecraft:grass_block")
            for x in range(min_x, max_x + 1)
            for z in range(min_z, max_z + 1)
        })
    knowledge.observe_blocks(stamp, blocks)
    return PhysicsWorldView(knowledge.view(), ruleset)


def declared_fixture_validation_world(
        path: Path, ruleset: PhysicsRuleset) -> PhysicsWorldView:
    """Build only cells explicitly declared by a Fabric fixture command log."""
    session = WorldSessionId("offline-declared-fixture-validation")
    stamp = ObservationStamp(session, 0, 0, "declared-validation-fixture", 0)
    knowledge = WorldKnowledge(session)
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        command = json.loads(line)
        positions = tuple(tuple(int(axis) for axis in item)
                          for item in command["positions"])
        material = command["material"]
        if material == "minecraft:air":
            knowledge.confirm_air(stamp, positions)
        else:
            knowledge.observe_blocks(stamp, {
                position: BlockGeometry.full_cube(material) for position in positions
            })
    return PhysicsWorldView(knowledge.view(), ruleset)


def _percentiles(values: list[float]) -> tuple[tuple[str, float], ...]:
    if not values:
        return (("p50", 0.), ("p95", 0.), ("p99", 0.), ("max", 0.))
    ordered = sorted(values)
    def nearest(percent: float) -> float:
        return ordered[max(0, math.ceil(percent * len(ordered)) - 1)]
    return (("p50", nearest(.50)), ("p95", nearest(.95)),
            ("p99", nearest(.99)), ("max", ordered[-1]))


def compare_tick_rows(rows: Iterable[Mapping], world: PhysicsWorldView,
                      ruleset: PhysicsRuleset) -> ValidationReport:
    """Compare independent one-tick evidence rows without feeding back predictions."""
    rows = tuple(rows)
    differences = []
    issues = []
    position_errors: list[float] = []
    velocity_errors: list[float] = []
    mismatch_rows = []
    for index, row in enumerate(rows):
        if (not isinstance(row, Mapping)
                or row.get("schema_version") != "mc2p.client-physics-tick.v1"):
            issues.append(f"row_{index}:invalid_schema")
            continue
        try:
            state = state_from_tick_evidence(row, world, ruleset)
            tick_input = input_from_tick_evidence(row)
            expected = row["post_state"]
        except (KeyError, TypeError, ValueError) as error:
            issues.append(f"row_{index}:invalid_fields:{type(error).__name__}")
            continue
        calculated = step(state, tick_input, world, ruleset)
        if calculated.status is not CalculationStatus.OK or calculated.next_state is None:
            detail = (calculated.invalid_reasons or calculated.unsupported_reasons
                      or tuple(str(cell) for cell in calculated.missing_cells))
            suffix = ",".join(detail) if detail else "unspecified"
            issues.append(
                f"row_{index}:calculation_{calculated.status.value}:{suffix}")
            continue
        actual = calculated.next_state
        expected_position = _vector(expected, "position")
        expected_velocity = _vector(expected, "velocity")
        position_error = tuple(abs(a - b) for a, b in zip(actual.position, expected_position))
        velocity_error = tuple(
            abs(a - b) for a, b in zip(actual.velocity_blocks_per_tick, expected_velocity))
        position_errors.extend(position_error)
        velocity_errors.extend(velocity_error)
        predicted_contacts = set(actual_event for actual_event in calculated.events
                                 if actual_event in _CONTACT_EVENTS)
        expected_contacts = set(row.get("contact_events", ()))
        contacts_match = predicted_contacts == expected_contacts
        sprint_match = actual.sprinting == bool(expected["actual_sprinting"])
        pose_match = actual.pose == str(expected["pose"])
        tick_id = int(row["movement_tick_id"])
        if not (contacts_match and sprint_match and pose_match):
            mismatch_rows.append(tick_id)
        differences.append(TickDifference(
            tick_id, position_error, velocity_error,
            contacts_match, sprint_match, pose_match,
        ))
    return ValidationReport(
        total_rows=len(rows), complete_rows=len(differences),
        incomplete_rows=len(rows) - len(differences), differences=tuple(differences),
        issues=tuple(issues), event_mismatch_rows=tuple(mismatch_rows),
        position_error_max=max(position_errors, default=0.),
        velocity_error_max=max(velocity_errors, default=0.),
        position_error_percentiles=_percentiles(position_errors),
        velocity_error_percentiles=_percentiles(velocity_errors),
        assumptions=(
            "ordinary_full_block_slipperiness_0.6",
            "base_movement_speed_attribute_0.1",
            "jumping_cooldown_0",
            "food_20_and_no_status_effects",
        ),
    )


def _boundary_differences(post: Mapping, pre: Mapping) -> tuple[str, ...]:
    try:
        differences = []
        for name in ("position", "velocity"):
            if _vector(post, name) != _vector(pre, name):
                differences.append(name)
        for name in (
                "on_ground", "horizontal_collision", "vertical_collision",
                "actual_sprinting", "actual_sneaking"):
            if bool(post[name]) != bool(pre[name]):
                differences.append(name)
        for name in ("yaw", "pitch"):
            if float(post[name]) != float(pre[name]):
                differences.append(name)
        if str(post["pose"]) != str(pre["pose"]):
            differences.append("pose")
        return tuple(differences)
    except (KeyError, TypeError, ValueError):
        return ("invalid_boundary_state",)


def sequence_evidence_rows(rows: Iterable[Mapping]) -> tuple[
        tuple[tuple[Mapping, ...], ...], tuple[tuple[int, tuple[str, ...]], ...]]:
    rows = tuple(rows)
    groups: list[list[Mapping]] = []
    breaks = []
    for index, row in enumerate(rows):
        differences = () if not groups else _boundary_differences(
            groups[-1][-1].get("post_state", {}), row.get("pre_state", {}))
        if groups:
            try:
                prior_tick = int(groups[-1][-1]["movement_tick_id"])
                current_tick = int(row["movement_tick_id"])
                if current_tick != prior_tick + 1:
                    differences = tuple(sorted(set(differences + ("movement_tick_id",))))
            except (KeyError, TypeError, ValueError):
                differences = tuple(sorted(set(differences + ("movement_tick_id",))))
        if not groups or differences:
            if groups:
                breaks.append((index, differences))
            groups.append([row])
        else:
            groups[-1].append(row)
    return tuple(tuple(group) for group in groups), tuple(breaks)


def compare_open_loop_rows(rows: Iterable[Mapping], world: PhysicsWorldView,
                           ruleset: PhysicsRuleset, *,
                           position_tolerance: float = 1e-3,
                           velocity_tolerance: float = 1e-4) -> OpenLoopValidationReport:
    """Run each uninterrupted evidence segment without observation feedback."""
    rows = tuple(rows)
    groups, sequence_breaks = sequence_evidence_rows(rows)
    issues = []
    divergences = []
    complete = 0
    ticks_compared = 0
    position_errors: list[float] = []
    velocity_errors: list[float] = []
    for sequence_index, group in enumerate(groups):
        first = group[0]
        if first.get("schema_version") != "mc2p.client-physics-tick.v1":
            issues.append(f"sequence_{sequence_index}:invalid_schema")
            continue
        try:
            current = state_from_tick_evidence(first, world, ruleset)
        except (KeyError, TypeError, ValueError) as error:
            issues.append(f"sequence_{sequence_index}:invalid_initial:{type(error).__name__}")
            continue
        sequence_complete = True
        first_bad = None
        for offset, row in enumerate(group):
            calculated = step(current, input_from_tick_evidence(row), world, ruleset)
            if calculated.status is not CalculationStatus.OK or calculated.next_state is None:
                issues.append(
                    f"sequence_{sequence_index}:tick_{offset}:{calculated.status.value}")
                sequence_complete = False
                break
            current = calculated.next_state
            expected = row["post_state"]
            position_error = tuple(
                abs(a - b) for a, b in zip(current.position, _vector(expected, "position")))
            velocity_error = tuple(
                abs(a - b) for a, b in zip(
                    current.velocity_blocks_per_tick, _vector(expected, "velocity")))
            position_errors.extend(position_error)
            velocity_errors.extend(velocity_error)
            ticks_compared += 1
            predicted_contacts = {
                event for event in calculated.events if event in _CONTACT_EVENTS
            }
            discrete_match = (
                predicted_contacts == set(row.get("contact_events", ()))
                and current.sprinting == bool(expected["actual_sprinting"])
                and current.pose == str(expected["pose"])
            )
            if (first_bad is None and (
                    max(position_error) > position_tolerance
                    or max(velocity_error) > velocity_tolerance
                    or not discrete_match)):
                first_bad = offset
        if sequence_complete:
            complete += 1
        if first_bad is not None:
            divergences.append((sequence_index, first_bad))
    return OpenLoopValidationReport(
        sequence_count=len(groups), complete_sequences=complete,
        ticks_compared=ticks_compared, issues=tuple(issues),
        first_divergence=tuple(divergences),
        position_error_max=max(position_errors, default=0.),
        velocity_error_max=max(velocity_errors, default=0.),
        position_error_percentiles=_percentiles(position_errors),
        velocity_error_percentiles=_percentiles(velocity_errors),
        position_tolerance=position_tolerance,
        velocity_tolerance=velocity_tolerance,
        sequence_breaks=sequence_breaks,
    )
