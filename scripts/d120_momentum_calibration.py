"""D120 diagnostic calibration: sealed prerequisites and the shared D118 loop."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts import d118_momentum_calibration as evidence
from scripts import d118_momentum_calibration_runtime as frozen
from scripts import d120_momentum_entry_calibration as entry
from scripts.control_probe_core import write_json_atomic, write_jsonl_atomic
from mc2p.runtime.segmented_trace import iter_segmented_jsonl

OFFLINE_MANIFEST_SHA = "89c5ee269da0b8ac969d69be781a589c8f28449faf0b153fe69a6b2c2a433dd6"
FROZEN_MANIFESTS = {
    "0ee556cbd682b3692066dc0371b4936c0abe43c125c37f6629368df878e66f18": (3,123),
    "9c54a00e635ccabd4224c79255645b03cafeab51332e669800fc10dc9f2e75c0": (36,1476),
}
OWNED_DIRECTORY = "d120-calibration"


def build_manifest(*, full_matrix=False):
    manifest = frozen.build_calibration_manifest(
        directions=(0,1,2,3) if full_matrix else (0,), repetitions=3 if full_matrix else 1)
    manifest = replace(manifest, schema_version="mc2p.d120-calibration-manifest.v1", trials=tuple(
        trial for trial in manifest.trials if trial.trial_id.startswith(
            ("landing-immediate-", "fallback-sneak-"))))
    return replace(manifest, sha256=frozen.manifest_sha256(manifest))


def manifest_document(manifest):
    return {**frozen._manifest_payload(manifest), "sha256":manifest.sha256}


def validate_manifest_document(document):
    manifest = evidence._manifest_from_document(document)
    if (manifest.schema_version != "mc2p.d120-calibration-manifest.v1"
            or frozen.manifest_sha256(manifest) != manifest.sha256
            or entry.digest({k:v for k,v in document.items() if k != "sha256"}) != manifest.sha256
            or FROZEN_MANIFESTS.get(manifest.sha256) != (
                len(manifest.trials), sum(len(t.commands) for t in manifest.trials))):
        raise ValueError("D120 manifest identity or shape differs")
    return manifest


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError("D120 prerequisite or evidence must be an object")
    return value


def validate_offline_prerequisite(directory):
    directory = Path(directory)
    try:
        index = _read(directory/"index.json")
        summary = _read(directory/"summary.json")
        if (index.get("schema_version") != "mc2p.d120-offline-index.v1"
                or index.get("status") != "passed"
                or index.get("manifest_sha256") != OFFLINE_MANIFEST_SHA
                or index.get("summary") != {k:v for k,v in summary.items() if k != "elapsed_seconds"}
                or summary.get("status") != "passed"
                or summary.get("raw_gate_status") != "passed"
                or summary.get("sound_envelope") != "unverified"
                or (summary.get("directions"),summary.get("sample_count"),summary.get("suffix_count")) != (4,972,2916)):
            raise ValueError("offline summary identity or coverage differs")
        names = {"manifest.json", "old-raw-gate.json", *(f"coverage-{n}.json" for n in range(4))}
        if set(index.get("files", {})) != names or any(
                _hash(directory/name) != index["files"][name] for name in names):
            raise ValueError("offline prerequisite file hash differs")
        manifest = _read(directory/"manifest.json")
        entry.validate_manifest(manifest)
        for direction in range(4):
            report = _read(directory/f"coverage-{direction}.json")
            if report.get("direction_index") != direction:
                raise ValueError("offline direction differs")
            entry.validate_coverage(manifest,report)
        if not entry.raw_gate_passed(_read(directory/"old-raw-gate.json")):
            raise ValueError("offline raw prerequisite failed")
        return _hash(directory/"index.json")
    except (OSError,KeyError,TypeError,ValueError) as error:
        raise ValueError(f"D120 offline prerequisite invalid: {error}") from error


def validate_south_prerequisite(directory, offline_hash):
    directory = Path(directory)
    try:
        index = _read(directory/"index.json")
        if (index.get("schema_version") != "mc2p.d120-calibration-index.v1"
                or index.get("status") != "passed" or index.get("full_matrix") is not False
                or index.get("offline_prerequisite_sha256") != offline_hash):
            raise ValueError("south prerequisite status or identity differs")
        expected = {"summary.json","analysis.json","command.json","probe-stdout.txt","probe-stderr.txt"}
        if set(index.get("files",{})) != expected or any(
                _hash(directory/name) != index["files"][name] for name in expected):
            raise ValueError("south prerequisite file hash differs")
        summary = _read(directory/"summary.json")
        if (summary.get("acceptance_status") != "passed"
                or summary.get("capture_status") != "captured"
                or summary.get("manifest_sha256") != build_manifest().sha256
                or summary.get("offline_prerequisite_sha256") != offline_hash):
            raise ValueError("south prerequisite summary differs")
        analysis = _read(directory/"analysis.json")
        expected_trials = {trial.trial_id for trial in build_manifest().trials}
        if (analysis.get("schema_version") != "mc2p.d120-calibration-analysis.v1"
                or analysis.get("status") != "passed"
                or analysis.get("manifest_sha256") != build_manifest().sha256
                or analysis.get("joined_command_count") != 123
                or len(analysis.get("entries",())) != 3
                or {row.get("trial_id") for row in analysis.get("entries",())} != expected_trials
                or analysis.get("health_evidence",{}).get("status") != "passed"
                or not isinstance(analysis.get("responsibility_cutoff"),dict)
                or type(analysis.get("post_release_tail_count")) is not int
                or analysis.get("post_release_tail_count",-1)<0
                or analysis.get("post_release_tail_status") != "passed"):
            raise ValueError("south prerequisite analysis failed or incomplete")
        source = Path(summary["source_run"])
        raw = summary.get("raw_files",{})
        required = {"result.json",*(f"client-0/{OWNED_DIRECTORY}/{name}" for name in (
            "d118-input-manifest.json","d118-calibration-runtime.json","d118-runtime-receipts.jsonl",
            "d118-fixture-commands.jsonl","d120-health-observations.jsonl")),
            *(f"client-0/{stream}/{name}" for stream in ("physics-tick-events","ftick-events")
                for name in ("manifest.jsonl","complete.json"))}
        if not required.issubset(raw) or any(not any(
                name.startswith(f"client-0/{stream}/segment-") and name.endswith(".jsonl") for name in raw)
                for stream in ("physics-tick-events","ftick-events")):
            raise ValueError("south prerequisite necessary raw coverage missing")
        for name in raw:
            (source/name).resolve().relative_to(source.resolve())
        if any(_hash(source/name) != expected_hash for name,expected_hash in raw.items()):
            raise ValueError("south prerequisite raw hash differs")
        if (_read(source/"result.json").get("status") != "passed"
                or analyze_run(source) != analysis):
            raise ValueError("south prerequisite raw reanalysis differs")
        return _hash(directory/"index.json")
    except (OSError,KeyError,TypeError,ValueError) as error:
        raise ValueError(f"D120 south prerequisite invalid: {error}") from error


def fabric_probe_command(*, server_port, ipc_port, full_matrix=False):
    command = evidence.fabric_probe_command(server_port=server_port,ipc_port=ipc_port)
    command[command.index("--d118-motion-calibration-probe")] = "--d120-motion-calibration-probe"
    if full_matrix:
        command.append("--d120-full-matrix")
    return command


def run_calibration_runtime(runtime, backend, episode, directory, deadline_ns,
                            fixture_writer, player_teleporter, *, full_matrix=False, **kwargs):
    """One execution loop: D118 owns cancellation, arbitration and teleport checks."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    health_rows = []
    diagnostic = kwargs.pop("observation_diagnostic",None)
    def capture_observation():
        observation = runtime.observation
        own = observation.self_state.value
        row = {
            "schema_version":"mc2p.d120-health-observation.v1",
            "episode_id":observation.episode_id,
            "observation_sequence_id":observation.sequence_id,
            "request_sequence_id":getattr(observation,"request_sequence_id",None),
            "movement_tick_id":None if own is None else own.movement_tick_id,
            "health_points":None if own is None else own.health_points,
            "self_damage_event_ids":[event.event_sequence_id for event in observation.damage_events if event.target_is_self],
            "damage_events_dropped":observation.damage_events_dropped,
        }
        health_rows.append(row)
        return diagnostic() if diagnostic is not None else row
    try:
        summary,rows,checks = frozen.run_d118_momentum_calibration_runtime(
            runtime,backend,episode,directory,deadline_ns,fixture_writer,player_teleporter,
            manifest=build_manifest(full_matrix=full_matrix),observation_diagnostic=capture_observation,**kwargs)
    finally:
        write_jsonl_atomic(directory/"d120-health-observations.jsonl",health_rows)
    # A second cancel must find nothing: the shared loop already released it.
    unreleased = runtime.cancel_source(frozen._SOURCE)
    summary.update(schema_version="mc2p.d120-calibration-runtime.v1",
                   scope="diagnostic_only",sound_envelope="unverified",
                   source_release_status="released" if not unreleased else "failed")
    try:
        health = validate_health_evidence(health_rows,evidence._jsonl(directory/"d118-runtime-receipts.jsonl"))
    except ValueError as error:
        health = {"status":"failed","reason":str(error),"observation_count":len(health_rows)}
    summary["health_evidence"] = health
    summary["responsibility_cutoff"] = _responsibility_cutoff(health_rows[-1])
    checks.append({"name":"d120_shared_loop_source_released","passed":not unreleased})
    checks.append({"name":"d120_real_observation_zero_damage","passed":health["status"]=="passed"})
    write_json_atomic(directory/"d118-calibration-runtime.json",summary)
    return summary,rows,checks


