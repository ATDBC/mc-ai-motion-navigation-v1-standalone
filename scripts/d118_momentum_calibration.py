"""Launch and seal the D118-0 fixed-input Windows Fabric calibration."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import platform
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
_FROZEN_MANIFESTS = {
    "23379a353a87e914c8d61490ee58f866a4ceadb69740c97b371c1a2dc8a83b6f": (7, 273),
    "4b6866f13abb92ff3f4fa3315d6e763620e85d42043387744ea5ac526c833c95": (84, 3276),
}
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.control_probe_core import write_json_atomic
from mc2p.motion_nav.evidence.physics_validation import (
    compare_open_loop_rows,
    compare_tick_rows,
    declared_fixture_validation_world,
    input_from_tick_evidence,
    state_from_tick_evidence,
)
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_types import (
    CalculationStatus,
    JAVA_1_21_RULESET,
)
from mc2p.runtime.segmented_trace import iter_segmented_jsonl


def fabric_probe_command(
    *, server_port: int, ipc_port: int, full_matrix: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts/probe_fabric_deployment_observation.py"),
        "--seed", "21001",
        "--server-port", str(server_port),
        "--ipc-port", str(ipc_port),
        "--d118-motion-calibration-probe",
        "--time-diagnostics",
        "--physics-tick-diagnostics",
        "--ftick-diagnostics",
        "--timeout-seconds", "600",
    ]
    if full_matrix:
        command.append("--d118-full-matrix")
    return command


def _run_directory(stdout: str) -> Path | None:
    prefix = "FABRIC_DEPLOYMENT_RUN_DIR="
    matches = [line[len(prefix):].strip() for line in stdout.splitlines()
               if line.startswith(prefix)]
    return Path(matches[0]) if len(matches) == 1 else None


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError(f"D118 evidence is not an object: {path.name}")
    return value


def _jsonl(path: Path) -> tuple[dict, ...]:
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            value = json.loads(line)
            if type(value) is not dict:
                raise ValueError(
                    f"D118 evidence row is not an object: {path.name}:{number}"
                )
            rows.append(value)
    return tuple(rows)


def validate_receipts_against_manifest(manifest, receipts: Iterable[Mapping]) -> None:
    """Bind every recorded command to the immutable pre-run manifest."""
    expected = {
        (trial.trial_id, index): (trial, command)
        for trial in manifest.trials
        for index, command in enumerate(trial.commands)
    }
    receipts = tuple(receipts)
    actual_keys = tuple(
        (row.get("trial_id"), row.get("command_index")) for row in receipts
    )
    if any(type(trial_id) is not str or type(index) is not int
           for trial_id, index in actual_keys):
        raise ValueError("D118 Runtime receipt identity has invalid types")
    if len(receipts) != len(expected) or set(actual_keys) != set(expected):
        raise ValueError("D118 Runtime receipt coverage differs from frozen manifest")
    if len(set(actual_keys)) != len(actual_keys):
        raise ValueError("D118 Runtime receipt identity is duplicate")
    movement_keys = {"forward", "strafe", "jump", "sneak", "sprint"}
    for row in receipts:
        trial, command = expected[(row["trial_id"], row["command_index"])]
        expected_movement = row.get("expected_movement")
        applied_movement = row.get("applied_movement")
        if (type(expected_movement) is not dict
                or type(applied_movement) is not dict
                or set(expected_movement) != movement_keys
                or set(applied_movement) != movement_keys):
            raise ValueError("D118 Runtime movement key set differs")
        frozen_movement = {
            "forward": command.forward,
            "strafe": command.strafe,
            "jump": command.jump,
            "sneak": command.sneak,
            "sprint": command.sprint,
        }
        if expected_movement != frozen_movement:
            raise ValueError("D118 Runtime expected movement differs from frozen command")
        if (row.get("timing_kind") != trial.timing_kind.value
                or row.get("stream_id") != command.stream_id
                or row.get("phase") != command.phase):
            raise ValueError("D118 Runtime metadata differs from frozen command")


def analyze_timeline_identity(run_dir: Path) -> tuple[dict, ...]:
    """Join each Runtime receipt to real physics and F-TICK sidecar rows."""
    from scripts.d118_momentum_calibration_runtime import join_command_timeline

    source = Path(run_dir).resolve()
    client = source / "client-0"
    result = _json_object(source / "result.json")
    manifest = _json_object(client / "d118-input-manifest.json")
    runtime = _json_object(client / "d118-calibration-runtime.json")
    if result.get("status") != "passed":
        raise ValueError("D118 probe result is not passed")
    if runtime.get("status") != "recorded":
        raise ValueError("D118 runtime result is not recorded")
    frozen_manifest = validate_manifest_document(manifest)
    manifest_hash = frozen_manifest.sha256
    if (type(manifest_hash) is not str or len(manifest_hash) != 64
            or runtime.get("manifest_sha256") != manifest_hash):
        raise ValueError("D118 manifest identity differs")
    expected_shape = _FROZEN_MANIFESTS.get(manifest_hash)
    trials = manifest.get("trials")
    if type(trials) is not list or expected_shape is None:
        raise ValueError("D118 manifest is not a frozen smoke or full matrix")
    command_count = sum(
        len(trial.get("commands", ())) if type(trial) is dict
        and type(trial.get("commands")) is list else -1
        for trial in trials
    )
    if (len(trials), command_count) != expected_shape:
        raise ValueError("D118 manifest shape differs from its frozen hash")

    receipts = _jsonl(client / "d118-runtime-receipts.jsonl")
    if (len(receipts) != expected_shape[1]
            or runtime.get("trial_count") != expected_shape[0]
            or runtime.get("command_count") != expected_shape[1]):
        raise ValueError("D118 Runtime receipt coverage is incomplete")
    validate_receipts_against_manifest(
        frozen_manifest, receipts,
    )
    keys = tuple((row.get("trial_id"), row.get("command_index"))
                 for row in receipts)
    if (any(type(trial) is not str or type(index) is not int
            for trial, index in keys) or len(set(keys)) != len(keys)):
        raise ValueError("D118 Runtime command identity is invalid or duplicate")
    physics_rows = tuple(iter_segmented_jsonl(
        client / "physics-tick-events",
    ))
    ftick_stream = tuple(iter_segmented_jsonl(client / "ftick-events"))
    summaries = tuple(
        row for row in ftick_stream
        if row.get("schema_version") == "mc2p.client-ftick-summary.v1"
        and row.get("kind") == "SUMMARY"
    )
    ftick_rows = tuple(
        row for row in ftick_stream
        if row.get("schema_version") == "mc2p.client-ftick-event.v1"
    )
    if (len(summaries) != 1
            or summaries[0].get("dropped_event_count") != 0
            or summaries[0].get("event_count") != len(ftick_rows)):
        raise ValueError("D118 F-TICK sidecar is incomplete or dropped events")
    return tuple(
        join_command_timeline(receipt, physics_rows, ftick_rows)
        for receipt in receipts
    )


def _manifest_from_document(document: Mapping):
    from scripts.d118_momentum_calibration_runtime import (
        CalibrationManifest,
        CalibrationTrial,
        FixedCommand,
        TimingKind,
    )

    trials = []
    for item in document.get("trials", ()):
        commands = tuple(FixedCommand(**command) for command in item["commands"])
        trials.append(CalibrationTrial(
            trial_id=item["trial_id"], family=item["family"],
            direction_index=item["direction_index"],
            repetition=item["repetition"],
            timing_kind=TimingKind(item["timing_kind"]),
            start_local=tuple(item["start_local"]),
            first_gap_cells=item["first_gap_cells"],
            middle_platform_cells=item["middle_platform_cells"],
            second_gap_cells=item["second_gap_cells"],
            commands=commands, fallback=item.get("fallback"),
        ))
    return CalibrationManifest(
        str(document["schema_version"]), tuple(trials), str(document["sha256"]),
    )


def validate_manifest_document(document: Mapping):
    from scripts.d118_momentum_calibration_runtime import manifest_sha256

    manifest = _manifest_from_document(document)
    calculated = manifest_sha256(manifest)
    if calculated != manifest.sha256:
        raise ValueError("D118 manifest content hash differs from declared sha256")
    expected_shape = _FROZEN_MANIFESTS.get(calculated)
    shape = (
        len(manifest.trials),
        sum(len(trial.commands) for trial in manifest.trials),
    )
    if expected_shape != shape:
        raise ValueError("D118 manifest is not a frozen smoke or full matrix")
    return manifest


@dataclass(frozen=True, slots=True)
class DeclaredTrialFixture:
    world: object
    origin: tuple[int, int]
    feet_y: int
    platform_x_bounds: tuple[float, float]
    platform_z_bounds: tuple[float, float]


def declared_trial_fixture_contexts(
    manifest, fixture_rows: Iterable[Mapping],
) -> dict[str, DeclaredTrialFixture]:
    """Rebuild each trial and its platform bounds from declared fixture rows."""
    from scripts.d118_momentum_calibration_runtime import trial_fixture_rows

    rows = tuple(fixture_rows)
    trial_ids = {trial.trial_id for trial in manifest.trials}
    expected_keys = {
        "schema_version", "trial_id", "phase", "origin", "feet_y",
        "material", "positions",
    }
    if any(
            not isinstance(row, Mapping)
            or set(row) != expected_keys
            or row.get("schema_version") != "mc2p.d118-fixture-command.v1"
            or row.get("trial_id") not in trial_ids
            or row.get("phase") not in {"clear", "support"}
            or type(row.get("origin")) is not list
            or len(row["origin"]) != 2
            or any(type(value) is not int for value in row["origin"])
            or type(row.get("feet_y")) is not int
            or type(row.get("positions")) is not list
            for row in rows):
        raise ValueError("fixture_identity is missing or invalid")
    contexts = {}
    with TemporaryDirectory(prefix="mc2p-d118-fixture-") as directory:
        root = Path(directory)
        for trial in manifest.trials:
            selected = tuple(
                row for row in rows if row["trial_id"] == trial.trial_id
            )
            if ({row["phase"] for row in selected} != {"clear", "support"}
                    or len(selected) != 2):
                raise ValueError(
                    f"fixture_identity for {trial.trial_id} is incomplete"
                )
            by_phase = {row["phase"]: row for row in selected}
            if (by_phase["clear"]["material"] != "minecraft:air"
                    or by_phase["support"]["material"]
                    != "minecraft:grass_block"
                    or any(
                        type(position) is not list or len(position) != 3
                        or any(type(axis) is not int for axis in position)
                        for row in selected for position in row["positions"]
                    )):
                raise ValueError(
                    f"fixture_identity for {trial.trial_id} has invalid cells"
                )
            origins = {tuple(row["origin"]) for row in selected}
            feet_values = {row["feet_y"] for row in selected}
            if (len(origins) != 1 or len(feet_values) != 1
                    or any(len(origin) != 2 for origin in origins)
                    or any(type(value) is not int
                           for origin in origins for value in origin)
                    or any(type(value) is not int for value in feet_values)):
                raise ValueError(
                    f"fixture_identity for {trial.trial_id} has no fixed origin"
                )
            origin = next(iter(origins))
            feet_y = next(iter(feet_values))
            expected_fixture = {
                row["phase"]: row for row in trial_fixture_rows(
                    trial, origin=origin, feet_y=feet_y,
                )
            }
            if any(by_phase[phase] != expected_fixture[phase]
                   for phase in ("clear", "support")):
                raise ValueError(
                    f"fixture_identity for {trial.trial_id} differs from frozen trial"
                )
            path = root / f"{trial.trial_id}.jsonl"
            path.write_text(
                "".join(json.dumps({
                    "material": row["material"],
                    "positions": row["positions"],
                }, sort_keys=True) + "\n" for row in selected),
                encoding="utf-8", newline="\n",
            )
            world = declared_fixture_validation_world(
                path, JAVA_1_21_RULESET,
            )
            support = by_phase["support"]
            dx, dz = ((0, 1), (1, 0), (0, -1), (-1, 0))[
                trial.direction_index
            ]
            lateral_x, lateral_z = dz, -dx
            middle_low = float(trial.first_gap_cells)
            middle_high = middle_low + float(trial.middle_platform_cells)
            platform_cells = []
            for position in support["positions"]:
                if len(position) != 3 or int(position[1]) != feet_y - 1:
                    continue
                relative_x = float(position[0]) + 0.5 - origin[0]
                relative_z = float(position[2]) + 0.5 - origin[1]
                local_x = relative_x * lateral_x + relative_z * lateral_z
                local_z = relative_x * dx + relative_z * dz
                if middle_low <= local_z < middle_high:
                    platform_cells.append((local_x, local_z))
            if not platform_cells:
                raise ValueError(
                    f"fixture_identity for {trial.trial_id} has no middle platform"
                )
            contexts[trial.trial_id] = DeclaredTrialFixture(
                world=world, origin=origin, feet_y=feet_y,
                platform_x_bounds=(
                    min(value[0] for value in platform_cells) - 0.5,
                    max(value[0] for value in platform_cells) + 0.5,
                ),
                platform_z_bounds=(
                    min(value[1] for value in platform_cells) - 0.5,
                    max(value[1] for value in platform_cells) + 0.5,
                ),
            )
    return contexts


def declared_trial_fixture_worlds(manifest, fixture_rows: Iterable[Mapping]) -> dict:
    """Compatibility view of the per-trial declared fixture worlds."""
    return {
        trial_id: context.world
        for trial_id, context in declared_trial_fixture_contexts(
            manifest, fixture_rows,
        ).items()
    }


def _state_vector(state: Mapping, name: str) -> tuple[float, float, float]:
    value = state[name]
    return float(value["x"]), float(value["y"]), float(value["z"])


def _angle_error_degrees(actual_radians: float, expected_degrees: float) -> float:
    actual = math.degrees(actual_radians)
    return abs((actual - expected_degrees + 180.0) % 360.0 - 180.0)


def _discrete_mismatches(predicted, expected: Mapping,
                         events: Iterable[str], row: Mapping) -> tuple[str, ...]:
    expected_events = frozenset(row.get("contact_events", ()))
    actual_events = frozenset(
        event for event in events if event in {
            "left_ground", "landed", "horizontal_collision", "vertical_collision",
        }
    )
    mismatches = []
    checks = (
        ("on_ground", predicted.on_ground, bool(expected["on_ground"])),
        ("horizontal_collision", predicted.horizontal_collision,
         bool(expected["horizontal_collision"])),
        ("vertical_collision", predicted.vertical_collision,
         bool(expected["vertical_collision"])),
        ("actual_sprinting", predicted.sprinting,
         bool(expected["actual_sprinting"])),
        ("actual_sneaking", predicted.sneaking,
         bool(expected["actual_sneaking"])),
        ("pose", predicted.pose, str(expected["pose"])),
        ("contact_events", actual_events, expected_events),
    )
    for name, actual, wanted in checks:
        if actual != wanted:
            mismatches.append(name)
    return tuple(mismatches)


def _event_timing(trial, rows: tuple[Mapping, ...]) -> dict:
    landings = tuple(
        int(row["movement_tick_id"]) for row in rows
        if "landed" in row.get("contact_events", ())
    )
    takeoffs = tuple(
        int(row["movement_tick_id"]) for row in rows
        if "left_ground" in row.get("contact_events", ())
    )
    second_jump = tuple(
        int(rows[index]["movement_tick_id"])
        for index, command in enumerate(trial.commands)
        if command.stream_id == "jump-2" and command.jump
    )
    sneak = tuple(
        int(row["movement_tick_id"]) for row in rows
        if bool(row.get("actual_input", {}).get("sneak"))
    )
    landing = landings[0] if landings else None
    return {
        "takeoff_ticks": list(takeoffs),
        "landing_ticks": list(landings),
        "first_landing_tick": landing,
        "second_jump_applied_tick": second_jump[0] if second_jump else None,
        "second_jump_relative_to_landing": (
            None if landing is None or not second_jump else second_jump[0] - landing
        ),
        "sneak_applied_tick": sneak[0] if sneak else None,
        "sneak_relative_to_landing": (
            None if landing is None or not sneak else sneak[0] - landing
        ),
    }


def _fallback_timing_failures(trial, timing: Mapping) -> tuple[str, ...]:
    if trial.fallback != "sneak":
        return ()
    failures = []
    landings = timing.get("landing_ticks")
    if type(landings) is not list or len(landings) != 1:
        failures.append("landing_count")
    if timing.get("sneak_applied_tick") is None:
        failures.append("application_missing")
    relative = timing.get("sneak_relative_to_landing")
    if relative is not None and (
            type(relative) is not int or relative < 0 or relative > 1):
        failures.append("application_outside_window")
    return tuple(failures)


def _fallback_margin(
    trial, rows: tuple[Mapping, ...], context: DeclaredTrialFixture,
) -> dict:
    if trial.fallback is None:
        return {"status": "not_applicable"}
    if not rows:
        return {"status": "unverified", "reason": "no_physics_rows"}
    final_state = rows[-1]["post_state"]
    final = _state_vector(final_state, "position")
    dx, dz = ((0, 1), (1, 0), (0, -1), (-1, 0))[trial.direction_index]
    lateral_x, lateral_z = dz, -dx
    relative_x = final[0] - context.origin[0]
    relative_z = final[2] - context.origin[1]
    final_local_x = relative_x * lateral_x + relative_z * lateral_z
    final_local_z = relative_x * dx + relative_z * dz
    low_x, high_x = context.platform_x_bounds
    low_z, high_z = context.platform_z_bounds
    # Vanilla edge support only needs some horizontal footprint overlap.
    # A 0.6-wide body therefore permits its center 0.3 blocks past an edge.
    longitudinal_margin = min(
        final_local_z - (low_z - 0.3),
        (high_z + 0.3) - final_local_z,
    )
    lateral_margin = min(
        final_local_x - (low_x - 0.3),
        (high_x + 0.3) - final_local_x,
    )
    velocity = _state_vector(final_state, "velocity")
    settled = (
        bool(final_state["on_ground"])
        and math.hypot(velocity[0], velocity[2]) < 0.003
    )
    vertical_match = abs(final[1] - float(context.feet_y)) <= 0.001
    minimum_margin = min(longitudinal_margin, lateral_margin)
    safe = settled and vertical_match and minimum_margin >= 0.0
    return {
        "status": "measured",
        "fallback": trial.fallback,
        "safe": safe,
        "settled": settled,
        "vertical_match": vertical_match,
        "final_longitudinal_margin_blocks": longitudinal_margin,
        "final_lateral_margin_blocks": lateral_margin,
        "minimum_platform_overlap_margin_blocks": minimum_margin,
    }


def analyze_physics_replay(
    manifest,
    receipts: Iterable[Mapping],
    physics_rows: Iterable[Mapping],
    fixture_rows: Iterable[Mapping],
) -> dict:
    """Replay each trial from its observed pre-state in its own declared world."""
    receipts = tuple(receipts)
    physics_rows = tuple(physics_rows)
    try:
        contexts = declared_trial_fixture_contexts(manifest, fixture_rows)
    except (KeyError, TypeError, ValueError) as error:
        return {
            "schema_version": "mc2p.d118-physics-replay.v1",
            "status": "unverified", "trial_count": 0, "tick_count": 0,
            "issues": [f"fixture_identity:{error}"],
            "entry_margin": {
                "status": "unverified",
                "reason": "no_sound_entry_envelope_is_frozen",
            },
        }
    receipt_by_trial = {}
    for receipt in receipts:
        receipt_by_trial.setdefault(receipt.get("trial_id"), []).append(receipt)
    physics_by_identity = {}
    for row in physics_rows:
        key = (
            row.get("episode_id"), row.get("request_sequence_id"),
            row.get("movement_tick_id"),
        )
        physics_by_identity.setdefault(key, []).append(row)
    evidence_issues = []
    failures = []
    per_trial = []
    max_position = [0.0, 0.0, 0.0]
    max_velocity = [0.0, 0.0, 0.0]
    max_yaw_error = 0.0
    max_pitch_error = 0.0
    discrete_count = 0
    tick_count = 0
    for trial in manifest.trials:
        selected_receipts = sorted(
            receipt_by_trial.get(trial.trial_id, ()),
            key=lambda row: row.get("command_index", -1),
        )
        if (len(selected_receipts) != len(trial.commands)
                or [row.get("command_index") for row in selected_receipts]
                != list(range(len(trial.commands)))):
            evidence_issues.append(f"{trial.trial_id}:receipt_coverage")
            continue
        rows = []
        for receipt in selected_receipts:
            key = (
                receipt.get("episode_id"), receipt.get("request_sequence_id"),
                receipt.get("movement_tick_id"),
            )
            candidates = physics_by_identity.get(key, ())
            if len(candidates) != 1:
                evidence_issues.append(f"{trial.trial_id}:physics_identity")
                rows = []
                break
            rows.append(candidates[0])
        if not rows:
            continue
        rows = tuple(rows)
        context = contexts[trial.trial_id]
        world = context.world
        tick_report = compare_tick_rows(rows, world, JAVA_1_21_RULESET)
        open_report = compare_open_loop_rows(
            rows, world, JAVA_1_21_RULESET,
            position_tolerance=0.001, velocity_tolerance=0.0001,
        )
        if (tick_report.incomplete_rows or tick_report.issues
                or open_report.complete_sequences != open_report.sequence_count
                or open_report.issues):
            evidence_issues.append(
                f"{trial.trial_id}:physics_replay_incomplete"
            )
            continue
        current = state_from_tick_evidence(
            rows[0], world, JAVA_1_21_RULESET,
        )
        trial_mismatches = []
        for offset, row in enumerate(rows):
            calculated = physics_step(
                current, input_from_tick_evidence(row), world, JAVA_1_21_RULESET,
            )
            if (calculated.status is not CalculationStatus.OK
                    or calculated.next_state is None):
                evidence_issues.append(
                    f"{trial.trial_id}:tick_{offset}:{calculated.status.value}"
                )
                break
            current = calculated.next_state
            expected = row["post_state"]
            position_error = tuple(
                abs(a - b) for a, b in zip(
                    current.position, _state_vector(expected, "position"),
                )
            )
            velocity_error = tuple(
                abs(a - b) for a, b in zip(
                    current.velocity_blocks_per_tick,
                    _state_vector(expected, "velocity"),
                )
            )
            for axis in range(3):
                max_position[axis] = max(max_position[axis], position_error[axis])
                max_velocity[axis] = max(max_velocity[axis], velocity_error[axis])
            max_yaw_error = max(
                max_yaw_error,
                _angle_error_degrees(current.yaw_radians, float(expected["yaw"])),
            )
            max_pitch_error = max(
                max_pitch_error,
                abs(math.degrees(current.pitch_radians) - float(expected["pitch"])),
            )
            mismatches = _discrete_mismatches(
                current, expected, calculated.events, row,
            )
            if mismatches:
                trial_mismatches.append({
                    "movement_tick_id": row["movement_tick_id"],
                    "fields": list(mismatches),
                })
                discrete_count += 1
            tick_count += 1
        timing = _event_timing(trial, rows)
        fallback = _fallback_margin(trial, rows, context)
        expected_safe = trial.fallback == "sneak"
        if (fallback.get("status") == "measured"
                and fallback.get("safe") != expected_safe):
            failures.append(f"{trial.trial_id}:fallback_classification")
        failures.extend(
            f"{trial.trial_id}:sneak_fallback_{reason}"
            for reason in _fallback_timing_failures(trial, timing)
        )
        per_trial.append({
            "trial_id": trial.trial_id,
            "tick_count": len(rows),
            "one_tick_complete": tick_report.complete_rows,
            "open_loop_complete_sequences": open_report.complete_sequences,
            "open_loop_sequence_count": open_report.sequence_count,
            "event_timing": timing,
            "fallback_margin": fallback,
            "discrete_mismatches": trial_mismatches,
        })
    if any(value > 0.001 for value in max_position):
        failures.append("position_tolerance_exceeded")
    if any(value > 0.0001 for value in max_velocity):
        failures.append("velocity_tolerance_exceeded")
    if discrete_count:
        failures.append("discrete_state_mismatch")
    status = (
        "failed" if failures else "unverified" if evidence_issues else "passed"
    )
    return {
        "schema_version": "mc2p.d118-physics-replay.v1",
        "status": status,
        "trial_count": len(per_trial),
        "tick_count": tick_count,
        "position_error_max_by_axis": max_position,
        "velocity_error_max_by_axis": max_velocity,
        "yaw_error_max_degrees": max_yaw_error,
        "pitch_error_max_degrees": max_pitch_error,
        "position_tolerance_by_axis": 0.001,
        "velocity_tolerance_by_axis": 0.0001,
        "discrete_mismatch_count": discrete_count,
        "issues": [*failures, *evidence_issues],
        "entry_margin": {
            "status": "unverified",
            "reason": "no_sound_entry_envelope_is_frozen",
        },
        "trials": per_trial,
    }


def analyze_physics_from_run(run_dir: Path) -> dict:
    source = Path(run_dir).resolve()
    client = source / "client-0"
    try:
        manifest = _manifest_from_document(_json_object(
            client / "d118-input-manifest.json",
        ))
        receipts = _jsonl(client / "d118-runtime-receipts.jsonl")
        fixtures = _jsonl(client / "d118-fixture-commands.jsonl")
        physics_rows = tuple(iter_segmented_jsonl(
            client / "physics-tick-events",
        ))
        return analyze_physics_replay(
            manifest, receipts, physics_rows, fixtures,
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {
            "schema_version": "mc2p.d118-physics-replay.v1",
            "status": "unverified", "trial_count": 0, "tick_count": 0,
            "issues": [f"evidence_incomplete:{type(error).__name__}:{error}"],
            "entry_margin": {
                "status": "unverified",
                "reason": "no_sound_entry_envelope_is_frozen",
            },
        }


def seal_run(
    output: Path,
    run_dir: Path | None,
    *,
    command: list[str],
    return_code: int,
    stdout: str,
    stderr: str,
) -> dict:
    target = Path(output).resolve()
    target.mkdir(parents=True, exist_ok=False)
    source = None if run_dir is None else Path(run_dir).resolve()
    required = () if source is None else (
        source / "result.json",
        source / "client-0/d118-input-manifest.json",
        source / "client-0/d118-calibration-runtime.json",
        source / "client-0/d118-runtime-receipts.jsonl",
        source / "client-0/d118-trials.jsonl",
        source / "client-0/d118-fixture-commands.jsonl",
        source / "client-0/d118-teleports.jsonl",
        source / "client-0/physics-tick-events/complete.json",
        source / "client-0/ftick-events/complete.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    raw_candidates = () if source is None else (
        *required,
        *(source / "client-0/physics-tick-events").glob("manifest.jsonl"),
        *(source / "client-0/physics-tick-events").glob("segment-*.jsonl"),
        *(source / "client-0/ftick-events").glob("manifest.jsonl"),
        *(source / "client-0/ftick-events").glob("segment-*.jsonl"),
    )
    raw = {
        path.relative_to(source).as_posix(): {
            "sha256": _sha256(path), "bytes": path.stat().st_size,
        }
        for path in raw_candidates if path.is_file()
    } if source is not None else {}
    timelines: tuple[dict, ...] = ()
    timeline_error = None
    physics_report = {
        "schema_version": "mc2p.d118-physics-replay.v1",
        "status": "unverified", "trial_count": 0, "tick_count": 0,
        "issues": ["source_run_unavailable"],
        "entry_margin": {
            "status": "unverified",
            "reason": "no_sound_entry_envelope_is_frozen",
        },
    }
    if return_code == 0 and source is not None and not missing:
        try:
            timelines = analyze_timeline_identity(source)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            timeline_error = f"{type(error).__name__}: {error}"
        if timeline_error is None:
            physics_report = analyze_physics_from_run(source)
    capture_status = (
        "captured"
        if return_code == 0 and source is not None and not missing
        and timeline_error is None else "failed"
    )
    acceptance_status = (
        "failed"
        if capture_status == "failed" or physics_report["status"] == "failed"
        else "passed"
        if (physics_report["status"] == "passed"
            and physics_report.get("entry_margin", {}).get("status") == "passed")
        else "unverified"
    )
    source_result_status = None
    if source is not None and (source / "result.json").is_file():
        try:
            source_result_status = _json_object(
                source / "result.json",
            ).get("status")
        except (OSError, ValueError, json.JSONDecodeError):
            source_result_status = "invalid"
    summary = {
        "schema_version": "mc2p.d118-calibration-launch.v1",
        "status": acceptance_status,
        "capture_status": capture_status,
        "acceptance_status": acceptance_status,
        "formal_platform": "Windows",
        "actual_platform": platform.platform(),
        "source_run": None if source is None else str(source),
        "source_result_status": source_result_status,
        "probe_return_code": return_code,
        "missing_required_files": missing,
        "raw_files": raw,
        "timeline_identity_status": (
            "passed" if timeline_error is None and timelines else "failed"
        ),
        "timeline_identity_error": timeline_error,
        "joined_command_count": len(timelines),
        "physics_replay_status": physics_report["status"],
        "physics_replay_issues": physics_report["issues"],
        "stage_status": "in_progress",
        "note": (
            "C3 replays every trial in its own declared fixture; the entry "
            "envelope remains typed unverified, so D118-0 is not signed"
        ),
    }
    write_json_atomic(target / "summary.json", summary)
    write_json_atomic(target / "physics-replay.json", physics_report)
    with (target / "timelines.jsonl").open(
            "x", encoding="utf-8", newline="\n") as stream:
        for row in timelines:
            stream.write(json.dumps(
                row, ensure_ascii=False, sort_keys=True,
            ) + "\n")
    (target / "command.json").write_text(
        json.dumps(command, ensure_ascii=False, indent=2) + "\n", "utf-8",
    )
    (target / "probe-stdout.txt").write_text(stdout, "utf-8")
    (target / "probe-stderr.txt").write_text(stderr, "utf-8")
    write_json_atomic(target / "index.json", {
        "schema_version": "mc2p.d118-calibration-index.v1",
        "status": acceptance_status,
        "capture_status": capture_status,
        "acceptance_status": acceptance_status,
        "raw_files": raw,
        "files": {
            name: {
                "sha256": _sha256(target / name),
                "bytes": (target / name).stat().st_size,
            }
            for name in (
                "summary.json", "command.json", "probe-stdout.txt",
                "probe-stderr.txt", "timelines.jsonl", "physics-replay.json",
            )
        },
    })
    return summary


def exit_code_for_summary(summary: Mapping) -> int:
    if summary.get("capture_status") == "captured":
        return 0 if summary.get("acceptance_status") == "passed" else 3
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--server-port", type=int, default=25597)
    parser.add_argument("--ipc-port", type=int, default=8140)
    parser.add_argument(
        "--full-matrix", action="store_true",
        help="run four directions with three repetitions; default is south smoke",
    )
    args = parser.parse_args(argv)
    command = fabric_probe_command(
        server_port=args.server_port, ipc_port=args.ipc_port,
        full_matrix=args.full_matrix,
    )
    stdout = stderr = ""
    return_code = 0
    run_dir = args.run_dir
    if run_dir is None:
        completed = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        stdout, stderr = completed.stdout, completed.stderr
        return_code = completed.returncode
        run_dir = _run_directory(stdout)
    summary = seal_run(
        args.output, run_dir, command=command, return_code=return_code,
        stdout=stdout, stderr=stderr,
    )
    print(json.dumps({
        "status": summary["status"],
        "capture_status": summary["capture_status"],
        "acceptance_status": summary["acceptance_status"],
        "output": str(args.output.resolve()),
        "source_run": summary["source_run"],
    }, ensure_ascii=False))
    return exit_code_for_summary(summary)


if __name__ == "__main__":
    raise SystemExit(main())
