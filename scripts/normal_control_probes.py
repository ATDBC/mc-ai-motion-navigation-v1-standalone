"""Frozen, bounded real-stage schedules and evidence checks for J3 controls."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from mc2p.runtime.segmented_trace import iter_segmented_jsonl
from mc2p.skills.normal_direction_control import world_direction
from scripts.block_observation_v3_sources import V3_BODY_SOURCES


PROBE_KINDS = ("side", "back", "yaw_sweep", "pitch_sweep", "release")
STEP_NS = 50_000_000
STANDING_BODY_HALF_WIDTH = .30
CANONICAL_BODY_AND_UNCERTAINTY_MARGIN = .45
_BODY_COMPATIBILITY_PREFIXES = (
    "deployment/fabric-observation-probe/",
    "mc2p/backends/",
    "mc2p/contracts/",
    "mc2p/runtime/",
)
# Select from the reviewed explicit body tuple, never by walking the checkout.
CONTROL_COMPATIBILITY_PATHS = tuple(
    path for path in V3_BODY_SOURCES
    if path.startswith(_BODY_COMPATIBILITY_PREFIXES)
) + ("mc2p/skills/normal_direction_control.py",)
CONTROL_PARAMETERS = {
    "wire_schema": "mc2p.client_action.v1",
    "control_profile": "normal_decoupled_v1",
    "movement_values": [-1, 0, 1],
    "jump": False,
    "sneak": False,
    "sprint": False,
    "input_valid_for_ticks": 1,
    "side_single_tick_request_limit_per_sign": 7,
    "control_interval_ns": STEP_NS,
    "client_tick_rate_hz": 20,
    "diagnostic_clocks": ["client_ticks", "sampled_at_jvm_ns"],
    "maximum_look_rate_degrees_per_second": 60.0,
    "action_lease_ns": 250_000_000,
    "release_limit_ticks": 20,
    "release_speed_blocks_per_tick": .03,
    "steady_cross_track_blocks": .10,
    "look_precision_degrees": .25,
    "standing_body_half_width_blocks": STANDING_BODY_HALF_WIDTH,
    "canonical_body_and_uncertainty_margin_blocks": CANONICAL_BODY_AND_UNCERTAINTY_MARGIN,
}
VALIDATION_PROFILE = "mc2p.normal-control-validation.v2"
MIN_SIGNED_DISPLACEMENT_BLOCKS = .01
MIN_SIGNED_VELOCITY_BLOCKS_PER_TICK = .000001


def _linear_targets(points: tuple[tuple[int, float], ...], *, axis: str):
    result: list[tuple[int, float, float]] = []
    for (start_ns, start), (end_ns, end) in zip(points, points[1:]):
        steps = (end_ns - start_ns) // STEP_NS
        if steps < 1 or start_ns + steps * STEP_NS != end_ns:
            raise ValueError("probe sweep anchors must align to 50 ms")
        for index in range(steps):
            elapsed_ns = index * STEP_NS
            value = start + (end - start) * index / steps
            result.append((
                start_ns + elapsed_ns,
                value if axis == "yaw" else 0.0,
                value if axis == "pitch" else 0.0,
            ))
    end_ns, end = points[-1]
    result.append((end_ns, end if axis == "yaw" else 0.0,
                   end if axis == "pitch" else 0.0))
    return tuple(result)


def probe_schedule(kind: str) -> tuple[tuple[int, float, float], ...]:
    """Return relative controller time and absolute expected look targets."""
    if kind not in PROBE_KINDS:
        raise ValueError("undeclared normal control probe")
    if kind == "yaw_sweep":
        return _linear_targets((
            (0, 0.0), (1_000_000_000, 60.0),
            (3_000_000_000, -60.0), (4_000_000_000, 0.0),
            (4_500_000_000, 0.0), (5_250_000_000, 45.0),
            (6_750_000_000, -45.0), (7_500_000_000, 0.0),
            (8_500_000_000, 0.0),
        ), axis="yaw")
    if kind == "pitch_sweep":
        return _linear_targets((
            (0, 0.0), (500_000_000, 30.0),
            (1_500_000_000, -30.0), (2_000_000_000, 0.0),
            (2_500_000_000, 0.0), (3_000_000_000, 30.0),
            (4_000_000_000, -30.0), (4_500_000_000, 0.0),
            (5_500_000_000, 0.0),
        ), axis="pitch")
    duration = {"side": 2_700_000_000, "back": 1_500_000_000,
                "release": 1_500_000_000}[kind]
    return tuple((elapsed, 0.0, 0.0)
                 for elapsed in range(0, duration + STEP_NS, STEP_NS))


def control_capability_profile(records: list[dict],
                               dependency_fingerprints: dict[str, str]) -> dict:
    """Build the J4-consumable gate without inferring unmeasured combinations."""
    if (type(records) is not list or type(dependency_fingerprints) is not dict
            or not dependency_fingerprints):
        raise ValueError("control capability inputs are invalid")
    required = {(backend, seed, kind)
                for backend in ("standalone", "craftground")
                for seed in (21001, 21002) for kind in PROBE_KINDS}
    selected: dict[tuple[str, int, str], dict] = {}
    for record in records:
        key = (record.get("backend"), record.get("seed"), record.get("probe_kind"))
        if key not in required:
            raise ValueError("capability record is outside the declared matrix")
        if record.get("classification") != ("development" if key[1] == 21001 else "held_out"):
            raise ValueError("capability record classification differs")
        previous = selected.get(key)
        if previous is None or record["run_id"] > previous["run_id"]:
            selected[key] = record

    allowed: list[dict] = []
    for kind in PROBE_KINDS:
        keys = sorted(key for key in required if key[2] == kind)
        if not all(key in selected for key in keys):
            continue
        measured_sets = []
        for key in keys:
            record = selected[key]
            metrics = record.get("metrics", {})
            engineering_status = record.get("engineering_status")
            capability_outcome = record.get("capability_outcome")
            if engineering_status != "valid" and capability_outcome == "passed":
                raise ValueError("invalid engineering run claims capability")
            if engineering_status == "valid" and capability_outcome == "passed":
                combinations = metrics.get("verified_combinations")
                if type(combinations) is not list:
                    raise ValueError("passed capability record lacks measured combinations")
            elif engineering_status == "valid":
                slices = metrics.get("capability_slices", [])
                if type(slices) is not list:
                    raise ValueError("capability slices differ")
                combinations = []
                for item in slices:
                    if (type(item) is not dict or set(item) != {
                            "stage", "outcome", "checks", "metrics",
                            "verified_combinations"}
                            or item.get("outcome") not in {"passed", "failed"}
                            or type(item.get("verified_combinations")) is not list):
                        raise ValueError("capability slice shape differs")
                    if item["outcome"] == "passed":
                        combinations.extend(item["verified_combinations"])
            else:
                combinations = []
            measured_sets.append({json.dumps(item, sort_keys=True) for item in combinations})
        intersection = set.intersection(*measured_sets)
        for encoded in sorted(intersection):
            combination = json.loads(encoded)
            if (type(combination) is not dict or set(combination) != {
                    "forward", "strafe", "look_axes", "stage"}):
                raise ValueError("measured control combination shape differs")
            allowed.append({**combination, "probe_kind": kind})
    complete = set(selected) == required
    status = ("incomplete" if not complete else
              "ready" if all(item["capability_outcome"] == "passed"
                             for item in selected.values())
              else "ready_with_restrictions")
    return {
        "schema_version": "mc2p.normal-control-capability.v1",
        "control_profile": "normal_decoupled_v1",
        "status": status,
        "compatibility_fingerprints": dict(sorted(dependency_fingerprints.items())),
        "required_matrix": [
            {"backend": backend, "seed": seed, "probe_kind": kind,
             "classification": "development" if seed == 21001 else "held_out"}
            for backend in ("standalone", "craftground")
            for seed in (21001, 21002) for kind in PROBE_KINDS
        ],
        "results": [selected[key] for key in sorted(selected)],
        "allowed_controls": allowed,
        "parameters": CONTROL_PARAMETERS,
        "not_inferred": [
            "unobserved signed or diagonal controls",
            "continuous leased input",
            "simultaneous yaw and pitch",
            "Cartesian combinations of separately passed controls",
        ],
    }


def refresh_control_capability_profile(artifact_parent: Path,
                                       dependency_fingerprints: dict[str, str]) -> Path:
    """Atomically refresh the aggregate gate from immutable, same-dependency runs."""
    from scripts.control_probe_core import write_json_atomic

    parent = Path(artifact_parent).absolute()
    records = []
    for run_dir in sorted(path for path in parent.iterdir() if path.is_dir()):
        manifest_path = run_dir / "evidence" / "run-manifest.json"
        result_path = run_dir / "evidence-result.json"
        if not manifest_path.is_file() or not result_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text("utf-8"))
            result = json.loads(result_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (manifest.get("mode") != "control_probe"
                or manifest.get("control_compatibility_fingerprints") != dependency_fingerprints
                or manifest.get("control_parameters") != CONTROL_PARAMETERS):
            continue
        records.append({
            "run_id": run_dir.name,
            "backend": manifest["backend"], "seed": manifest["seed"],
            "probe_kind": manifest["probe_kind"],
            "classification": manifest["classification"],
            "engineering_status": result.get("engineering_status", "invalid_run"),
            "capability_outcome": result.get("capability_outcome", "unavailable"),
            "result_sha256": _sha256(result_path),
            "source_tree_sha256": manifest["source_archive"]["tree_sha256"],
            "metrics": result.get("metrics", {}),
        })
    profile = control_capability_profile(records, dependency_fingerprints)
    destination = parent / "control-capability-v1.json"
    write_json_atomic(destination, profile)
    return destination


def write_derived_control_evaluation(run_directory: Path,
                                     destination: Path) -> Path:
    """Append a corrected evaluation without changing an immutable run."""
    from scripts.control_probe_core import write_json_atomic

    run = Path(run_directory).absolute()
    evidence = run / "evidence"
    original_result = run / "evidence-result.json"
    manifest = evidence / "run-manifest.json"
    archive_manifest = evidence / "source-archive" / "manifest.json"
    output = Path(destination).absolute()
    if output.exists():
        raise FileExistsError("derived control evaluation already exists")
    for path in (original_result, manifest, archive_manifest):
        if not path.is_file():
            raise FileNotFoundError(path)
    manifest_payload = json.loads(manifest.read_text("utf-8"))
    archive_payload = json.loads(archive_manifest.read_text("utf-8"))
    evaluator_path = Path(__file__).resolve()
    derived = {
        "schema_version": "mc2p.normal-control-derived-evaluation.v1",
        "validation_profile": VALIDATION_PROFILE,
        "control_parameters": CONTROL_PARAMETERS,
        "run_id": run.name,
        "original_evidence": {
            "result_sha256": _sha256(original_result),
            "run_manifest_sha256": _sha256(manifest),
            "source_archive_manifest_sha256": _sha256(archive_manifest),
            "source_tree_sha256": archive_payload["tree_sha256"],
            "manifest_source_tree_sha256": manifest_payload["source_archive"]["tree_sha256"],
        },
        "evaluator": {
            "path": "scripts/normal_control_probes.py",
            "sha256": _sha256(evaluator_path),
        },
        "evaluation": evaluate_control_probe(evidence),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output, derived)
    return output


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(fingerprints: dict[str, str]) -> str:
    encoded = json.dumps(
        fingerprints, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite(value, label: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError(label + " is not finite")
    return float(value)


def _vector(value: object, keys: tuple[str, ...], label: str) -> tuple[float, ...]:
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(label + " shape differs")
    return tuple(_finite(value[key], label + " " + key) for key in keys)


def _archive(directory: Path, manifest: dict) -> None:
    fingerprints = manifest.get("source_fingerprints")
    summary = manifest.get("source_archive")
    if type(fingerprints) is not dict or not fingerprints or type(summary) is not dict:
        raise ValueError("source archive declaration is missing")
    archive_root = directory / "source-archive"
    archived = json.loads((archive_root / "manifest.json").read_text("utf-8"))
    tree = _tree_digest(fingerprints)
    if (archived.get("source_fingerprints") != fingerprints
            or archived.get("file_count") != len(fingerprints)
            or archived.get("tree_sha256") != tree
            or summary != {"file_count": len(fingerprints), "tree_sha256": tree}):
        raise ValueError("source archive manifest differs")
    found: dict[str, str] = {}
    for path in (archive_root / "files").rglob("*"):
        if path.is_file():
            found[path.relative_to(archive_root / "files").as_posix()] = _sha256(path)
    if found != fingerprints:
        raise ValueError("source archive content differs")
    dependencies = manifest.get("control_compatibility_fingerprints")
    if type(dependencies) is not dict or not dependencies \
            or any(fingerprints.get(name) != digest for name, digest in dependencies.items()):
        raise ValueError("control dependency fingerprint binding differs")


def _convex_hull(points: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    ordered = sorted(set(points))
    if len(ordered) <= 1:
        return tuple(ordered)

    def cross(origin, first, second):
        return ((first[0] - origin[0]) * (second[1] - origin[1])
                - (first[1] - origin[1]) * (second[0] - origin[0]))

    lower: list[tuple[float, float]] = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 1e-12:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(ordered):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 1e-12:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def _inside_closed_convex(polygon: tuple[tuple[float, float], ...],
                          point: tuple[float, float]) -> bool:
    signs = []
    for first, second in zip(polygon, polygon[1:] + polygon[:1]):
        cross = ((second[0] - first[0]) * (point[1] - first[1])
                 - (second[1] - first[1]) * (point[0] - first[0]))
        if abs(cross) > 1e-9:
            signs.append(cross > 0)
    return not signs or all(signs) or not any(signs)


def _inside_canonical_envelope(request: dict, x: float, z: float) -> bool:
    ox, _, oz = _vector(request["origin"], ("x", "y", "z"), "request origin")
    vx, _, vz = _vector(request["velocity"], ("x", "y", "z"), "request velocity")
    yaw, _ = _vector(request["expected_look"], ("yaw", "pitch"), "expected look")
    movement = request["movement"]
    dx, dz = world_direction(yaw, movement["forward"], movement["strafe"])
    speed = math.hypot(vx, vz)
    travel = 0.0 if dx == dz == 0 else .45 + 2 * speed
    center_hull = _convex_hull((
        (ox, oz),
        (ox + dx * travel, oz + dz * travel),
        (ox + 2 * vx, oz + 2 * vz),
        (ox + 2 * vx + dx * travel, oz + 2 * vz + dz * travel),
    ))
    margin = CANONICAL_BODY_AND_UNCERTAINTY_MARGIN
    authorized_body_hull = _convex_hull(tuple(
        (point[0] + expand_x, point[1] + expand_z)
        for point in center_hull
        for expand_x in (-margin, margin)
        for expand_z in (-margin, margin)
    ))
    actual_body_corners = (
        (x + expand_x, z + expand_z)
        for expand_x in (-STANDING_BODY_HALF_WIDTH, STANDING_BODY_HALF_WIDTH)
        for expand_z in (-STANDING_BODY_HALF_WIDTH, STANDING_BODY_HALF_WIDTH)
    )
    return all(_inside_closed_convex(authorized_body_hull, corner)
               for corner in actual_body_corners)


def _wrap_yaw(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _expected_runtime_action(request: dict) -> dict:
    expected_yaw, expected_pitch = _vector(
        request["expected_look"], ("yaw", "pitch"), "expected look")
    initial_yaw, initial_pitch = _vector(
        request["initial_look"], ("yaw", "pitch"), "initial look")
    return {
        "schema_version": "mc2p.action-snapshot.v1",
        "episode_id": request["episode_id"],
        "request_sequence_id": request["request_sequence_id"],
        "deadline_monotonic_ns": request["expires_at_controller_ns"],
        "valid_for_ticks": 1,
        "movement": request["movement"],
        "look": {
            "yaw_delta_degrees": _wrap_yaw(expected_yaw - initial_yaw),
            "pitch_delta_degrees": expected_pitch - initial_pitch,
        },
        "operation": None,
        "cancel_request_sequence_id": None,
    }


def _movement_segment_metrics(requests: list[dict],
                              by_request: dict[tuple[str, int], list[dict]],
                              required_segments: set[str]) -> list[dict]:
    metrics = []
    for segment_id in sorted(required_segments):
        selected = [request for request in requests
                    if request.get("phase") == "probe"
                    and request.get("segment_id") == segment_id]
        actual = []
        seen_events = set()
        for request in selected:
            identity = (request["episode_id"], request["request_sequence_id"])
            for row in by_request.get(identity, []):
                event_identity = row.get("event_sequence", id(row))
                if event_identity not in seen_events:
                    seen_events.add(event_identity)
                    actual.append(row)
        actual.sort(key=lambda row: (row["client_ticks"], row["sampled_at_jvm_ns"]))
        if selected:
            sx, sz = _vector(
                selected[0]["segment_origin"], ("x", "z"), "segment origin")
            dx, dz = _vector(
                selected[0]["segment_direction"], ("x", "z"), "segment direction")
        else:
            sx = sz = dx = dz = 0.0
        if actual:
            final_x, _, final_z = _vector(
                actual[-1]["actual_pose"]["position"],
                ("x", "y", "z"), "actual position")
            displacement = (final_x - sx) * dx + (final_z - sz) * dz
            signed_velocities = []
            for row in actual:
                vx, _, vz = _vector(
                    row["actual_pose"]["velocity"],
                    ("x", "y", "z"), "actual velocity")
                signed_velocities.append(vx * dx + vz * dz)
            maximum_velocity = max(signed_velocities)
        else:
            displacement = None
            maximum_velocity = None
        passed = (displacement is not None
                  and displacement > MIN_SIGNED_DISPLACEMENT_BLOCKS
                  and maximum_velocity is not None
                  and maximum_velocity > MIN_SIGNED_VELOCITY_BLOCKS_PER_TICK)
        metrics.append({
            "segment_id": segment_id,
            "actual_event_count": len(actual),
            "signed_displacement_blocks": displacement,
            "maximum_signed_velocity_blocks_per_tick": maximum_velocity,
            "passed": passed,
        })
    return metrics


def _gaze_capability_slices(kind: str, requests: list[dict],
                            by_request: dict[tuple[str, int], list[dict]],
                            approved_region: dict) -> list[dict]:
    if kind not in {"yaw_sweep", "pitch_sweep"}:
        return []
    axis = "yaw" if kind == "yaw_sweep" else "pitch"
    declared = ({"stationary_calibration": (-60.0, 60.0),
                 "moving": (-45.0, 45.0)} if axis == "yaw" else
                {"stationary_calibration": (-30.0, 30.0),
                 "moving": (-30.0, 30.0)})
    result = []
    for stage in ("stationary_calibration", "moving"):
        selected = [request for request in requests
                    if request["phase"] == "probe" and request["stage"] == stage]
        look_events = []
        combinations = set()
        normal = True
        neutral = True
        envelope = True
        max_cross = 0.0
        max_look_error = 0.0
        positions = []
        max_horizontal_speed = 0.0
        for request in selected:
            identity = (request["episode_id"], request["request_sequence_id"])
            actual = by_request.get(identity, [])
            movement = request["movement"]
            expected_yaw, expected_pitch = _vector(
                request["expected_look"], ("yaw", "pitch"), "expected look")
            initial_yaw, initial_pitch = _vector(
                request["initial_look"], ("yaw", "pitch"), "initial look")
            yaw_changed = abs(_wrap_yaw(expected_yaw - initial_yaw)) > 1e-9
            pitch_changed = abs(expected_pitch - initial_pitch) > 1e-9
            look_axis = ("yaw_pitch" if yaw_changed and pitch_changed else
                         "yaw" if yaw_changed else "pitch" if pitch_changed else "fixed")
            if look_axis == axis:
                combinations.add((movement["forward"], movement["strafe"],
                                  look_axis, stage))
            sx, sz = _vector(request["segment_origin"], ("x", "z"), "segment origin")
            direction = _vector(
                request["segment_direction"], ("x", "z"), "segment direction")
            for row in actual:
                pose = row["actual_pose"]
                px, _, pz = _vector(pose["position"], ("x", "y", "z"),
                                     "actual position")
                vx, _, vz = _vector(pose["velocity"], ("x", "y", "z"),
                                     "actual velocity")
                positions.append((px, pz))
                max_horizontal_speed = max(max_horizontal_speed, math.hypot(vx, vz))
                normal &= (pose["pose"] == "standing" and pose["on_ground"] is True
                           and pose["actual_sprinting"] is False
                           and pose["actual_sneaking"] is False)
                envelope &= _inside_canonical_envelope(request, px, pz)
                envelope &= (approved_region["min_x"]
                             <= px - STANDING_BODY_HALF_WIDTH
                             and px + STANDING_BODY_HALF_WIDTH
                             <= approved_region["max_x"]
                             and approved_region["min_z"]
                             <= pz - STANDING_BODY_HALF_WIDTH
                             and pz + STANDING_BODY_HALF_WIDTH
                             <= approved_region["max_z"])
                max_cross = max(max_cross, abs(
                    (px - sx) * direction[1] - (pz - sz) * direction[0]
                ) if direction != (0.0, 0.0) else 0.0)
                if row["event"] == "input_consumed":
                    neutral &= (row["input_state"] == "neutral"
                                and row["actual_input"] == {
                                    "forward": 0.0, "strafe": 0.0,
                                    "jump": False, "sneak": False, "sprint": False,
                                }) if stage == "stationary_calibration" else True
                elif row["event"] == "look_applied":
                    look_events.append(row)
                    actual_yaw, actual_pitch = _vector(
                        row["actual_look"], ("yaw", "pitch"), "actual look")
                    max_look_error = max(
                        max_look_error, abs(_wrap_yaw(actual_yaw - expected_yaw)),
                        abs(actual_pitch - expected_pitch))
        look_events.sort(key=lambda row: row["sampled_at_jvm_ns"])
        max_rate = 0.0
        for previous, current in zip(look_events, look_events[1:]):
            tick_delta = current["client_ticks"] - previous["client_ticks"]
            if tick_delta <= 0:
                max_rate = math.inf
                break
            before, after = previous["actual_look"], current["actual_look"]
            max_rate = max(max_rate, math.hypot(
                _wrap_yaw(after["yaw"] - before["yaw"]),
                after["pitch"] - before["pitch"],
            ) / (tick_delta / 20.0))
        values = [row["actual_look"][axis] for row in look_events]
        actual_range = [min(values), max(values)] if values else None
        lower, upper = declared[stage]
        range_ok = (actual_range is not None
                    and lower - .25 <= actual_range[0] <= lower + .25
                    and upper - .25 <= actual_range[1] <= upper + .25)
        if positions:
            origin_x, origin_z = positions[0]
            stationary_drift = max(math.hypot(x - origin_x, z - origin_z)
                                   for x, z in positions)
        else:
            stationary_drift = math.inf
        movement_segments = (_movement_segment_metrics(
            selected, by_request,
            {"yaw-moving" if kind == "yaw_sweep" else "pitch-moving"})
            if stage == "moving" else [])
        checks = {
            "actual_identity": bool(selected) and all(
                by_request.get((request["episode_id"], request["request_sequence_id"]))
                for request in selected),
            "normal_state": normal,
            "approved_and_canonical_envelope": envelope,
            "look_precision_and_rate": max_look_error <= .25 + 1e-9
            and max_rate <= 60.0 + 1e-6,
            "declared_axis_range": range_ok,
            "stationary_neutral": (neutral and max_horizontal_speed <= .03 + 1e-9
                                   and stationary_drift <= .10 + 1e-9
                                   if stage == "stationary_calibration" else True),
            "moving_cross_track": (max_cross <= .10 + 1e-9
                                   if stage == "moving" else True),
            "moving_combination": (bool(combinations)
                                   if stage == "moving" else True),
            "moving_signed_motion": (all(
                item["passed"] for item in movement_segments)
                if stage == "moving" else True),
        }
        passed = all(checks.values())
        result.append({
            "stage": stage, "outcome": "passed" if passed else "failed",
            "checks": checks,
            "metrics": {
                "actual_axis_range": actual_range,
                "max_look_error_degrees": max_look_error,
                "max_actual_look_rate_degrees_per_second": max_rate,
                "max_cross_track_blocks": max_cross,
                "max_stationary_drift_blocks": stationary_drift,
                "max_horizontal_speed_blocks_per_tick": max_horizontal_speed,
                "movement_segments": movement_segments,
            },
            "verified_combinations": [
                {"forward": forward, "strafe": strafe, "look_axes": look_axis,
                 "stage": combination_stage}
                for forward, strafe, look_axis, combination_stage
                in sorted(combinations)
            ] if passed else [],
        })
    return result


def evaluate_control_probe(directory: Path) -> dict:
    """Validate one immutable probe without consulting the current checkout."""
    root = Path(directory).absolute()
    result = {
        "schema_version": "mc2p.normal-control-probe-result.v1",
        "engineering_status": "invalid_run",
        "capability_outcome": "unavailable",
        "probe_kind": None,
        "backend": None,
        "seed": None,
        "metrics": {},
        "checks": {},
        "errors": [],
    }
    try:
        manifest = json.loads((root / "run-manifest.json").read_text("utf-8"))
        required = {
            "schema_version", "mode", "probe_kind", "backend", "seed",
            "classification", "control_profile", "approved_region",
            "control_parameters",
            "control_events_path", "source_archive", "source_fingerprints",
            "control_compatibility_fingerprints",
        }
        if type(manifest) is not dict or set(manifest) != required:
            raise ValueError("control run manifest fields differ")
        kind, backend, seed = manifest["probe_kind"], manifest["backend"], manifest["seed"]
        result.update(probe_kind=kind, backend=backend, seed=seed)
        if (manifest["schema_version"] != "mc2p.normal-control-probe-run.v1"
                or manifest["mode"] != "control_probe" or kind not in PROBE_KINDS
                or backend not in {"standalone", "craftground"}
                or type(seed) is not int or seed not in {21001, 21002}
                or manifest["classification"] != ("development" if seed == 21001 else "held_out")
                or manifest["control_profile"] != "normal_decoupled_v1"
                or manifest["control_parameters"] != CONTROL_PARAMETERS):
            raise ValueError("control run identity differs")
        _archive(root, manifest)
        result["checks"]["source_archive"] = True
        terminal = json.loads((root / "control-terminal.json").read_text("utf-8"))
        cleanup = json.loads((root / "cleanup.json").read_text("utf-8"))
        if (type(terminal) is not dict or set(terminal) != {
                "schema_version", "finished_at_controller_ns", "failure"}
                or terminal.get("schema_version")
                != "mc2p.normal-control-probe-terminal.v1"
                or type(terminal.get("finished_at_controller_ns")) is not int
                or terminal["finished_at_controller_ns"] < 0
                or terminal.get("failure") is not None):
            raise ValueError("control probe worker terminal failed")
        if (type(cleanup) is not dict or set(cleanup) != {
                "schema_version", "ordered_source_released", "live_processes",
                "listening_ports", "source_tree_unchanged", "errors"}
                or cleanup.get("schema_version") != "mc2p.normal-control-probe-cleanup.v1"
                or cleanup.get("ordered_source_released") is not True
                or cleanup.get("live_processes") != 0
                or cleanup.get("listening_ports") != []
                or cleanup.get("source_tree_unchanged") is not True
                or cleanup.get("errors") != []):
            raise ValueError("control probe cleanup failed")
        result["checks"]["worker_terminal_and_cleanup"] = True
        try:
            requests = list(iter_segmented_jsonl(root / "control-requests"))
        except (OSError, ValueError) as error:
            raise ValueError("accepted request stream is missing or unsealed") from error
        try:
            events = list(iter_segmented_jsonl(root / manifest["control_events_path"]))
        except (OSError, ValueError) as error:
            raise ValueError("actual control stream is missing or unsealed") from error
        if not requests or not events:
            raise ValueError("actual control stream or request stream is empty")
        try:
            runtime_rows = list(iter_segmented_jsonl(root / "runtime-trace" / "trace"))
        except (OSError, ValueError) as error:
            raise ValueError("raw Runtime stream is missing or unsealed") from error
        if not runtime_rows:
            raise ValueError("raw Runtime stream is empty")
        result["checks"]["sealed_streams"] = True
        by_request: dict[tuple[str, int], list[dict]] = {}
        inactive_input_ok = True
        previous_sequence = 0
        previous_jvm = -1
        for event in events:
            common = {
                "schema_version", "session_id", "world_id", "event_sequence",
                "time_event_sequence", "client_ticks", "sampled_at_jvm_ns",
                "event", "episode_id", "request_sequence_id", "available",
                "actual_pose",
            }
            extra = ({"actual_look"} if event.get("event") == "look_applied"
                     else {"input_state", "actual_input"}
                     if event.get("event") == "input_consumed" else set())
            if (not extra or set(event) != common | extra
                    or event.get("schema_version") != "mc2p.client-control-event.v1"
                    or type(event.get("event_sequence")) is not int
                    or event["event_sequence"] != previous_sequence + 1
                    or type(event.get("client_ticks")) is not int or event["client_ticks"] < 0
                    or type(event.get("sampled_at_jvm_ns")) is not int
                    or event["sampled_at_jvm_ns"] <= previous_jvm
                    or event.get("available") is not True):
                raise ValueError("actual control stream stage is unavailable or malformed")
            previous_sequence = event["event_sequence"]
            previous_jvm = event["sampled_at_jvm_ns"]
            _vector(event["actual_pose"]["position"], ("x", "y", "z"), "actual position")
            _vector(event["actual_pose"]["velocity"], ("x", "y", "z"), "actual velocity")
            _finite(event["actual_pose"]["yaw"], "actual yaw")
            _finite(event["actual_pose"]["pitch"], "actual pitch")
            identity = (event.get("episode_id"), event.get("request_sequence_id"))
            if type(identity[0]) is not str or type(identity[1]) is not int:
                raise ValueError("actual control request identity is missing")
            by_request.setdefault(identity, []).append(event)
            if event["event"] == "input_consumed":
                actual_input = event["actual_input"]
                if event["input_state"] in {"expired", "lease_exhausted", "disallowed"} \
                        and actual_input != {
                            "forward": 0.0, "strafe": 0.0, "jump": False,
                            "sneak": False, "sprint": False,
                        }:
                    inactive_input_ok = False

        request_ids: set[tuple[str, int]] = set()
        requests_by_id: dict[tuple[str, int], dict] = {}
        max_cross_track = 0.0
        max_look_error = 0.0
        envelope_ok = True
        normal_ok = True
        look_events: list[dict] = []
        probe_look_events: list[dict] = []
        last_leased_tick = None
        leased_ticks: list[int] = []
        verified_combinations: set[tuple[int, int, str, str]] = set()
        segment_references: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {}
        release_request_count = 0
        for request in requests:
            expected_keys = {
                "schema_version", "phase", "episode_id", "request_sequence_id",
                "runtime_trace_ordinal",
                "submitted_at_controller_ns", "expires_at_controller_ns",
                "movement", "expected_look", "look_required", "origin", "velocity",
                "segment_id", "segment_origin", "segment_direction",
                "initial_look", "stage", "guard", "receipt",
            }
            if set(request) != expected_keys \
                    or request.get("schema_version") != "mc2p.normal-control-request.v1" \
                    or request.get("phase") not in {"pre_observation", "probe"}:
                raise ValueError("control request record differs")
            identity = (request["episode_id"], request["request_sequence_id"])
            if identity in request_ids:
                raise ValueError("duplicate control request identity")
            request_ids.add(identity)
            requests_by_id[identity] = request
            ordinal = request["runtime_trace_ordinal"]
            if type(ordinal) is not int or not 0 <= ordinal < len(runtime_rows):
                raise ValueError("control request Runtime ordinal differs")
            runtime_row = runtime_rows[ordinal]
            try:
                runtime_action = runtime_row["payload"]["decision"]["action"]
                runtime_observation = runtime_row["payload"]["backend_result"]["observation"]
                runtime_receipt = runtime_row["payload"]["backend_result"]["receipt"]
            except (KeyError, TypeError) as error:
                raise ValueError("control request does not point to a Runtime action") from error
            if (runtime_row.get("record_type") != "step"
                    or (runtime_action.get("episode_id"), runtime_action.get("request_sequence_id")) != identity):
                raise ValueError("control request identity differs from raw Runtime")
            expected_runtime_action = _expected_runtime_action(request)
            if (type(runtime_action) is not dict
                    or set(runtime_action) != set(expected_runtime_action) | {
                        "observation_sequence_id"}
                    or any(runtime_action.get(name) != value
                           for name, value in expected_runtime_action.items())
                    or type(runtime_action.get("observation_sequence_id")) is not int
                    or runtime_action["observation_sequence_id"] < 0):
                raise ValueError("raw Runtime action differs from accepted control request")
            observation_sequence = runtime_action["observation_sequence_id"]
            if (type(runtime_observation) is not dict
                    or runtime_observation.get("episode_id") != identity[0]
                    or runtime_observation.get("sequence_id") != observation_sequence + 1
                    or runtime_observation.get("request_sequence_id") != identity[1]):
                raise ValueError("raw Runtime action observation binding differs")
            if (type(runtime_receipt) is not dict
                    or runtime_receipt.get("episode_id") != identity[0]
                    or runtime_receipt.get("request_sequence_id") != identity[1]
                    or runtime_receipt.get("generation_id") != runtime_observation["sequence_id"]
                    or runtime_receipt.get("status") != request["receipt"].get("status")
                    or runtime_receipt.get("reason") != request["receipt"].get("reason")):
                raise ValueError("raw Runtime receipt differs from accepted control request")
            actual = by_request.get(identity, [])
            if not actual:
                raise ValueError("actual control stream does not cover accepted request")
            if request["guard"] != {"passed": True, "reason": None} \
                    or request["receipt"]["status"] not in {"executed", "confirmed_local"}:
                raise ValueError("blocked guard or unaccepted receipt cannot prove control")
            movement = request["movement"]
            if (type(movement) is not dict or set(movement) != {
                    "forward", "strafe", "jump", "sneak", "sprint"}
                    or movement["forward"] not in {-1, 0, 1}
                    or movement["strafe"] not in {-1, 0, 1}
                    or any(movement[name] is not False for name in ("jump", "sneak", "sprint"))):
                raise ValueError("probe movement is not frozen normal input")
            expected_yaw, expected_pitch = _vector(
                request["expected_look"], ("yaw", "pitch"), "expected look")
            initial_yaw, initial_pitch = _vector(
                request["initial_look"], ("yaw", "pitch"), "initial look")
            stage = request["stage"]
            if stage not in {"pre_observation", "stationary_calibration", "moving",
                             "release_observation", "repeated_single_tick"}:
                raise ValueError("control request stage differs")
            allowed_segments = {
                "side": {"side-positive", "side-negative"},
                "back": {"back"},
                "yaw_sweep": {"yaw-calibration", "yaw-moving"},
                "pitch_sweep": {"pitch-calibration", "pitch-moving"},
                "release": {"forward-release"},
            }
            if request["phase"] == "pre_observation":
                if request["segment_id"] != "pre-observation" or stage != "pre_observation":
                    raise ValueError("pre-observation segment identity differs")
            elif request["segment_id"] not in allowed_segments[kind]:
                raise ValueError("probe world-reference segment identity differs")
            associated_look = [row for row in actual if row["event"] == "look_applied"]
            associated_input = [row for row in actual if row["event"] == "input_consumed"]
            expected_input_state = ("neutral" if movement["forward"] == movement["strafe"] == 0
                                    else "leased")
            if not any(row["input_state"] == expected_input_state
                       for row in associated_input):
                raise ValueError("accepted request lacks actual input consumption")
            if request["look_required"]:
                if len(associated_look) != 1:
                    raise ValueError("look request lacks exactly one actual application")
                look_events.extend(associated_look)
                if request["phase"] == "probe":
                    probe_look_events.extend(associated_look)
                actual_yaw, actual_pitch = _vector(
                    associated_look[0]["actual_look"], ("yaw", "pitch"), "actual look")
                max_look_error = max(max_look_error,
                                     abs(_wrap_yaw(actual_yaw - expected_yaw)),
                                     abs(actual_pitch - expected_pitch))
            elif associated_look:
                raise ValueError("neutral look request has an unexpected application")
            sx, sz = _vector(request["segment_origin"], ("x", "z"), "segment origin")
            direction = _vector(
                request["segment_direction"], ("x", "z"), "segment direction")
            direction_length = math.hypot(*direction)
            if direction_length not in {0.0, 1.0}:
                raise ValueError("segment direction is not zero or unit length")
            segment_id = request["segment_id"]
            reference = ((sx, sz), direction)
            if segment_references.setdefault(segment_id, reference) != reference:
                raise ValueError("world-reference segment was reset")
            yaw_changed = abs(_wrap_yaw(expected_yaw - initial_yaw)) > 1e-9
            pitch_changed = abs(expected_pitch - initial_pitch) > 1e-9
            look_axes = ("yaw_pitch" if yaw_changed and pitch_changed else
                         "yaw" if yaw_changed else "pitch" if pitch_changed else "fixed")
            leased_for_request = [row for row in associated_input
                                  if row["input_state"] == "leased"]
            if movement["forward"] != 0 or movement["strafe"] != 0:
                if len(leased_for_request) != 1:
                    raise ValueError("single-tick request lacks exactly one actual leased sample")
                if request["phase"] == "probe":
                    verified_combinations.add((
                        movement["forward"], movement["strafe"], look_axes, stage))
                if stage == "repeated_single_tick":
                    release_request_count += 1
            elif request["phase"] == "probe" and look_axes != "fixed":
                verified_combinations.add((0, 0, look_axes, stage))
            for row in actual:
                pose = row["actual_pose"]
                px, _, pz = _vector(pose["position"], ("x", "y", "z"), "actual position")
                region = manifest["approved_region"]
                if not (region["min_x"] <= px - STANDING_BODY_HALF_WIDTH
                        and px + STANDING_BODY_HALF_WIDTH <= region["max_x"]
                        and region["min_z"] <= pz - STANDING_BODY_HALF_WIDTH
                        and pz + STANDING_BODY_HALF_WIDTH <= region["max_z"]):
                    raise ValueError("actual control body left the approved region")
                envelope_ok &= _inside_canonical_envelope(request, px, pz)
                max_cross_track = max(
                    max_cross_track,
                    abs((px - sx) * direction[1] - (pz - sz) * direction[0])
                    if direction != (0.0, 0.0) else 0.0,
                )
                normal_ok &= (pose["pose"] == "standing" and pose["on_ground"] is True
                              and pose["actual_sprinting"] is False
                              and pose["actual_sneaking"] is False
                              and row.get("actual_input", {}).get("jump", False) is False
                              and row.get("actual_input", {}).get("sneak", False) is False
                              and row.get("actual_input", {}).get("sprint", False) is False)
                if row["event"] == "input_consumed":
                    actual_input = row["actual_input"]
                    if row["input_state"] in {"expired", "lease_exhausted", "disallowed"}:
                        inactive_input_ok &= (actual_input == {
                            "forward": 0.0, "strafe": 0.0, "jump": False,
                            "sneak": False, "sprint": False,
                        })
                    elif row["input_state"] in {"leased", "neutral"}:
                        inactive_input_ok &= (
                            actual_input["forward"] == float(movement["forward"])
                            and actual_input["strafe"] == float(movement["strafe"])
                        )
                        if row["input_state"] == "leased":
                            last_leased_tick = row["client_ticks"]
                            leased_ticks.append(row["client_ticks"])
                    else:
                        raise ValueError("actual input stage label differs")

        runtime_ids = set()
        close_release_ids = set()
        for row in runtime_rows:
            try:
                if row.get("record_type") == "step":
                    action = row["payload"]["decision"]["action"]
                elif row.get("record_type") == "close_release":
                    action = row["payload"]["action"]
                    if (action.get("movement") != {
                            "forward": 0, "strafe": 0, "jump": False,
                            "sneak": False, "sprint": False,
                        } or action.get("look") != {
                            "yaw_delta_degrees": 0.0,
                            "pitch_delta_degrees": 0.0,
                        }):
                        continue
                    close_release_ids.add(
                        (action["episode_id"], action["request_sequence_id"]))
                    continue
                else:
                    continue
                runtime_ids.add((action["episode_id"], action["request_sequence_id"]))
            except (KeyError, TypeError):
                continue
        for identity in by_request:
            release_events = by_request[identity]
            neutral_release = (
                identity in close_release_ids
                and all(row["event"] == "input_consumed"
                        and row["input_state"] == "neutral"
                        and row["actual_input"] == {
                            "forward": 0.0, "strafe": 0.0, "jump": False,
                            "sneak": False, "sprint": False,
                        } for row in release_events)
            )
            if (identity not in request_ids and identity not in runtime_ids
                    and not neutral_release):
                raise ValueError("actual control event is not bound to an archived Runtime action")
        look_events.sort(key=lambda row: row["sampled_at_jvm_ns"])
        max_rate = 0.0
        for previous, current in zip(look_events, look_events[1:]):
            tick_delta = current["client_ticks"] - previous["client_ticks"]
            if tick_delta <= 0:
                raise ValueError("duplicate look application within one client tick")
            elapsed = tick_delta / 20.0
            before = previous["actual_look"]
            after = current["actual_look"]
            rate = math.hypot(_wrap_yaw(after["yaw"] - before["yaw"]),
                              after["pitch"] - before["pitch"]) / elapsed
            max_rate = max(max_rate, rate)

        input_events = [row for row in events if row["event"] == "input_consumed"]
        release_speed = None
        release_stop_ticks = None
        side_release_windows = []
        if last_leased_tick is not None:
            if kind == "release":
                last_leased = max(
                    (row for row in input_events if row["input_state"] == "leased"),
                    key=lambda row: row["sampled_at_jvm_ns"])
                identity = (last_leased["episode_id"],
                            last_leased["request_sequence_id"])
                silent = [row for row in input_events
                          if (row["episode_id"], row["request_sequence_id"]) == identity
                          and row["input_state"] in {"lease_exhausted", "expired"}
                          and 0 < row["client_ticks"] - last_leased_tick <= 20]
                silent.sort(key=lambda row: row["client_ticks"])
                deltas = [row["client_ticks"] - last_leased_tick for row in silent]
                if not silent or deltas != list(range(1, deltas[-1] + 1)):
                    raise ValueError("silent release Input.tick interval is missing")
                for row, delta in zip(silent, deltas):
                    velocity = row["actual_pose"]["velocity"]
                    speed = math.hypot(velocity["x"], velocity["z"])
                    if speed <= .03 + 1e-9:
                        release_speed = speed
                        release_stop_ticks = delta
                        break
                if release_speed is None:
                    velocity = silent[-1]["actual_pose"]["velocity"]
                    release_speed = math.hypot(velocity["x"], velocity["z"])
            else:
                stopped = [row for row in input_events
                           if row["client_ticks"] - last_leased_tick >= 20]
                if stopped:
                    velocity = stopped[0]["actual_pose"]["velocity"]
                    release_speed = math.hypot(velocity["x"], velocity["z"])
        if kind == "side":
            for segment_id in ("side-positive", "side-negative"):
                leased = [row for row in input_events
                          if row["input_state"] == "leased"
                          and (row["episode_id"], row["request_sequence_id"])
                          in requests_by_id
                          and requests_by_id[(row["episode_id"],
                                              row["request_sequence_id"])]["segment_id"]
                          == segment_id]
                if leased:
                    last = max(leased, key=lambda row: (
                        row["client_ticks"], row["sampled_at_jvm_ns"]))
                    next_motion_ticks = [
                        row["client_ticks"] for row in input_events
                        if row["input_state"] == "leased"
                        and row["client_ticks"] > last["client_ticks"]
                        and (row["episode_id"], row["request_sequence_id"])
                        in requests_by_id
                        and requests_by_id[(row["episode_id"],
                                            row["request_sequence_id"])]["segment_id"]
                        != segment_id]
                    next_motion_tick = min(next_motion_ticks, default=None)
                    candidates = [row for row in input_events
                                  if 0 < row["client_ticks"] - last["client_ticks"] <= 20
                                  and (next_motion_tick is None
                                       or row["client_ticks"] < next_motion_tick)]
                    candidates.sort(key=lambda row: (
                        row["client_ticks"], row["sampled_at_jvm_ns"]))
                    stop_row = None
                    for row in candidates:
                        velocity = row["actual_pose"]["velocity"]
                        if math.hypot(velocity["x"], velocity["z"]) <= .03 + 1e-9:
                            stop_row = row
                            break
                    stop_ticks = (None if stop_row is None else
                                  stop_row["client_ticks"] - last["client_ticks"])
                    observed_deltas = sorted(set(
                        row["client_ticks"] - last["client_ticks"]
                        for row in candidates
                        if stop_ticks is None
                        or row["client_ticks"] - last["client_ticks"] <= stop_ticks))
                    continuous = (stop_ticks is not None
                                  and observed_deltas == list(range(1, stop_ticks + 1)))
                    stop_speed = None
                    if stop_row is not None:
                        velocity = stop_row["actual_pose"]["velocity"]
                        stop_speed = math.hypot(velocity["x"], velocity["z"])
                else:
                    last = None
                    next_motion_tick = None
                    observed_deltas = []
                    stop_ticks = None
                    stop_speed = None
                    continuous = False
                side_release_windows.append({
                    "segment_id": segment_id,
                    "last_leased_tick": None if last is None else last["client_ticks"],
                    "next_movement_tick": next_motion_tick,
                    "observed_release_ticks": observed_deltas,
                    "release_stop_ticks": stop_ticks,
                    "release_speed_blocks_per_tick": stop_speed,
                    "passed": continuous,
                })
            measured_speeds = [row["release_speed_blocks_per_tick"]
                               for row in side_release_windows
                               if row["release_speed_blocks_per_tick"] is not None]
            release_speed = max(measured_speeds, default=None)
            release_stop_ticks = max(
                (row["release_stop_ticks"] for row in side_release_windows
                 if row["release_stop_ticks"] is not None), default=None)
        required_movement_segments = {
            "side": {"side-positive", "side-negative"},
            "back": {"back"},
            "yaw_sweep": {"yaw-moving"},
            "pitch_sweep": {"pitch-moving"},
            "release": {"forward-release"},
        }[kind]
        movement_segments = _movement_segment_metrics(
            requests, by_request, required_movement_segments)
        signed_motion_ok = all(row["passed"] for row in movement_segments)
        unique_leased_ticks = sorted(set(leased_ticks))
        leased_intervals = [current - previous for previous, current in zip(
            unique_leased_ticks, unique_leased_ticks[1:])]
        probe_yaws = [row["actual_look"]["yaw"] for row in probe_look_events]
        probe_pitches = [row["actual_look"]["pitch"] for row in probe_look_events]
        result["metrics"] = {
            "max_cross_track_blocks": max_cross_track,
            "max_look_error_degrees": max_look_error,
            "max_actual_look_rate_degrees_per_second": max_rate,
            "release_speed_blocks_per_tick": release_speed,
            "release_stop_ticks": release_stop_ticks,
            "side_release_windows": side_release_windows,
            "movement_segments": movement_segments,
            "actual_event_count": len(events),
            "accepted_request_count": len(requests),
            "actual_leased_tick_count": len(unique_leased_ticks),
            "actual_leased_tick_intervals": leased_intervals,
            "maximum_actual_leased_tick_gap": max(leased_intervals, default=None),
            "actual_probe_yaw_range": ([min(probe_yaws), max(probe_yaws)]
                                       if probe_yaws else None),
            "actual_probe_pitch_range": ([min(probe_pitches), max(probe_pitches)]
                                         if probe_pitches else None),
            "verified_combinations": [
                {"forward": forward, "strafe": strafe, "look_axes": axes,
                 "stage": stage}
                for forward, strafe, axes, stage in sorted(verified_combinations)
            ],
            "capability_slices": _gaze_capability_slices(
                kind, requests, by_request, manifest["approved_region"]),
        }
        failures = []
        if not inactive_input_ok:
            failures.append("motion input after lease expiry or release")
        if not envelope_ok:
            failures.append("actual trajectory exceeded canonical guard envelope")
        if not normal_ok:
            failures.append("normal movement state was not preserved")
        if not signed_motion_ok:
            failures.append("actual signed movement did not prove declared displacement and velocity")
        if max_cross_track > .10 + 1e-9:
            failures.append("steady cross-track deviation exceeded .10 blocks")
        if kind in {"yaw_sweep", "pitch_sweep"} and (
                max_look_error > .25 + 1e-9 or max_rate > 60.0 + 1e-6):
            failures.append("actual gaze precision or angular rate exceeded its bound")
        if kind == "yaw_sweep" and (not probe_yaws
                or min(probe_yaws) < -60.25 or max(probe_yaws) > 60.25
                or min(probe_yaws) > -59.75 or max(probe_yaws) < 59.75):
            failures.append("actual yaw sweep did not cover and remain within its declared range")
        if kind == "pitch_sweep" and (not probe_pitches
                or min(probe_pitches) < -30.25 or max(probe_pitches) > 30.25
                or min(probe_pitches) > -29.75 or max(probe_pitches) < 29.75):
            failures.append("actual pitch sweep did not cover and remain within its declared range")
        if kind in {"yaw_sweep", "pitch_sweep"}:
            expected_axis = "yaw" if kind == "yaw_sweep" else "pitch"
            if not any(axes == expected_axis and stage == "moving" and (forward or strafe)
                       for forward, strafe, axes, stage in verified_combinations):
                failures.append("moving gaze stage lacked an actual combined control sample")
            if (0, 0, expected_axis, "stationary_calibration") \
                    not in verified_combinations:
                failures.append("stationary gaze calibration lacked actual coverage")
        if kind in {"back", "release"} and (
                release_speed is None or release_speed > .03 + 1e-9):
            failures.append("release did not reach .03 blocks/tick within one second")
        if kind == "side" and not all(
                row["passed"] for row in side_release_windows):
            failures.append("each signed side release did not stop before the next movement")
        if kind == "release" and release_request_count != 10:
            failures.append("release probe did not consume ten measured single-tick inputs")
        result["checks"] |= {
            "actual_stage_identity": True,
            "canonical_guard_envelope": envelope_ok,
            "normal_state": normal_ok,
            "inactive_input_neutral": inactive_input_ok,
            "actual_signed_motion": signed_motion_ok,
            "signed_side_release_windows": (
                all(row["passed"] for row in side_release_windows)
                if kind == "side" else True),
        }
        result["engineering_status"] = "valid"
        result["capability_outcome"] = "failed" if failures else "passed"
        result["errors"].extend(failures)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        result["errors"].append(str(error))
    return result