def _responsibility_cutoff(observation):
    return {key:observation[key] for key in (
        "episode_id","observation_sequence_id","request_sequence_id","movement_tick_id")}


def validate_health_evidence(rows,receipts):
    if not rows:
        raise ValueError("D120 health observation evidence missing")
    previous = None
    identities = {}
    for index,row in enumerate(rows):
        health = row.get("health_points")
        identity = (row.get("episode_id"),row.get("observation_sequence_id"))
        reset_before_first_movement = (
            index==0 and identity[1]==0
            and "request_sequence_id" in row and row["request_sequence_id"] is None
            and row.get("movement_tick_id") is None)
        if (row.get("schema_version") != "mc2p.d120-health-observation.v1"
                or type(health) not in (int,float) or not math.isfinite(health) or health<=0
                or type(identity[0]) is not str or type(identity[1]) is not int
                or (type(row.get("movement_tick_id")) is not int and not reset_before_first_movement)
                or identity in identities or row.get("damage_events_dropped") != 0
                or row.get("self_damage_event_ids") != []):
            raise ValueError("D120 health or self-damage observation invalid")
        if previous is not None and (health < previous["health_points"]
                or identity[0] != previous["episode_id"]
                or identity[1] != previous["observation_sequence_id"]+1):
            raise ValueError("D120 health decreased or observation coverage incomplete")
        identities[identity] = row
        previous = row
    for receipt in receipts:
        before = identities.get((receipt["episode_id"],receipt["result_observation_sequence_id"]-1))
        after = identities.get((receipt["episode_id"],receipt["result_observation_sequence_id"]))
        if (before is None or after is None or after["movement_tick_id"] < receipt["movement_tick_id"]
                or after.get("request_sequence_id") != receipt["request_sequence_id"]):
            raise ValueError("D120 health receipt coverage missing")
    return {"status":"passed","observation_count":len(rows),"receipt_count":len(receipts),
        "trial_count":len({row["trial_id"] for row in receipts}),"health_decrease_count":0,"self_damage_event_count":0}


