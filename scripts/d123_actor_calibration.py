"""D123-1 read-only fixed-eye stop probe; no live calibration or movement API."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts import d122_fabric_timing as d122
from scripts.control_probe_core import write_json_atomic

MAX_CALIBRATION_TICKS = 80
MAX_CALIBRATION_WALL_NS = 30_000_000_000
# Proposed live bounds are recorded only as unexecuted contract scaffolding.
# The stop rule below prevents delivery of a live calibration entry.


def calibration_request(direction, *, origin, feet_y):
    from mc2p.contracts.observation_request_v3 import ObservationRequestV3
    from scripts.d120_momentum_entry_calibration import build_fixture
    return ObservationRequestV3("navigation_v1", d122.fabric_observation_positions(
        build_fixture(direction).trial, direction_index=direction, origin=origin, feet_y=feet_y))


def inspect_actor_gate(world, manifest):
    result = d122.inspect_fabric_dependency_knowledge(world, manifest)
    unknown, mismatch = len(result["unknown"]), len(result["mismatched"])
    status = ("NEEDS_WORLD_AND_FACT_MISMATCH" if unknown and mismatch else
              "NEEDS_WORLD" if unknown else "FACT_MISMATCH" if mismatch else "PASS")
    return {**result, "unknown_count": unknown, "mismatch_count": mismatch, "status": status}


def static_gate(body, health):
    failures = []
    if not body.is_on_ground:
        failures.append("not_grounded")
    if math.hypot(body.velocity_blocks_per_second[0], body.velocity_blocks_per_second[2]) > .03:
        failures.append("not_still")
    if health is None or body.health_points is None or body.health_points != health:
        failures.append("health_changed")
    return tuple(failures)


def within_bounds(tick, wall_ns, start_tick, start_wall_ns):
    return (type(tick) is int and 0 <= tick - start_tick < MAX_CALIBRATION_TICKS
            and 0 <= wall_ns - start_wall_ns < MAX_CALIBRATION_WALL_NS)


def dispatch(mode, offline, launch):
    """Structural preflight ordering seam only; this probe has no live launcher."""
    # Product modes intentionally have no implementation before D123-2 sign off.
    if mode != "calibration-only" or offline.get("status") != "PASS":
        return 2
    return launch()


def preflight_then_dispatch(mode, preflight, launch):
    return dispatch(mode, preflight(), launch)


def support_plane_blocks_cell(eye, cell, support_rect, support_y):
    """All rays to the candidate cube cross a known opaque support rectangle.

    At the support plane, each horizontal coordinate is a linear fractional
    function of target Y and a linear function of target X/Z. Its extrema on
    the cube occur at vertices. This tests the full cube, not a center ray.
    """
    if not eye[1] > support_y > cell[1] + 1:
        return False
    intersections = []
    for x in (cell[0], cell[0] + 1):
        for y in (cell[1], cell[1] + 1):
            for z in (cell[2], cell[2] + 1):
                t = (support_y - eye[1]) / (y - eye[1])
                intersections.append((eye[0] + t * (x - eye[0]),
                                      eye[2] + t * (z - eye[2])))
    x0, z0, x1, z1 = support_rect
    return all(x0 < x < x1 and z0 < z < z1 for x, z in intersections)


def fixed_eye_geometry(direction):
    """Use detached fixture geometry only to diagnose observation infeasibility."""
    from scripts import d118_momentum_calibration_runtime as frozen
    from scripts.d120_momentum_entry_calibration import build_fixture
    fixture = build_fixture(direction)
    support = next(row for row in frozen.trial_fixture_rows(
        fixture.trial, origin=(0, 0), feet_y=64) if row["phase"] == "support")["positions"]
    positions = [tuple(p) for p in support]
    data = {"boxes": [(*p, p[0] + 1, p[1] + 1, p[2] + 1) for p in positions],
            "centers": [(p[0] + .5, p[1] + .5, p[2] + .5) for p in positions],
            "owners": list(range(len(positions))), "opaque": [1] * len(positions)}
    start_x, start_z = frozen._local_to_world((0, 0), direction,
        fixture.trial.start_local[0], fixture.trial.start_local[2])
    # Vanilla standing eye height; diagnosis is cross-checked against v8 actor
    # UNKNOWN facts, not used as actor knowledge or a production proof.
    eye = (start_x, 65.62, start_z)
    manifest = d122.fabric_dependency_manifest(direction, origin=(0, 0), feet_y=64)
    lower = tuple(sorted(p for p in manifest["facts"] if p[1] == 62))
    rectangles = []
    for local_low, local_high in ((-12, 0), (1, 2), (6, 12)):
        corners = [frozen._local_to_world((0, 0), direction, x, z)
                   for x in (-1, 2) for z in (local_low, local_high)]
        rect = (min(x for x, _ in corners), min(z for _, z in corners),
                max(x for x, _ in corners), max(z for _, z in corners))
        # Derive confidence from all actual declared unit support cells; the
        # rectangle alone must never invent a blocker in either gap.
        covered = {(x, 63, z) for x in range(round(rect[0]), round(rect[2]))
                   for z in range(round(rect[1]), round(rect[3]))}
        if covered.issubset(set(positions)):
            rectangles.append(rect)
    blockers = {p: tuple(rect for rect in rectangles if support_plane_blocks_cell(eye, p, rect, 64))
                for p in lower}
    return {"eye": eye, "data": data, "required": tuple(sorted(manifest["facts"])),
            "lower": lower, "continuous_blocked": tuple(p for p in lower if blockers[p]),
            "blockers": [{"position": p, "support_rectangles": blockers[p]}
                         for p in lower if blockers[p]]}


def readonly_probe(output):
    """Bounded Windows native angular scan plus an orientation-independent blocker."""
    if os.name != "nt":
        raise RuntimeError("Windows is the formal platform")
    if output.exists():
        raise FileExistsError("append-only evidence")
    output.mkdir(parents=True)
    from scripts.surface_depth_probe.native import Cache
    historical = ROOT / "evidence/motion_navigation/d122-client-timing-v1/d122-4/fabric-smoke-v8/observation-stop.json"
    old = json.loads(historical.read_text("utf-8"))
    if (old["required_count"], old["known_count"], old["unknown_count"], old["mismatch_count"]) != (67, 55, 12, 0):
        raise ValueError("historical actor stop identity differs")
    historical_hash = hashlib.sha256(historical.read_bytes()).hexdigest()
    trace = Path(old["source_run"]) / "client-0/trace.jsonl"
    trace_hash = hashlib.sha256(trace.read_bytes()).hexdigest()
    if trace_hash != old["trace_sha256"]:
        raise ValueError("historical actor trace hash differs")
    observations = [record["payload"]["backend_result"]["observation"]
                    for record in (json.loads(line) for line in trace.read_text("utf-8").splitlines())
                    if record["record_type"] == "step"]
    last = observations[-1]
    last_position = last["self_state"]["value"]["position"]
    stable = [o for o in observations if o["self_state"]["value"]["position"] == last_position]
    unknown = {tuple(p) for p in old["unknown_positions"]}
    raw_union = {tuple(block["position"]) for o in stable for block in o["perception"]["value"]["blocks"]}
    if raw_union & unknown:
        raise ValueError("historical multi-frame actor blocks unexpectedly resolve a lower cell")
    started = time.perf_counter_ns()
    directions = []
    for direction in range(4):
        geometry = fixed_eye_geometry(direction)
        samples, visible = [], set()
        with Cache() as cache:
            cache.update(geometry["data"])
            for yaw in range(-180, 180, 15):
                for pitch in range(-90, 91, 15):
                    if time.perf_counter_ns() - started > 60_000_000_000:
                        raise TimeoutError("READONLY_NATIVE_PROBE_BOUND_EXCEEDED")
                    _, air, _, _ = cache.frame_pose_air((*geometry["eye"], yaw, pitch), geometry["lower"])
                    states = tuple(int(n) for n in air)
                    visible.update(p for p, status in zip(geometry["lower"], states) if status in (1, 4))
                    samples.append({"yaw": yaw, "pitch": pitch, "statuses": states})
        persistent = tuple(p for p in geometry["lower"] if p not in visible)
        if not geometry["continuous_blocked"] or not set(geometry["continuous_blocked"]).issubset(persistent):
            raise AssertionError("continuous blocker not confirmed by existing native core")
        directions.append({"direction": direction, "required_count": len(geometry["required"]),
            "required": geometry["required"], "lower": geometry["lower"], "eye": geometry["eye"],
            "continuous_blockers": geometry["blockers"], "never_visible_in_scan": persistent,
            "visible_in_scan": tuple(sorted(visible)), "samples": samples})
    files = ("deployment/surface-depth-diagnostic/native/geometry.hpp",
             "mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceSensor.java",
             "mc2p/backends/runtime_overlays/mc121_observation/ClientBlockObservationV3.java",
             "scripts/d122_fabric_timing.py", "scripts/d123_actor_calibration.py",
             "artifacts/surface-cache/native-build/Release/surface_cache.dll")
    result = {"schema": "mc2p.d123-1-fixed-eye-stop.v1", "status": "PARTIAL_STOPPED",
        "reason": "FIXED_EYE_OCCLUSION_REQUIRES_VIEWPOINT_PLANNING_OR_NEW_SENSING",
        "platform": platform.platform(), "python": sys.version,
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "historical_actor": {"path": historical.relative_to(ROOT).as_posix(), "sha256": historical_hash,
            "required": old["required_count"], "known": old["known_count"], "unknown": old["unknown_count"],
            "mismatch": old["mismatch_count"], "air_results": old["air_query_results"],
            "trace_sha256": trace_hash, "stable_sequences": [o["sequence_id"] for o in stable],
            "stable_position": last_position, "standing_pose": last["self_state"]["value"]["pose"],
            "lower_cells_present_in_stable_raw_block_union": [],
            "raw_union_check_scope": "Presence check only, not formal WorldModel admission or new actor calibration"},
        "directions": directions, "native_bound": {"wall_ns": 60_000_000_000,
            "max_evaluations": 4 * 24 * 13, "movement_ticks": 0},
        "elapsed_wall_ns": time.perf_counter_ns() - started,
        "production_semantics": d122._production_semantic_binding(),
        "files": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files},
        "new_fabric_runs": 0, "live_actor_calibration": "NOT_RUN_STOP_RULE",
        "movement_trials": 0, "D123_2": "NOT_STARTED",
        "two_phase_command": "NOT_DELIVERED_STOP_RULE",
        "limits": ["Detached fixture/native diagnosis is not new actor knowledge or Runtime admission.",
            "Finite angular scan alone does not prove all orientations; support-plane argument covers each listed continuous blocker.",
            "The historical 67/67 offline fixture gate is not actor calibration.",
            "No fixture facts are ingested into WorldModel; no viewpoint is selected or teleported."]}
    write_json_atomic(output / "stop.json", result)
    print(json.dumps({"status": result["status"], "directions": [
        {"direction": row["direction"], "continuous_blocked": len(row["continuous_blockers"]),
         "never_visible": len(row["never_visible_in_scan"])} for row in directions],
        "new_fabric_runs": 0, "movement_trials": 0}))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    readonly_probe(args.output)
    return 2  # Stop is an evidence result, never calibration success.


if __name__ == "__main__":
    raise SystemExit(main())

