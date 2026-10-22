"""D125-0 read-only observation-feasibility probe.

This module never starts Runtime, Minecraft, Fabric, or a movement trial.  It
uses the frozen D120 fixture, the current D124 proof dependencies, and the
same native visibility core used by actor observation diagnostics.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import math
import platform
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.landing_edge_probe import _corner_vantage
from scripts import d118_momentum_calibration_runtime as frozen
from scripts import d122_fabric_timing as d122
from scripts import d123_dependency_probe as dependency
from scripts.d120_momentum_entry_calibration import build_fixture


STOP_CODE = "PRECOMMIT_FACT_NOT_OBSERVABLE_FROM_SAFE_REACHABLE_VIEWPOINT"
VISIBLE_AIR_STATUSES = frozenset((1, 4))
EYE_HEIGHTS = (1.27, 1.62)
SAFE_SCAN_X = (-1.299, -.5, .5, 1.5, 2.299)
SAFE_SCAN_Z = (-2., -1., -.3, 0., .299)
SAFE_SCAN_YAW_OFFSETS = tuple(range(-90, 91, 15))
SAFE_SCAN_PITCHES = tuple(range(0, 91, 15))
WITNESS_YAW_OFFSETS = tuple(range(-90, 91, 3))
WITNESS_PITCHES = tuple(range(0, 91, 3))


def _validate_direction(direction: int) -> None:
    if type(direction) is not int or direction not in range(4):
        raise ValueError("D125 direction must be 0..3")


def _jsonable(value):
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(value), ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def required_fact_roles(direction: int) -> tuple[dict, ...]:
    """Classify current D124 lower dependencies by replaying formal proofs."""
    _validate_direction(direction)
    case = dependency.fixed_case(direction)
    manifest = d122.fabric_dependency_manifest(
        direction, origin=(0, 0), feet_y=64,
    )
    rows = []
    for position in sorted(p for p in manifest["facts"] if p[1] == 62):
        replay = dependency.replay(case, {position: dependency.UNKNOWN_CELL})
        if (position not in replay["dependencies"]
                or replay["offline_dependency_gate"] != "NEEDS_WORLD"):
            continue
        parent = replay["parent_status"] != "solved"
        successor = replay["successor_binding_status"] != "solved"
        rows.append({
            "position": position,
            "parent_required": parent,
            "successor_required": successor,
            "parent_status_when_unknown": replay["parent_status"],
            "successor_status_when_unknown": replay["successor_binding_status"],
        })
    if (len(rows) != 3
            or sum(row["parent_required"] for row in rows) != 2
            or sum(row["successor_required"] for row in rows) != 2
            or sum(row["parent_required"] and row["successor_required"]
                   for row in rows) != 1):
        raise ValueError("D125 current proof roles differ from D124")
    return tuple(rows)


def _fixture_support(direction: int, *, remove_middle: bool = False):
    fixture = build_fixture(direction)
    rows = frozen.trial_fixture_rows(
        fixture.trial, origin=(0, 0), feet_y=64,
    )
    support = {
        tuple(position)
        for row in rows if row["phase"] == "support"
        for position in row["positions"]
    }
    middle = set()
    for local_x in (-.5, .5, 1.5):
        world_x, world_z = frozen._local_to_world(
            (0, 0), direction, local_x, 1.5,
        )
        middle.add((math.floor(world_x), 63, math.floor(world_z)))
    if not middle.issubset(support):
        raise ValueError("D125 middle platform identity differs")
    if remove_middle:
        support.difference_update(middle)
    positions = tuple(sorted(support))
    data = {
        "boxes": [(*position, position[0] + 1, position[1] + 1,
                    position[2] + 1) for position in positions],
        "centers": [(position[0] + .5, position[1] + .5,
                     position[2] + .5) for position in positions],
        "owners": list(range(len(positions))),
        "opaque": [1] * len(positions),
    }
    return fixture, data, tuple(sorted(middle))


def _world_eye(direction: int, local_x: float, local_z: float,
               eye_height: float = 1.62) -> tuple[float, float, float]:
    x, z = frozen._local_to_world((0, 0), direction, local_x, local_z)
    return x, 64. + eye_height, z


def _visible(required, statuses) -> tuple[tuple[int, int, int], ...]:
    return tuple(position for position, status in zip(required, statuses)
                 if int(status) in VISIBLE_AIR_STATUSES)


def _find_witness(cache, direction: int, required, local_position,
                  expected) -> dict | None:
    eye = _world_eye(direction, *local_position)
    base_yaw = frozen._DIRECTIONS[direction][2]
    for yaw_offset in WITNESS_YAW_OFFSETS:
        for pitch in WITNESS_PITCHES:
            _, statuses, _, _ = cache.frame_pose_air(
                (*eye, base_yaw + yaw_offset, pitch), required,
            )
            visible = _visible(required, statuses)
            if expected(visible):
                return {
                    "local_body_position": (local_position[0], 64., local_position[1]),
                    "eye": eye,
                    "yaw_degrees": base_yaw + yaw_offset,
                    "pitch_degrees": pitch,
                    "statuses": tuple(int(value) for value in statuses),
                    "visible_cells": visible,
                }
    return None


def _safe_scan(cache, direction: int, blocked_cell) -> dict:
    """Finite near-edge scan; the continuous domain is covered separately."""
    base_yaw = frozen._DIRECTIONS[direction][2]
    poses = visible_count = 0
    for local_x in SAFE_SCAN_X:
        for local_z in SAFE_SCAN_Z:
            for eye_height in EYE_HEIGHTS:
                eye = _world_eye(direction, local_x, local_z, eye_height)
                for yaw_offset in SAFE_SCAN_YAW_OFFSETS:
                    for pitch in SAFE_SCAN_PITCHES:
                        poses += 1
                        _, statuses, _, _ = cache.frame_pose_air(
                            (*eye, base_yaw + yaw_offset, pitch),
                            (blocked_cell,),
                        )
                        visible_count += int(
                            int(statuses[0]) in VISIBLE_AIR_STATUSES
                        )
    return {
        "scope": "finite_near_edge_scan_not_continuous_proof",
        "local_x": SAFE_SCAN_X,
        "local_z": SAFE_SCAN_Z,
        "eye_heights": EYE_HEIGHTS,
        "yaw_offsets": SAFE_SCAN_YAW_OFFSETS,
        "pitches": SAFE_SCAN_PITCHES,
        "pose_count": poses,
        "blocked_visible_count": visible_count,
    }


def _continuous_blocker(direction: int, blocked_cell, middle_cells) -> dict:
    """Keep the fixture-specific continuous argument separate from samples."""
    local_intervals = {
        "body_center_x_open": (-1.3, 2.3),
        "body_center_z_open": (-12.3, .3),
        "eye_y_minimum": 65.27,
        "target_cube": ((0., 62., 2.), (1., 63., 3.)),
        "start_platform": ((-1., 63., -12.), (2., 64., 0.)),
        "middle_platform": ((-1., 63., 1.), (2., 64., 2.)),
    }
    expected_middle = set()
    for local_x in (-.5, .5, 1.5):
        x, z = frozen._local_to_world((0, 0), direction, local_x, 1.5)
        expected_middle.add((math.floor(x), 63, math.floor(z)))
    roles = required_fact_roles(direction)
    joint = next(tuple(row["position"]) for row in roles
                 if row["parent_required"] and row["successor_required"])
    eye_x_min, eye_x_max = -1.3, 2.3
    eye_z_min, eye_z_max = -12.3, .3
    eye_y_min, eye_y_max = 65.27, 65.62
    target_x_min, target_x_max = 0., 1.
    target_y_min, target_y_max = 62., 63.
    target_z_min, target_z_max = 2., 3.

    # At the middle platform front (z=1), the line has already moved far
    # enough toward the target that it is strictly inside the three-wide
    # platform.  The same holds at z=2.
    t_z1_min = (1. - eye_z_max) / (target_z_max - eye_z_max)
    x_z1 = (
        (1. - t_z1_min) * eye_x_min + t_z1_min * target_x_min,
        (1. - t_z1_min) * eye_x_max + t_z1_min * target_x_max,
    )
    t_z2_min = (2. - eye_z_max) / (target_z_max - eye_z_max)
    x_z2 = (
        (1. - t_z2_min) * eye_x_min + t_z2_min * target_x_min,
        (1. - t_z2_min) * eye_x_max + t_z2_min * target_x_max,
    )
    y_z2_max = ((1. - t_z2_min) * eye_y_max
                + t_z2_min * target_y_max)

    # If a ray is already below the middle front at z=1, linearity with the
    # target gives y(z=0)<64.  It therefore crossed the start-platform top.
    # The crossing fraction is bounded independently of z and keeps x inside
    # the platform; even the back-edge eye reaches y=64 after z=-12.
    t_y64_values = tuple(
        (eye_y - 64.) / (eye_y - target_y)
        for eye_y in (eye_y_min, eye_y_max)
        for target_y in (target_y_min, target_y_max)
    )
    t_y64_min = min(t_y64_values)
    x_y64 = (
        (1. - t_y64_min) * eye_x_min + t_y64_min * target_x_min,
        (1. - t_y64_min) * eye_x_max + t_y64_min * target_x_max,
    )
    z_y64_min = ((1. - t_y64_min) * eye_z_min
                 + t_y64_min * target_z_min)
    inequalities = {
        "x_at_middle_front": x_z1,
        "x_at_middle_back": x_z2,
        "maximum_y_at_middle_back": y_z2_max,
        "x_at_start_top_crossing": x_y64,
        "minimum_z_at_start_top_crossing": z_y64_min,
        "checks": {
            "middle_front_x_inside": -1. < x_z1[0] < x_z1[1] < 2.,
            "middle_back_x_inside": -1. < x_z2[0] < x_z2[1] < 2.,
            "high_front_ray_crosses_top_by_z2": y_z2_max < 64.,
            "low_front_ray_crosses_start_laterally_inside": (
                -1. < x_y64[0] < x_y64[1] < 2.
            ),
            "low_front_ray_crosses_after_start_back": z_y64_min > -12.,
        },
    }
    proven = (tuple(blocked_cell) == joint
              and expected_middle == set(map(tuple, middle_cells))
              and all(inequalities["checks"].values()))
    return {
        "kind": "fixture_specific_continuous_occlusion_argument",
        "proven": proven,
        "rotation_preserves_argument": True,
        "local_intervals": local_intervals,
        "inequality_certificate": inequalities,
        "reason": (
            "at z=1 every ray is laterally inside the middle prism; rays in "
            "y=[63,64] hit its front, higher rays cross its top before z=2, "
            "and lower rays must already have crossed the start-platform top"
        ),
    }


@lru_cache(maxsize=4)
def visibility_evidence(direction: int) -> dict:
    _validate_direction(direction)
    from scripts.surface_depth_probe.native import Cache

    roles = required_fact_roles(direction)
    required = tuple(tuple(row["position"]) for row in roles)
    blocked = next(tuple(row["position"]) for row in roles
                   if row["parent_required"] and row["successor_required"])
    _, data, middle_cells = _fixture_support(direction)
    expected_staging = set(required) - {blocked}
    with Cache() as cache:
        cache.update(data)
        staging = _find_witness(
            cache, direction, required, (.5, -.5),
            lambda visible: set(visible) == expected_staging,
        )
        middle = _find_witness(
            cache, direction, required, (.5, 1.5),
            lambda visible: set(visible) == set(required),
        )
        safe_scan = _safe_scan(cache, direction, blocked)
    if staging is None or middle is None:
        raise ValueError("D125 required visibility witness was not found")

    _, without_middle, _ = _fixture_support(direction, remove_middle=True)
    with Cache() as cache:
        cache.update(without_middle)
        mutation = _find_witness(
            cache, direction, (blocked,), (.5, -.5),
            lambda visible: visible == (blocked,),
        )
    if mutation is None:
        raise ValueError("D125 platform-removal mutation did not expose fact")
    return {
        "direction": direction,
        "required_cells": required,
        "blocked_parent_cell": blocked,
        "staging_witness": staging,
        "safe_scan": safe_scan,
        "continuous_blocker": _continuous_blocker(
            direction, blocked, middle_cells,
        ),
        "middle_witness": middle,
        "middle_platform_removed_mutation": {
            "blocked_visible": True,
            "witness": mutation,
        },
    }


@lru_cache(maxsize=4)
def formal_blocked_fact_gate(direction: int) -> dict:
    roles = required_fact_roles(direction)
    blocked = next(tuple(row["position"]) for row in roles
                   if row["parent_required"] and row["successor_required"])
    replay = dependency.replay(
        dependency.fixed_case(direction), {blocked: dependency.UNKNOWN_CELL},
    )
    return {
        "blocked_parent_cell": blocked,
        "parent_status": replay["parent_status"],
        "successor_binding_status": replay["successor_binding_status"],
        "offline_dependency_gate": replay["offline_dependency_gate"],
        "gate_unknown": tuple(replay["gate_unknown"]),
        "unknown_to_air_inference_used": False,
    }


@lru_cache(maxsize=4)
def landing_probe_applicability(direction: int) -> dict:
    roles = required_fact_roles(direction)
    blocked = next(tuple(row["position"]) for row in roles
                   if row["parent_required"] and row["successor_required"])
    body_x, body_z = frozen._local_to_world((0, 0), direction, .5, -.5)
    vantage = _corner_vantage((body_x, 64., body_z), blocked)
    _, _, middle_cells = _fixture_support(direction)
    owner = (math.floor(vantage[0]), 63, math.floor(vantage[1]))
    on_middle = owner in set(middle_cells)
    return {
        "blocked_parent_cell": blocked,
        "start_body_position": (body_x, 64., body_z),
        "landing_probe_vantage": vantage,
        "vantage_support_owner": owner,
        "vantage_on_middle_platform": on_middle,
        "applicable_from_start_support": not on_middle,
        "can_authorize_precommit": False,
        "reason": "single landing-cell vantage is across the first gap",
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(output: Path) -> int:
    """Write append-only PARTIAL/STOPPED evidence for D125-0."""
    output = Path(output)
    if output.exists():
        raise FileExistsError("D125 evidence is append-only")
    output.mkdir(parents=True)
    directions = []
    for direction in range(4):
        directions.append({
            "direction": direction,
            "fact_roles": required_fact_roles(direction),
            "visibility": visibility_evidence(direction),
            "formal_gate": formal_blocked_fact_gate(direction),
            "landing_probe": landing_probe_applicability(direction),
        })
    stopped = all(
        row["visibility"]["safe_scan"]["blocked_visible_count"] == 0
        and row["visibility"]["continuous_blocker"]["proven"]
        and row["formal_gate"]["offline_dependency_gate"] == "NEEDS_WORLD"
        and not row["landing_probe"]["applicable_from_start_support"]
        for row in directions
    )
    if not stopped:
        raise AssertionError("D125 stop trigger was not reproduced")
    inputs = (
        ROOT / "scripts/d125_three_fact_observation.py",
        ROOT / "scripts/d123_dependency_probe.py",
        ROOT / "scripts/d123_actor_calibration.py",
        ROOT / "scripts/d122_fabric_timing.py",
        ROOT / "scripts/d120_momentum_entry_calibration.py",
        ROOT / "scripts/d118_momentum_calibration_runtime.py",
        ROOT / "mc2p/motion_nav/landing_edge_probe.py",
        ROOT / "evidence/motion_navigation/d124-proof-dependency-projection-v1/targeted/run-8/summary.json",
    )
    result = {
        "schema_version": "mc2p.d125-observation-feasibility-stop.v1",
        "decision": "D125",
        "phase": "D125-0",
        "status": "PARTIAL_STOPPED",
        "stop_trigger": {
            "code": STOP_CODE,
            "fact_role": "parent_and_successor",
            "reason": (
                "the joint parent/successor fact is visible from the middle "
                "platform but not from any safe precommit viewpoint"
            ),
        },
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            ).strip(),
        },
        "directions": directions,
        "existing_mechanisms": {
            "exact_request_supported": True,
            "multi_fact_owner_supported": True,
            "generic_body_reposition_supported": False,
            "landing_edge_probe_applicable": False,
        },
        "safety": {
            "movement_trials_started": 0,
            "fabric_started": False,
            "minecraft_started": False,
            "production_files_modified": 0,
            "fixture_truth_ingested": False,
            "unknown_to_air_inference_used": False,
        },
        "not_started": (
            "runtime_acquisition", "D125-1", "D125-2",
            "D123-2", "D123-3", "full_matrix", "fabric",
        ),
        "next_steps_authorized": False,
        "inputs": {path.relative_to(ROOT).as_posix(): _sha256(path)
                   for path in inputs},
        "limits": (
            "finite native scan and continuous geometry argument are reported separately",
            "middle-platform visibility is attribution only and cannot authorize the first jump",
            "no actor fact was written to WorldModel",
        ),
    }
    write_json(output / "stop.json", result)
    write_json(output / "manifest.json", {
        "schema_version": "mc2p.d125-observation-feasibility-manifest.v1",
        "files": {"stop.json": _sha256(output / "stop.json")},
    })
    print(json.dumps({
        "status": result["status"],
        "stop_code": STOP_CODE,
        "directions": len(directions),
        "movement_trials": 0,
        "fabric": False,
    }, ensure_ascii=False))
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