def _post_state(row,world):
    return entry.state_from_tick_evidence({**row,"pre_state":row["post_state"],
        "movement_tick_id":row["movement_tick_id"]+1},world,entry.JAVA_1_21_RULESET)


def _complete_physics(rows,world):
    """Replay every captured application, including unleased neutral tail ticks."""
    current = entry.state_from_tick_evidence(rows[0],world,entry.JAVA_1_21_RULESET)
    failures = []
    previous_tick = rows[0]["movement_tick_id"]-1
    for row in rows:
        tick = row["movement_tick_id"]
        if tick != previous_tick+1:
            failures.append(f"{tick}:non_contiguous_tick")
        previous_tick = tick
        result = entry.physics_step(current,evidence.input_from_tick_evidence(row),world,entry.JAVA_1_21_RULESET)
        if result.status is not entry.CalculationStatus.OK or result.next_state is None:
            failures.append(f"{tick}:calculation_failed")
            break
        current = result.next_state
        expected = row["post_state"]
        if (max(abs(a-b) for a,b in zip(current.position,evidence._state_vector(expected,"position"))) > .001
                or max(abs(a-b) for a,b in zip(current.velocity_blocks_per_tick,evidence._state_vector(expected,"velocity"))) > .0001
                or evidence._discrete_mismatches(current,expected,result.events,row)):
            failures.append(f"{tick}:physics_mismatch")
    return {"status":"failed" if failures else "passed","tick_count":len(rows),"issues":failures}


