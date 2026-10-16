"""Record and verify the D105 T2 receiver and publication evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import statistics
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HASH_DOMAIN = "sha256_lf_bytes_v1"
TIMING_SCHEMA = "mc2p.d105-t2-receiver-timing.v1"
TIMING_RAW_SCHEMA = "mc2p.d105-t2-receiver-timing-raw.v1"
REPOSITORY_PYTHON = r".\.venv\python.exe"


def _lf_bytes(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def sha256_lf_bytes(data: bytes) -> str:
    return hashlib.sha256(_lf_bytes(data)).hexdigest()


def sha256_lf_path(path: Path) -> str:
    return sha256_lf_bytes(path.read_bytes())


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    "utf-8", newline="\n")


def _read_json(path: Path):
    return json.loads(path.read_text("utf-8"))


def _command_interpreter(command: str) -> str:
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return "" if end < 0 else command[1:end]
    return command.split(maxsplit=1)[0] if command else ""


def _resolve_interpreter(value: str) -> Path:
    candidate = Path(value.replace("\\", "/"))
    return candidate if candidate.is_absolute() else ROOT / candidate


def _interpreter_is_available(value: str) -> bool:
    if _resolve_interpreter(value).is_file():
        return True
    # A source checkout owns ``.venv/python.exe``.  A standalone export
    # deliberately does not copy that environment, so the exact recorded
    # repository interpreter is represented by the interpreter running the
    # verifier.  Other missing paths must still fail.
    recorded = os.path.normcase(value.replace("/", "\\"))
    expected = os.path.normcase(REPOSITORY_PYTHON)
    return (recorded == expected
            and (ROOT / "EXPORT-METADATA.json").is_file()
            and Path(sys.executable).is_file())


def _command_argv(command: str) -> list[str]:
    return [value.strip('"') for value in shlex.split(command, posix=False)]


def _normalized_command(argv: list[str]) -> tuple[str, ...]:
    normalized = []
    path_value = False
    for index, value in enumerate(argv):
        if index == 0:
            normalized.append(os.path.normcase(str(_resolve_interpreter(value).resolve())))
        elif index == 1 or path_value:
            path = Path(value.replace("\\", "/"))
            path = path if path.is_absolute() else ROOT / path
            normalized.append(os.path.normcase(str(path.resolve())))
        else:
            normalized.append(value)
        path_value = value in ("--input", "--output")
    return tuple(normalized)


def validate_generation_commands(index_path: Path) -> dict:
    """Require every recorded command to name an existing Python executable."""
    errors = []
    try:
        index_path = Path(index_path).resolve()
        root_index = _read_json(index_path)
        commands = root_index.get("generation_commands", {})
        if not commands:
            errors.append("T2 root index has no generation commands")
        for name, command in commands.items():
            interpreter = _command_interpreter(command) if type(command) is str else ""
            if not interpreter or not _interpreter_is_available(interpreter):
                errors.append("generation command interpreter does not exist: " + str(name))
        timing_path = index_path.parent / "receiver-timing/index.json"
        timing = _read_json(timing_path)
        argv = timing.get("generation_command_argv")
        command = timing.get("generation_command")
        if (type(argv) is not list or not argv or type(argv[0]) is not str
                or not _interpreter_is_available(argv[0])):
            errors.append("receiver timing argv interpreter does not exist")
        if (type(command) is not str
                or _command_interpreter(command) != (argv[0] if type(argv) is list and argv else None)):
            errors.append("receiver timing command and argv interpreter disagree")
        root_receiver = commands.get("receiver_timing")
        if (type(root_receiver) is not str or type(command) is not str
                or type(argv) is not list
                or _normalized_command(_command_argv(root_receiver)) != _normalized_command(argv)
                or _normalized_command(_command_argv(command)) != _normalized_command(argv)):
            errors.append("root and receiver timing command/argv disagree")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        errors.append("invalid generation command evidence: " + str(error))
    return {"passed": not errors, "errors": list(dict.fromkeys(errors))}


def extract_timeline(raw: dict) -> dict:
    """Describe observed segments; timestamps are diagnostics, never gates."""
    submits = [event for event in raw.get("transport_events", ())
               if event.get("operation") == "submit" and event.get("job_token") is not None]
    receives = [event for event in raw.get("transport_events", ())
                if event.get("operation") == "receive" and event.get("job_token") is not None]
    publishes = [event for event in raw.get("native_events", ())
                 if event.get("operation") == "publish"]
    consumes = [event for event in raw.get("native_events", ())
                if event.get("operation") == "first_consume"]
    groups = (submits, receives, publishes, consumes)
    complete = all(len(group) == 1 for group in groups)
    if not complete:
        return {"complete": False, "identity": {}, "events_ns": {},
                "segments_ns": {}, "gate_inputs": []}
    submit, receive, publish, consume = (group[0] for group in groups)
    times = (submit.get("timestamp_ns"), receive.get("timestamp_ns"),
             publish.get("timestamp_ns"), consume.get("timestamp_ns"))
    identities_match = (submit.get("job_token") == receive.get("job_token")
                        and publish.get("proof_token") == consume.get("proof_token"))
    complete = (identities_match and all(type(value) is int for value in times)
                and tuple(sorted(times)) == times)
    events = dict(submit=times[0], worker_result_received=times[1],
                  receiver_publish=times[2], first_consume=times[3])
    segments = ({
        "submit_to_worker_result_received": times[1] - times[0],
        "worker_result_received_to_receiver_publish": times[2] - times[1],
        "receiver_publish_to_first_consume": times[3] - times[2],
        "submit_to_first_consume": times[3] - times[0],
    } if complete else {})
    return {
        "complete": complete,
        "identity": {"job_token": submit.get("job_token"),
                     "proof_token": publish.get("proof_token")},
        "events_ns": events,
        "segments_ns": segments,
        "gate_inputs": [],
        "worker_completion_boundary": "parent_observed_result_receive",
    }


def measure_receiver_sample(sample_index: int, arguments: tuple, bundle, snapshot) -> dict:
    """Time both receiver paths against one immutable message and parent facts."""
    from mc2p.motion_nav import prepared_motion_validation as validation

    elapsed = {}
    statuses = {}
    order = (("full_audit", False), ("trusted_local", True))
    if sample_index % 2 == 0:
        order = tuple(reversed(order))
    for name, trusted in order:
        started = time.perf_counter_ns()
        result = validation._validate_for_registration(
            *arguments, trusted_local_worker=trusted)
        elapsed[name] = time.perf_counter_ns() - started
        statuses[name] = result.admission.status.name
    message = arguments[0]
    return {
        "schema": TIMING_RAW_SCHEMA,
        "sample_index": sample_index,
        "full_audit_elapsed_ns": elapsed["full_audit"],
        "trusted_local_elapsed_ns": elapsed["trusted_local"],
        "full_audit_status": statuses["full_audit"],
        "trusted_local_status": statuses["trusted_local"],
        "message_payload_bytes": len(message.payload),
        "bundle_canonical_bytes": len(bundle.sealed_bytes),
        "snapshot_canonical_bytes": len(snapshot.canonical_bytes),
        "snapshot_cells": len(snapshot.facts),
    }


def _receiver_summary(rows: list[dict]) -> tuple[dict, tuple[str, ...]]:
    errors = []
    indices = [row.get("sample_index") for row in rows]
    if indices != list(range(1, len(rows) + 1)):
        errors.append("timing sample indices are not complete and ordered")
    scalar_names = ("message_payload_bytes", "bundle_canonical_bytes",
                    "snapshot_canonical_bytes", "snapshot_cells")
    scalars = {}
    for name in scalar_names:
        values = {row.get(name) for row in rows}
        if len(values) != 1 or any(type(value) is not int or value <= 0 for value in values):
            errors.append("timing raw values disagree: " + name)
        else:
            scalars[name] = values.pop()
    for row in rows:
        if row.get("schema") != TIMING_RAW_SCHEMA:
            errors.append("timing raw schema mismatch")
        if (row.get("full_audit_status") != "REGISTERED"
                or row.get("trusted_local_status") != "REGISTERED"):
            errors.append("timing receiver did not register the unchanged message")
    full = [row.get("full_audit_elapsed_ns") for row in rows]
    trusted = [row.get("trusted_local_elapsed_ns") for row in rows]
    if any(type(value) is not int or value <= 0 for value in full + trusted):
        errors.append("timing sample is not a positive integer")
        full = trusted = []
    summary = {"sample_count": len(rows), **scalars}
    if full and trusted:
        full_median = statistics.median(full)
        trusted_median = statistics.median(trusted)
        summary.update(
            full_audit={"minimum_ns": min(full), "median_ns": full_median,
                        "maximum_ns": max(full)},
            trusted_local={"minimum_ns": min(trusted), "median_ns": trusted_median,
                           "maximum_ns": max(trusted)},
            median_reduction_percent=round(
                100.0 * (full_median - trusted_median) / full_median, 6),
            clock_affects_behavior=False,
        )
    return summary, tuple(dict.fromkeys(errors))


def record_receiver_timing(output: Path, *, command_argv: list[str]) -> dict:
    if output.exists():
        raise ValueError("OUTPUT_EXISTS")
    from tests.motion_nav import test_d102_receiver as fixtures

    fixtures.ReceiverTests.setUpClass()
    arguments = (fixtures.ReceiverTests.message, fixtures.ReceiverTests.request,
                 fixtures.ReceiverTests.snapshot, fixtures.ReceiverTests.current)
    bundle, snapshot = fixtures.ReceiverTests.bundle, fixtures.ReceiverTests.snapshot
    measure_receiver_sample(1, arguments, bundle, snapshot)
    rows = []
    for sample_index in range(1, 11):
        row = measure_receiver_sample(sample_index, arguments, bundle, snapshot)
        path = output / "raw" / f"sample-{sample_index:02}.json"
        _write_json(path, row)
        rows.append({"sample_index": sample_index,
                     "path": path.relative_to(output).as_posix(),
                     "sha256": sha256_lf_path(path)})
    raw_values = [_read_json(output / row["path"]) for row in rows]
    summary, errors = _receiver_summary(raw_values)
    recorded = {"schema": TIMING_SCHEMA, "summary": summary,
                "errors": list(errors), "passed": not errors and len(rows) == 10}
    summary_path = output / "summary.json"
    _write_json(summary_path, recorded)
    index = {
        "schema": TIMING_SCHEMA,
        "hash_domain": HASH_DOMAIN,
        "generation_command_argv": command_argv,
        "generation_command": subprocess.list2cmdline(command_argv),
        "sample_count": len(rows),
        "raw_samples": rows,
        "summary": {"path": "summary.json", "sha256": sha256_lf_path(summary_path)},
    }
    _write_json(output / "index.json", index)
    return recompute_receiver_timing(output / "index.json")


def recompute_receiver_timing(index_path: Path) -> dict:
    index_path = Path(index_path).resolve()
    root = index_path.parent
    errors = []
    try:
        index = _read_json(index_path)
        if index.get("schema") != TIMING_SCHEMA or index.get("hash_domain") != HASH_DOMAIN:
            errors.append("receiver timing index schema or hash domain mismatch")
        rows = []
        declared = index.get("raw_samples", [])
        if index.get("sample_count") != 10 or len(declared) != 10:
            errors.append("receiver timing requires exactly ten raw samples")
        for item in declared:
            path = (root / item["path"]).resolve()
            if root not in path.parents or not path.is_file():
                errors.append("receiver timing raw path is missing or escapes its root")
                continue
            if sha256_lf_path(path) != item.get("sha256"):
                errors.append("receiver timing raw hash mismatch: " + item["path"])
                continue
            rows.append(_read_json(path))
        summary, row_errors = _receiver_summary(rows)
        errors.extend(row_errors)
        summary_path = (root / index["summary"]["path"]).resolve()
        if root not in summary_path.parents or not summary_path.is_file():
            errors.append("receiver timing summary is missing or escapes its root")
            recorded = {}
        else:
            if sha256_lf_path(summary_path) != index["summary"].get("sha256"):
                errors.append("receiver timing summary hash mismatch")
            recorded = _read_json(summary_path)
        recorded_summary = recorded.get("summary")
        if recorded_summary != summary:
            errors.append("receiver timing summary does not match raw recomputation")
        if recorded.get("errors") or recorded.get("passed") is not True:
            errors.append("recorded receiver timing did not pass")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        errors.append("invalid receiver timing evidence: " + str(error))
        summary, recorded_summary, rows = {}, {}, []
    return {"passed": not errors, "errors": list(dict.fromkeys(errors)),
            "sample_count": len(rows), "summary": summary,
            "recorded_summary": recorded_summary}


def verify_evidence_index(
    index_path: Path,
    *,
    require_current_sources: bool = True,
) -> dict:
    index_path = Path(index_path).resolve()
    evidence_root = index_path.parent
    errors = []
    try:
        index = _read_json(index_path)
        if index.get("schema") != "mc2p.d105-t2-index.v2":
            errors.append("T2 evidence index schema mismatch")
        if index.get("hash_domain") != HASH_DOMAIN:
            errors.append("T2 evidence hash domain mismatch")
        evidence = index.get("evidence_files", [])
        declared_paths = [item.get("path") for item in evidence]
        if len(declared_paths) != len(set(declared_paths)):
            errors.append("T2 evidence paths are duplicated")
        actual_paths = sorted(path.relative_to(evidence_root).as_posix()
                              for path in evidence_root.rglob("*")
                              if path.is_file() and path.resolve() != index_path)
        if sorted(declared_paths) != actual_paths:
            errors.append("T2 index does not declare every evidence file exactly once")
        verified = 0
        for item in evidence:
            path = (evidence_root / item["path"]).resolve()
            if evidence_root not in path.parents or not path.is_file():
                errors.append("T2 evidence path is missing or escapes root: " + item["path"])
                continue
            if sha256_lf_path(path) != item.get("sha256"):
                errors.append("T2 evidence hash mismatch: " + item["path"])
                continue
            verified += 1
        if require_current_sources:
            for item in index.get("source_files", []):
                path = (ROOT / item["path"]).resolve()
                if ROOT not in path.parents or not path.is_file():
                    errors.append("T2 source path is missing or escapes repository: " + item["path"])
                elif sha256_lf_path(path) != item.get("sha256"):
                    errors.append("T2 source hash mismatch: " + item["path"])
        timing = recompute_receiver_timing(evidence_root / "receiver-timing/index.json")
        if not timing["passed"]:
            errors.extend("receiver timing: " + error for error in timing["errors"])
        commands = validate_generation_commands(index_path)
        if not commands["passed"]:
            errors.extend(commands["errors"])
        spawn_raw = _read_json(evidence_root / "spawn-probe/raw/probe.json")
        spawn_timeline = _read_json(evidence_root / "spawn-probe/timeline.json")
        if spawn_timeline.get("timeline") != extract_timeline(spawn_raw):
            errors.append("spawn timeline does not match raw recomputation")
        if (spawn_timeline.get("passed") is not True
                or any(spawn_timeline.get("final_resources", {}).values())):
            errors.append("spawn timeline or final resources did not pass")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        errors.append("invalid T2 evidence index: " + str(error))
        evidence, verified = [], 0
    return {"passed": not errors, "errors": list(dict.fromkeys(errors)),
            "evidence_file_count": len(evidence),
            "verified_evidence_file_count": verified}


def record_timeline(output: Path) -> dict:
    if platform.system() != "Windows":
        raise ValueError("D105 T2 formal timeline requires Windows")
    from tests.sim.d102_prepared_motion import record_probe, validate_probe

    manifest = ROOT / "tests/sim/manifests/prepared-motion-proof-d102-v1.json"
    axes = dict(platform_width=2, start_offset=0, entry_offset=0.,
                direction="south", backlog=0)
    raw = record_probe(axes, output=output, manifest=manifest, transport="spawn")
    errors = validate_probe(raw, check_performance=False)
    timeline = extract_timeline(raw)
    result = {
        "schema": "mc2p.d105-t2-timeline.v1",
        "hash_domain": HASH_DOMAIN,
        "platform": platform.system(),
        "python": sys.version,
        "timeline": timeline,
        "probe_status": raw.get("status"),
        "probe_errors": errors,
        "final_resources": raw.get("final_resources"),
        "passed": raw.get("status") == "PASS" and not errors and timeline["complete"]
                  and not any(raw.get("final_resources", {}).values()),
        "clock_affects_behavior": False,
    }
    _write_json(output / "timeline.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--record-receiver", action="store_true")
    modes.add_argument("--record-timeline", action="store_true")
    modes.add_argument("--recompute", action="store_true")
    modes.add_argument("--verify-evidence", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.record_receiver:
            if platform.system() != "Windows" or args.output is None or args.input is not None:
                raise ValueError("receiver recording requires Windows and only --output")
            result = record_receiver_timing(args.output,
                                            command_argv=[REPOSITORY_PYTHON, *sys.argv])
        elif args.record_timeline:
            if args.output is None or args.input is not None:
                raise ValueError("timeline recording requires only --output")
            result = record_timeline(args.output)
        elif args.recompute:
            if args.input is None or args.output is not None:
                raise ValueError("recompute requires only --input")
            result = recompute_receiver_timing(args.input)
        else:
            if args.input is None or args.output is not None:
                raise ValueError("evidence verification requires only --input")
            result = verify_evidence_index(args.input)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["passed"] else 1
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        print("FAIL: " + str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
