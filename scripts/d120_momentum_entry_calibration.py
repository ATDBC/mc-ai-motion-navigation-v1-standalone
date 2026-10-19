"""D120 finite entry calibration and read-only D118 evidence gate.

These diagnostic samples do not prove a continuous state envelope.  All physics
runs use frozen inputs and deterministic tick limits; wall time is reporting only.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import hashlib
import itertools
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.evidence.physics_validation import state_from_tick_evidence
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput
from mc2p.runtime.segmented_trace import iter_segmented_jsonl
from scripts import d118_momentum_calibration as evidence
from scripts import d118_momentum_calibration_runtime as frozen

AXES = ((-.002, 0., .002), (-.002, 0., .002),
        (-.0002, 0., .0002), (-.0002, 0., .0002), (-.1, 0., .1))
SUFFIX_LIMITS = {"landing-immediate": 42, "landing-immediate-late1": 42,
                 "fallback-sneak": 30}
DIRECTIONS = ((0, 1), (1, 0), (0, -1), (-1, 0))
NEUTRAL = (0., 0., False, False, False)
RUN = (1., 0., False, False, True)
JUMP = (1., 0., True, False, True)
SNEAK = (0., 0., False, True, False)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def build_manifest():
    trial = next(t for t in frozen.build_calibration_manifest(
        directions=(0,), repetitions=1).trials
        if t.trial_id == "landing-immediate-south-0")
    payload = {
        "schema_version": "mc2p.d120-finite-entry-manifest.v1",
        "ruleset": asdict(JAVA_1_21_RULESET),
        "axis_order": ["lateral_position", "longitudinal_position",
                       "lateral_velocity", "longitudinal_velocity", "yaw_degrees"],
        "axes": [list(a) for a in AXES],
        "offsets": [list(v) for v in itertools.product(*AXES)],
        "entry_phase": "first_landed_post_state",
        "initial_state": {
            "start_local": list(trial.start_local),
            "velocity_blocks_per_tick": [0., -.0784000015258789, 0.],
            "on_ground": True, "pose": "standing", "body_width": .6,
            "body_height": 1.8, "movement_speed_attribute": .1,
            "step_height_blocks": .6, "gravity_attribute": .08,
            "jump_strength_attribute": .42, "jumping_cooldown_ticks": 0,
            "sprinting": False, "sneaking": False,
        },
        "fixed_first_jump": [asdict(c) for c in trial.commands[:26]],
        "fixed_first_jump_sha256": digest([asdict(c) for c in trial.commands[:26]]),
        "world": {"first_gap_cells": 1, "middle_platform_cells": 1,
                  "second_gap_cells": 4, "support_material": "minecraft:grass_block",
                  "fixture_generator": "d118.trial_fixture_rows"},
        "suffix_limits": dict(SUFFIX_LIMITS),
        "suffix_inputs": {
            "landing-immediate": {"initial": [list(JUMP)], "flight": list(RUN), "settle": list(NEUTRAL)},
            "landing-immediate-late1": {"initial": [list(RUN), list(JUMP)], "flight": list(RUN), "settle": list(NEUTRAL)},
            "fallback-sneak": {"initial": [list(SNEAK)], "hold": list(SNEAK)},
        },
        "step_limits": {"per_direction": 30000, "four_directions": 120000},
        "settled_speed_exclusive": .003,
        "sound_envelope": "unverified",
    }
    return {**payload, "sha256": digest(payload)}


def validate_manifest(manifest):
    expected = build_manifest()
    if manifest != expected or digest({k: v for k, v in manifest.items() if k != "sha256"}) != manifest.get("sha256"):
        raise ValueError("D120 manifest differs from frozen definition")


@dataclass(frozen=True)
class Fixture:
    trial: object
    context: object
    sha256: str


@lru_cache(maxsize=4)
def build_fixture(direction_index=0):
    if type(direction_index) is not int or direction_index not in range(4):
        raise ValueError("D120 direction is invalid")
    manifest = frozen.build_calibration_manifest(directions=(direction_index,), repetitions=1)
    trial = next(t for t in manifest.trials if t.trial_id.startswith("landing-immediate-")
                 and "late1" not in t.trial_id)
    rows = frozen.trial_fixture_rows(trial, origin=(0, 0), feet_y=64)
    context = evidence.declared_trial_fixture_contexts(
        replace(manifest, trials=(trial,)), rows)[trial.trial_id]
    return Fixture(trial, context, digest(rows))


def _axes(state, fixture):
    dx, dz = DIRECTIONS[fixture.trial.direction_index]
    origin = fixture.context.origin
    px, _, pz = state.position
    vx, _, vz = state.velocity_blocks_per_tick
    return ((px-origin[0])*dz-(pz-origin[1])*dx,
            (px-origin[0])*dx+(pz-origin[1])*dz,
            vx*dz-vz*dx, vx*dx+vz*dz)


def yaw_offset_degrees(actual, nominal):
    """Return the shortest signed yaw difference, including +/-pi equivalence."""
    return math.degrees(math.remainder(actual-nominal, math.tau))


def _supported(state, fixture, span):
    x, z, _, _ = _axes(state, fixture)
    low_x, high_x = fixture.context.platform_x_bounds
    half = state.body_width / 2
    return (state.on_ground and abs(state.position[1]-fixture.context.feet_y) < 1e-7
            and min(x+half, high_x)-max(x-half, low_x) > 0
            and min(z+half, span[1])-max(z-half, span[0]) > 0)


def _input(values, yaw):
    return TickInput(*values, yaw)


def nominal_entry(fixture):
    trial, context = fixture.trial, fixture.context
    start_x, start_z = frozen._local_to_world(context.origin, trial.direction_index,
                                             trial.start_local[0], trial.start_local[2])
    row = frozen.physics_sidecar_row(episode_id="d120-offline", request_sequence_id=1, movement_tick_id=1)
    pre = row["pre_state"]
    pre["position"] = dict(zip(("x", "y", "z"), (start_x, context.feet_y, start_z)))
    # A settled vanilla grounded post-state retains the gravity/drag Y velocity.
    # Zero would fabricate a one-tick loss of grounding before the run-up.
    pre["velocity"] = {"x": 0., "y": -.0784000015258789, "z": 0.}
    pre["yaw"] = frozen._DIRECTIONS[trial.direction_index][2]
    current = state_from_tick_evidence(row, context.world, JAVA_1_21_RULESET)
    landed = []
    for command in trial.commands[:26]:
        result = physics_step(current, _input((command.forward, command.strafe,
            command.jump, command.sneak, command.sprint), current.yaw_radians),
            context.world, JAVA_1_21_RULESET)
        if result.status is not CalculationStatus.OK:
            raise ValueError("D120 nominal prefix lacks complete physics")
        current = result.next_state
        if "landed" in result.events:
            landed.append(current)
    if len(landed) != 1 or landed[0] != current or not current.on_ground:
        raise ValueError("D120 nominal entry must be first landed post-state")
    return current


def perturb_entry(entry, offsets, direction_index):
    dx, dz = DIRECTIONS[direction_index]
    lateral, longitudinal, lateral_v, longitudinal_v, yaw = offsets
    px, py, pz = entry.position
    vx, vy, vz = entry.velocity_blocks_per_tick
    return replace(entry,
        position=(px+lateral*dz+longitudinal*dx, py, pz-lateral*dx+longitudinal*dz),
        velocity_blocks_per_tick=(vx+lateral_v*dz+longitudinal_v*dx, vy,
                                   vz-lateral_v*dx+longitudinal_v*dz),
        yaw_radians=entry.yaw_radians+math.radians(yaw))


def replay_suffix(entry, fixture, suffix):
    if suffix not in (*SUFFIX_LIMITS, "fallback-neutral", "fallback-back"):
        raise ValueError("D120 suffix is not frozen")
    fallback = suffix.startswith("fallback-")
    delay = 2 if suffix == "landing-immediate-late1" else 1
    limit = SUFFIX_LIMITS.get(suffix, 30)
    middle = (fixture.trial.first_gap_cells,
              fixture.trial.first_gap_cells+fixture.trial.middle_platform_cells)
    target_start = sum((fixture.trial.first_gap_cells, fixture.trial.middle_platform_cells,
                        fixture.trial.second_gap_cells))
    target = (target_start, target_start+6)
    current = entry
    took_off = landed = False
    damage = 0.
    trace = []
    reason = "tick_limit"
    passed = False
    for index in range(limit):
        if fallback:
            values = {"fallback-sneak": SNEAK, "fallback-neutral": NEUTRAL,
                      "fallback-back": (-1., 0., False, False, False)}[suffix]
        elif index < delay-1:
            values = RUN
        elif index == delay-1:
            values = JUMP
        else:
            values = NEUTRAL if landed else RUN
        result = physics_step(current, _input(values, entry.yaw_radians),
                              fixture.context.world, JAVA_1_21_RULESET)
        if result.status is not CalculationStatus.OK:
            reason = result.status.value
            trace.append({"tick": current.movement_tick_id+1, "status": reason})
            break
        prior = current
        current = result.next_state
        if "landed" in result.events:
            damage += max(0., math.ceil(prior.fall_distance_blocks-3.))
        x, z, _, _ = _axes(current, fixture)
        span = middle if fallback else target
        longitudinal_overlap = min(z+.3, span[1])-max(z-.3, span[0])
        support = _supported(current, fixture, span)
        trace.append({"tick": current.movement_tick_id, "on_ground": current.on_ground,
                      "position": list(current.position),
                      "velocity": list(current.velocity_blocks_per_tick),
                      "events": list(result.events), "supported": support,
                      "longitudinal_footprint_overlap": longitudinal_overlap})
        if damage:
            reason = "damage"
            break
        if fallback:
            if not support:
                reason = "middle_platform_support_lost"
                break
            if math.hypot(current.velocity_blocks_per_tick[0], current.velocity_blocks_per_tick[2]) < .003:
                passed, reason = True, "settled_on_middle_platform"
                break
        else:
            if index == delay-1:
                took_off = "left_ground" in result.events and not current.on_ground
                if not took_off:
                    reason = "jump_did_not_take_off"
                    break
            if took_off and "landed" in result.events:
                if not support:
                    reason = "landed_on_wrong_support"
                    break
                landed = True
            if landed and not support:
                reason = "target_support_lost_during_settle"
                break
            if landed and math.hypot(current.velocity_blocks_per_tick[0], current.velocity_blocks_per_tick[2]) < .003:
                passed, reason = True, "landed_and_settled_on_target"
                break
            if current.position[1] < fixture.context.feet_y-3:
                reason = "fell_below_platform"
                break
    return {"suffix": suffix, "passed": passed, "reason": reason, "steps": len(trace),
            "damage": damage, "took_off": took_off, "landed_on_target": landed,
            "first_effect_relative_to_landing": delay,
            "first_effect_tick": entry.movement_tick_id+delay,
            "transport_late_injected": False, "trace": trace,
            "final_state": current.to_mapping(),
            "minimum_support_overlap": min((t["longitudinal_footprint_overlap"] for t in trace
                if "longitudinal_footprint_overlap" in t), default=None)}


def scan_direction(manifest, direction_index=0):
    validate_manifest(manifest)
    fixture = build_fixture(direction_index)
    entry = nominal_entry(fixture)
    samples = []
    steps = 0
    for point_index, offsets in enumerate(manifest["offsets"]):
        shifted = perturb_entry(entry, offsets, direction_index)
        # Independently derive offsets from the state actually sent to physics.
        before, after = _axes(entry, fixture), _axes(shifted, fixture)
        applied = [*(a-b for a, b in zip(after, before)),
                   math.degrees(shifted.yaw_radians-entry.yaw_radians)]
        if any(abs(a-b)>1e-12 for a,b in zip(applied, offsets)):
            raise ValueError("D120 reported perturbation was not applied to physics")
        suffixes = {suffix: replay_suffix(shifted, fixture, suffix) for suffix in SUFFIX_LIMITS}
        steps += sum(s["steps"] for s in suffixes.values())
        if steps > manifest["step_limits"]["per_direction"]:
            raise ValueError("D120 frozen per-direction step budget exhausted")
        samples.append({"point_index": point_index, "offsets": list(offsets),
                        "applied_offsets": applied, "entry": shifted.to_mapping(), "suffixes": suffixes})
    controls = {suffix: replay_suffix(entry, fixture, suffix)
                for suffix in ("fallback-neutral", "fallback-back")}
    return {"schema_version": "mc2p.d120-finite-entry-coverage.v1",
            "manifest_sha256": manifest["sha256"], "direction_index": direction_index,
            "fixture_sha256": fixture.sha256, "nominal_entry": entry.to_mapping(),
            "sample_coverage": "passed" if all(s["passed"] for p in samples for s in p["suffixes"].values()) else "failed",
            "sound_envelope": "unverified", "main_physics_steps": steps,
            "nominal_prefix_physics_steps": 26,
            "negative_control_physics_steps": sum(s["steps"] for s in controls.values()),
            "negative_controls": controls, "samples": samples}


def validate_coverage(manifest, coverage):
    validate_manifest(manifest)
    points = coverage.get("samples", [])
    if (coverage.get("manifest_sha256") != manifest["sha256"]
            or coverage.get("sound_envelope") != "unverified" or len(points) != 243
            or [p.get("offsets") for p in points] != manifest["offsets"]
            or [p.get("point_index") for p in points] != list(range(243))):
        raise ValueError("D120 coverage point identity is incomplete or duplicate")
    for point in points:
        nominal = coverage["nominal_entry"]
        direction = coverage["direction_index"]
        # Verify recorded physical state independently of self-reported offsets.
        dx, dz = DIRECTIONS[direction]
        p, v = point["entry"]["position"], point["entry"]["velocity_blocks_per_tick"]
        np, nv = nominal["position"], nominal["velocity_blocks_per_tick"]
        measured = [(p[0]-np[0])*dz-(p[2]-np[2])*dx,
                    (p[0]-np[0])*dx+(p[2]-np[2])*dz,
                    (v[0]-nv[0])*dz-(v[2]-nv[2])*dx,
                    (v[0]-nv[0])*dx+(v[2]-nv[2])*dz,
                    math.degrees(point["entry"]["yaw_radians"]-nominal["yaw_radians"])]
        if any(abs(a-b)>1e-12 for a,b in zip(measured,point["offsets"])):
            raise ValueError("D120 coverage physical state differs from reported offset")
        movable = {"position", "velocity_blocks_per_tick", "yaw_radians"}
        if (any(point["entry"].get(key) != value for key,value in nominal.items()
                if key not in movable)
                or p[1] != np[1] or v[1] != nv[1]):
            raise ValueError("D120 coverage altered a bound landed-state field")
        if (any(abs(a-b)>1e-12 for a,b in zip(point["applied_offsets"], point["offsets"]))
                or set(point["suffixes"]) != set(SUFFIX_LIMITS)):
            raise ValueError("D120 coverage perturbation or suffix differs")
        for name, result in point["suffixes"].items():
            if (result["suffix"] != name or result["steps"] > SUFFIX_LIMITS[name]
                    or result["first_effect_relative_to_landing"] != (2 if name.endswith("late1") else 1)):
                raise ValueError("D120 coverage suffix timing differs")
    if coverage["sample_coverage"] != "passed" or not all(
            s["passed"] for p in points for s in p["suffixes"].values()):
        raise ValueError("D120 coverage contains failed frozen suffixes")
    counted = sum(s["steps"] for p in points for s in p["suffixes"].values())
    if counted != coverage["main_physics_steps"] or counted > manifest["step_limits"]["per_direction"]:
        raise ValueError("D120 coverage physics-step budget differs")
    if any(c["passed"] for c in coverage["negative_controls"].values()):
        raise ValueError("D120 coverage negative control unexpectedly safe")


def landed_post_state(row, world):
    if ("landed" not in row.get("contact_events", ())
            or row.get("pre_state", {}).get("on_ground") is not False
            or row.get("post_state", {}).get("on_ground") is not True):
        raise ValueError("D120 entry requires authoritative first landed post-state")
    state = state_from_tick_evidence({**row, "pre_state": row["post_state"],
        "movement_tick_id": int(row["movement_tick_id"])+1}, world, JAVA_1_21_RULESET)
    return state


def raw_gate_passed(raw_report):
    return (raw_report.get("identity") == "passed"
            and raw_report.get("physics_replay", {}).get("status") == "passed"
            and raw_report.get("raw_hashes_unchanged") is True
            and bool(raw_report.get("entries"))
            and all(e.get("within_registered_bounds") is True
                    and e.get("entry_phase") == "landed_post_state"
                    and set(e.get("raw_suffix_replay", {})) == set(SUFFIX_LIMITS)
                    and all(s.get("passed") is True for s in e["raw_suffix_replay"].values())
                    for e in raw_report["entries"]))


def _raw_hashes(source):
    client = source / "client-0"
    paths = [source / "result.json", *(client/name for name in (
        "d118-input-manifest.json", "d118-calibration-runtime.json",
        "d118-fixture-commands.jsonl", "d118-runtime-receipts.jsonl"))]
    for directory in ("physics-tick-events", "ftick-events"):
        paths.extend(p for p in (client/directory).rglob("*") if p.is_file())
    return {str(p.relative_to(source)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


def analyze_raw(run_dir):
    source = Path(run_dir).resolve()
    before_hashes = _raw_hashes(source)
    identity = evidence.analyze_timeline_identity(source)
    client = source/"client-0"
    manifest = evidence.validate_manifest_document(json.loads((client/"d118-input-manifest.json").read_text(encoding="utf-8")))
    receipts = evidence._jsonl(client/"d118-runtime-receipts.jsonl")
    fixtures = evidence._jsonl(client/"d118-fixture-commands.jsonl")
    rows = tuple(iter_segmented_jsonl(client/"physics-tick-events"))
    contexts = evidence.declared_trial_fixture_contexts(manifest, fixtures)
    indexed = {(r["episode_id"], r["request_sequence_id"], r["movement_tick_id"]): r
               for r in rows if r.get("schema_version") == "mc2p.client-physics-tick.v1"}
    entries = []
    for trial in manifest.trials:
        if not (trial.trial_id.startswith("landing-immediate-") or trial.trial_id.startswith("fallback-")):
            continue
        selected = sorted((r for r in receipts if r["trial_id"] == trial.trial_id), key=lambda r:r["command_index"])
        ordered = [indexed[(r["episode_id"], r["request_sequence_id"], r["movement_tick_id"])] for r in selected]
        landing_index = next((i for i,r in enumerate(ordered) if "landed" in r.get("contact_events", ())), None)
        if landing_index != 25:
            raise ValueError("D120 raw first landing is not frozen command 25")
        context = contexts[trial.trial_id]
        fixture = Fixture(trial, context, digest([r for r in fixtures if r["trial_id"] == trial.trial_id]))
        actual = landed_post_state(ordered[landing_index], context.world)
        nominal = nominal_entry(fixture)
        a, n = _axes(actual, fixture), _axes(nominal, fixture)
        offsets = [*(x-y for x,y in zip(a,n)), yaw_offset_degrees(actual.yaw_radians,nominal.yaw_radians)]
        margins = [max(axis)-abs(value) for axis,value in zip(AXES,offsets)]
        immutable = (actual.on_ground == nominal.on_ground and actual.pose == nominal.pose
                     and actual.sprinting == nominal.sprinting and actual.sneaking == nominal.sneaking
                     and actual.horizontal_collision == nominal.horizontal_collision
                     and actual.vertical_collision == nominal.vertical_collision
                     and abs(actual.position[1]-nominal.position[1]) <= .001
                     and abs(actual.velocity_blocks_per_tick[1]-nominal.velocity_blocks_per_tick[1]) <= .0001
                     and _supported(actual,fixture,(1,2)))
        suffixes = {s: replay_suffix(actual,fixture,s) for s in SUFFIX_LIMITS}
        timing = evidence._event_timing(trial,tuple(ordered))
        expected_relative = 2 if "late1" in trial.trial_id else 1
        observed_relative = (timing["sneak_relative_to_landing"] if trial.fallback == "sneak"
                             else timing["second_jump_relative_to_landing"])
        if trial.fallback in ("neutral", "back"):
            observed_relative = selected[26]["movement_tick_id"]-actual.movement_tick_id
        if observed_relative != expected_relative:
            raise ValueError("D120 raw L+1/L+2 input timing identity differs")
        entries.append({"trial_id":trial.trial_id, "landing_command_index":landing_index,
                        "landing_movement_tick_id":actual.movement_tick_id,
                        "entry_phase":"landed_post_state", "actual_entry":actual.to_mapping(),
                        "offsets_from_nominal":offsets, "registered_bound_margins":margins,
                        "remaining_after_calibration_budget":[margins[0]-.001,margins[1]-.001,
                            margins[2]-.0001,margins[3]-.0001],
                        "within_registered_bounds":immutable and all(m>=-1e-12 for m in margins),
                        "raw_suffix_replay":suffixes, "observed_suffix_relative_tick":observed_relative})
    after_hashes = _raw_hashes(source)
    physics = evidence.analyze_physics_replay(manifest,receipts,rows,fixtures)
    result = {"schema_version":"mc2p.d120-raw-entry-gate.v1", "identity":"passed",
              "identity_command_count":len(identity), "physics_replay":physics,
              "source_sha256":before_hashes, "raw_hashes_unchanged":before_hashes==after_hashes,
              "entries":entries,
              "raw_suffix_physics_steps":sum(s["steps"] for e in entries for s in e["raw_suffix_replay"].values()),
              "raw_physics_replay_steps":physics.get("tick_count",0)*3,
              "sound_envelope":"unverified"}
    result["status"] = "passed" if raw_gate_passed(result) else "failed"
    return result


def _write(path, value):
    path.write_text(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8",newline="\n")


def write_result_index(directory):
    """Seal deterministic results; elapsed wall time is diagnostic only."""
    directory = Path(directory)
    summary = json.loads((directory/"summary.json").read_text(encoding="utf-8"))
    semantic_summary = {key:value for key,value in summary.items() if key != "elapsed_seconds"}
    names = ["manifest.json", *(f"coverage-{n}.json" for n in range(summary["directions"]))]
    if (directory/"old-raw-gate.json").is_file():
        names.append("old-raw-gate.json")
    result = {"schema_version":"mc2p.d120-offline-index.v1", "status":summary["status"],
              "manifest_sha256":summary["manifest_sha256"], "summary":semantic_summary,
              "files":{name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in names}}
    _write(directory/"index.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True,exist_ok=False)
    start = time.perf_counter()
    manifest = build_manifest()
    _write(args.output/"manifest.json",manifest)
    reports = []
    failure = None
    for direction in range(4):
        report = scan_direction(manifest,direction)
        reports.append(report)
        _write(args.output/f"coverage-{direction}.json",report)
        try:
            validate_coverage(manifest,report)
        except ValueError as error:
            failure = str(error)
            break
    steps = sum(r["main_physics_steps"] for r in reports)
    raw = None
    if failure is None and steps <=120000:
        raw = analyze_raw(args.run_dir)
        _write(args.output/"old-raw-gate.json",raw)
        if raw["status"] != "passed":
            failure = "D120 old raw gate failed"
    summary = {"schema_version":"mc2p.d120-offline-summary.v1",
               "status":"failed" if failure else "passed", "failure":failure,
               "manifest_sha256":manifest["sha256"], "directions":len(reports),
               "sample_count":sum(len(r["samples"]) for r in reports),
               "suffix_count":sum(len(p["suffixes"]) for r in reports for p in r["samples"]),
               "main_physics_steps":steps, "four_direction_limit":120000,
               "sound_envelope":"unverified", "elapsed_seconds":time.perf_counter()-start,
               "raw_gate_status":raw["status"] if raw else "not_run",
               "scope":"finite_samples_only_not_production_permission"}
    _write(args.output/"summary.json",summary)
    write_result_index(args.output)
    print(json.dumps(summary,sort_keys=True))
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