def analyze_run(run_dir, *, full_matrix=False):
    source = Path(run_dir)
    client = source/"client-0"
    owned = client/OWNED_DIRECTORY
    manifest = validate_manifest_document(_read(owned/"d118-input-manifest.json"))
    if manifest.sha256 != build_manifest(full_matrix=full_matrix).sha256:
        raise ValueError("D120 capture matrix differs")
    runtime = _read(owned/"d118-calibration-runtime.json")
    if (runtime.get("schema_version") != "mc2p.d120-calibration-runtime.v1"
            or runtime.get("status") != "recorded"
            or runtime.get("manifest_sha256") != manifest.sha256
            or runtime.get("trial_count") != len(manifest.trials)
            or runtime.get("command_count") != sum(len(t.commands) for t in manifest.trials)
            or runtime.get("source_release_status") != "released"):
        raise ValueError("D120 runtime capture or release incomplete")
    receipts = evidence._jsonl(owned/"d118-runtime-receipts.jsonl")
    health = validate_health_evidence(evidence._jsonl(owned/"d120-health-observations.jsonl"),receipts)
    if runtime.get("health_evidence") != health:
        raise ValueError("D120 health runtime summary differs from observations")
    fixtures = evidence._jsonl(owned/"d118-fixture-commands.jsonl")
    evidence.validate_receipts_against_manifest(manifest,receipts)
    physics = tuple(iter_segmented_jsonl(client/"physics-tick-events"))
    observed_health = evidence._jsonl(owned/"d120-health-observations.jsonl")
    cutoff = _responsibility_cutoff(observed_health[-1])
    if runtime.get("responsibility_cutoff") != cutoff:
        raise ValueError("D120 responsibility cutoff differs from final health observation")
    receipt_keys = {(r["episode_id"],r["request_sequence_id"]) for r in receipts}
    last_receipt = receipts[-1]
    if (cutoff["episode_id"] != last_receipt["episode_id"]
            or cutoff["observation_sequence_id"] < last_receipt["result_observation_sequence_id"]
            or cutoff["request_sequence_id"] != last_receipt["request_sequence_id"]
            or any(receipt["movement_tick_id"]>cutoff["movement_tick_id"] for receipt in receipts)):
        raise ValueError("D120 health evidence does not cover task responsibility")
    post_release = tuple(row for row in physics
        if (row.get("episode_id"),row.get("request_sequence_id")) in receipt_keys
        and row["movement_tick_id"]>cutoff["movement_tick_id"])
    neutral = {"forward":0.,"strafe":0.,"jump":False,"sneak":False,"sprint":False}
    if any(row.get("episode_id") != last_receipt["episode_id"]
            or row.get("request_sequence_id") != last_receipt["request_sequence_id"]
            or row.get("input_state") not in {"lease_exhausted","expired","disallowed"}
            or row.get("actual_input") != neutral for row in post_release):
        raise ValueError("D120 post-release tail is not the final inactive neutral request")
    stream = tuple(iter_segmented_jsonl(client/"ftick-events"))
    events = tuple(r for r in stream if r.get("schema_version") == "mc2p.client-ftick-event.v1")
    summaries = tuple(r for r in stream if r.get("schema_version") == "mc2p.client-ftick-summary.v1" and r.get("kind") == "SUMMARY")
    if len(summaries) != 1 or summaries[0].get("dropped_event_count") != 0 or summaries[0].get("event_count") != len(events):
        raise ValueError("D120 F-TICK capture incomplete")
    joined = [frozen.join_command_timeline(r,physics,events) for r in receipts]
    replay = evidence.analyze_physics_replay(manifest,receipts,physics,fixtures)
    contexts = evidence.declared_trial_fixture_contexts(manifest,fixtures)
    indexed = {(r["episode_id"],r["request_sequence_id"],r["movement_tick_id"]):r
               for r in physics if r.get("schema_version") == "mc2p.client-physics-tick.v1"}
    entries = []
    post_release_issues = []
    for trial in manifest.trials:
        selected = sorted((r for r in receipts if r["trial_id"] == trial.trial_id),key=lambda r:r["command_index"])
        ordered = tuple(indexed[(r["episode_id"],r["request_sequence_id"],r["movement_tick_id"])] for r in selected)
        first_landing = next((n for n,r in enumerate(ordered) if "landed" in r.get("contact_events",())),None)
        if first_landing != 25:
            raise ValueError("D120 first landed post-state differs")
        context = contexts[trial.trial_id]
        request_keys = {(r["episode_id"],r["request_sequence_id"]) for r in selected}
        all_rows = tuple(sorted((row for row in physics
            if (row.get("episode_id"),row.get("request_sequence_id")) in request_keys),
            key=lambda row:row["movement_tick_id"]))
        complete = _complete_physics(all_rows,context.world)
        task_rows = tuple(row for row in all_rows if row["movement_tick_id"]<=cutoff["movement_tick_id"])
        closing_rows = tuple(row for row in all_rows if row["movement_tick_id"]>cutoff["movement_tick_id"])
        closing_issues = [issue for issue in complete["issues"]
            if int(issue.split(":",1)[0])>cutoff["movement_tick_id"]]
        fixture = entry.Fixture(trial,context,entry.digest([r for r in fixtures if r["trial_id"] == trial.trial_id]))
        actual = entry.landed_post_state(ordered[25],context.world)
        nominal = entry.nominal_entry(fixture)
        offsets = [*(a-b for a,b in zip(entry._axes(actual,fixture),entry._axes(nominal,fixture))),
                   entry.yaw_offset_degrees(actual.yaw_radians,nominal.yaw_radians)]
        margins = [max(axis)-abs(value) for axis,value in zip(entry.AXES,offsets)]
        suffixes = {name:entry.replay_suffix(actual,fixture,name) for name in entry.SUFFIX_LIMITS}
        timing = evidence._event_timing(trial,ordered)
        relative = timing["sneak_relative_to_landing"] if trial.fallback else timing["second_jump_relative_to_landing"]
        expected = 2 if "late1" in trial.trial_id else 1
        landed_tick = actual.movement_tick_id
        tail = tuple(row for row in all_rows if row["movement_tick_id"]>=landed_tick)
        supported = all(entry._supported(_post_state(row,context.world),fixture,(1,2))
            for row in tail) if trial.fallback else True
        landing_ticks = [row["movement_tick_id"] for row in tail
            if "landed" in row.get("contact_events",()) and row["movement_tick_id"]>landed_tick]
        target_start = trial.first_gap_cells+trial.middle_platform_cells+trial.second_gap_cells
        target_supported = (bool(landing_ticks) and all(entry._supported(_post_state(row,context.world),
            fixture,(target_start,target_start+6)) for row in all_rows
            if row["movement_tick_id"]>=landing_ticks[0])) if not trial.fallback else True
        safe_span = (1,2) if trial.fallback else (target_start,target_start+6)
        if any(not entry._supported(_post_state(row,context.world),fixture,safe_span) for row in closing_rows):
            closing_issues.append("post_release_tail_left_safe_support")
        post_release_issues.extend(closing_issues)
        immutable = (actual.on_ground == nominal.on_ground and actual.pose == nominal.pose
            and actual.sprinting == nominal.sprinting and actual.sneaking == nominal.sneaking
            and actual.horizontal_collision == nominal.horizontal_collision
            and actual.vertical_collision == nominal.vertical_collision
            and abs(actual.position[1]-nominal.position[1]) <= .001
            and abs(actual.velocity_blocks_per_tick[1]-nominal.velocity_blocks_per_tick[1]) <= .0001
            and entry._supported(actual,fixture,(1,2)))
        entries.append({"trial_id":trial.trial_id,"entry_phase":"landed_post_state",
            "within_registered_bounds":immutable and all(m>=-1e-12 for m in margins),
            "registered_bound_margins":margins,"raw_suffix_replay":suffixes,
            "observed_suffix_relative_tick":relative,"expected_suffix_relative_tick":expected,
            "fallback_full_tail_supported":supported,"target_landing_tail_supported":target_supported,
            "complete_physics_replay":complete,"timing_passed":relative==expected})
        entries[-1]["responsibility_physics_tick_count"] = len(task_rows)
        entries[-1]["post_release_tail"] = {"count":len(closing_rows),
            "status":"failed" if closing_issues else "passed","issues":closing_issues}
    accepted = replay["status"] == "passed" and not post_release_issues and all(
        r["within_registered_bounds"] and r["timing_passed"] and r["fallback_full_tail_supported"]
        and r["target_landing_tail_supported"] and r["complete_physics_replay"]["status"]=="passed"
        and all(s["passed"] for s in r["raw_suffix_replay"].values()) for r in entries)
    return {"schema_version":"mc2p.d120-calibration-analysis.v1","status":"passed" if accepted else "failed",
        "manifest_sha256":manifest.sha256,"joined_command_count":len(joined),"physics_replay":replay,
        "entries":entries,"health_evidence":health,"responsibility_cutoff":cutoff,
        "post_release_tail_count":len(post_release),
        "post_release_tail_status":"failed" if post_release_issues else "passed",
        "sound_envelope":"unverified","scope":"diagnostic_only"}


def seal_run(output, run_dir, *, command,return_code,stdout,stderr,offline_hash,south_hash=None,full_matrix=False):
    output = Path(output)
    output.mkdir(parents=True,exist_ok=False)
    analysis = {"status":"unverified","sound_envelope":"unverified"}
    capture = "failed"
    error = None
    source = None if run_dir is None else Path(run_dir)
    raw_files = {} if source is None else {
        p.relative_to(source).as_posix():_hash(p) for p in sorted(source.rglob("*"))
        if p.is_file() and (OWNED_DIRECTORY in p.parts or "physics-tick-events" in p.parts
                            or "ftick-events" in p.parts or p == source/"result.json")}
    try:
        if return_code != 0 or source is None or _read(source/"result.json").get("status") != "passed":
            raise ValueError("D120 probe capture not passed")
        analysis = analyze_run(source,full_matrix=full_matrix)
        capture = "captured"
    except (OSError,ValueError,TypeError,KeyError) as failure:
        error = f"{type(failure).__name__}: {failure}"
    status = "passed" if capture == "captured" and analysis["status"] == "passed" else "failed"
    summary = {"schema_version":"mc2p.d120-calibration-launch.v1","status":status,
        "capture_status":capture,"acceptance_status":status,"error":error,
        "full_matrix":full_matrix,"manifest_sha256":build_manifest(full_matrix=full_matrix).sha256,
        "offline_prerequisite_sha256":offline_hash,"south_prerequisite_sha256":south_hash,
        "source_run":None if source is None else str(source.resolve()),"probe_return_code":return_code,
        "raw_files":raw_files,"scope":"diagnostic_only","sound_envelope":"unverified"}
    for name,value in (("summary.json",summary),("analysis.json",analysis),("command.json",command)):
        write_json_atomic(output/name,value)
    (output/"probe-stdout.txt").write_text(stdout,encoding="utf-8",newline="\n")
    (output/"probe-stderr.txt").write_text(stderr,encoding="utf-8",newline="\n")
    write_json_atomic(output/"index.json",{
        "schema_version":"mc2p.d120-calibration-index.v1","status":status,"full_matrix":full_matrix,
        "offline_prerequisite_sha256":offline_hash,"south_prerequisite_sha256":south_hash,
        "files":{name:_hash(output/name) for name in (
            "summary.json","analysis.json","command.json","probe-stdout.txt","probe-stderr.txt")}})
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--run-dir",type=Path)
    parser.add_argument("--server-port",type=int,default=25597)
    parser.add_argument("--ipc-port",type=int,default=8140)
    parser.add_argument("--full-matrix",action="store_true")
    parser.add_argument("--offline-prerequisite",type=Path,default=ROOT/".tmp/d120-entry-offline-v1")
    parser.add_argument("--south-prerequisite",type=Path,default=ROOT/".tmp/d120-fabric-south-v1")
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("D120 output already exists; preserve the previous result")
    offline = validate_offline_prerequisite(args.offline_prerequisite)
    south = validate_south_prerequisite(args.south_prerequisite,offline) if args.full_matrix else None
    command = fabric_probe_command(server_port=args.server_port,ipc_port=args.ipc_port,full_matrix=args.full_matrix)
    run_dir = args.run_dir
    stdout = stderr = ""
    code = 0
    if run_dir is None:
        completed = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace")
        stdout,stderr,code = completed.stdout,completed.stderr,completed.returncode
        run_dir = evidence._run_directory(stdout)
    summary = seal_run(args.output,run_dir,command=command,return_code=code,stdout=stdout,stderr=stderr,
        offline_hash=offline,south_hash=south,full_matrix=args.full_matrix)
    print(json.dumps(summary,ensure_ascii=False))
    return 0 if summary["status"] == "passed" else 3 if summary["capture_status"] == "captured" else 2


if __name__ == "__main__":
    raise SystemExit(main())
