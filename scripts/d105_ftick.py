"""Recompute D105 F-TICK timelines from B10 Runtime and client sidecar facts."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.runtime.segmented_trace import iter_segmented_jsonl
from scripts.control_probe_core import write_json_atomic
from scripts.b10_gap_solver_runtime import _receiver_diagnostic_summary


_DIRECTION_NAMES = ("south", "east", "north", "west")
_KINDS = (
    "CLIENT_FRAME_RECEIVED",
    "INPUT_SAMPLED",
    "MOVEMENT_APPLIED",
    "CLIENT_OBSERVATION_SENT",
)
_INPUT_KEYS = ("forward", "strafe", "jump", "sneak", "sprint")


def fabric_probe_command(*, server_port: int, ipc_port: int) -> list[str]:
    if (type(server_port) is not int or type(ipc_port) is not int
            or not 1 <= server_port <= 65_535 or not 1 <= ipc_port <= 65_535
            or server_port == ipc_port):
        raise ValueError("invalid F-TICK probe ports")
    return [
        sys.executable,
        str(Path(__file__).resolve().with_name("probe_fabric_deployment_observation.py")),
        "--seed", "21001",
        "--server-port", str(server_port),
        "--ipc-port", str(ipc_port),
        "--b10-gap-solver-probe",
        "--time-diagnostics",
        "--physics-tick-diagnostics",
        "--ftick-diagnostics",
    ]


def _action(row: dict) -> dict | None:
    payload = row.get("payload")
    if type(payload) is not dict:
        return None
    decision = payload.get("decision")
    action = decision.get("action") if type(decision) is dict else None
    return action if type(action) is dict else None


def _backend(row: dict) -> dict | None:
    payload = row.get("payload")
    if type(payload) is not dict:
        return None
    value = payload.get("backend_result")
    if type(value) is dict:
        return value
    value = payload.get("result")
    return value if type(value) is dict else None


def _one(rows: list[dict], label: str, errors: list[str]) -> dict | None:
    if len(rows) != 1:
        errors.append(f"{label}_{'missing' if not rows else 'duplicate'}")
        return None
    return rows[0]


def classify_tick_trace(
        trial: dict, trace_rows: tuple[dict, ...], client_rows: tuple[dict, ...], *,
        sidecar_sealed: bool, dropped_events: int) -> dict:
    """Classify one first command using identity and tick facts, never cross-clock deltas."""
    errors: list[str] = []
    if not sidecar_sealed:
        errors.append("client_sidecar_unsealed")
    if type(dropped_events) is not int or dropped_events < 0:
        errors.append("client_event_count_invalid")
    elif dropped_events:
        errors.append("client_event_overflow")

    episode = "fabric-21001-0"
    observation = trial.get("start_observation_sequence_id")
    anchor_tick = trial.get("anchor_movement_tick_id")
    if (trial.get("group") != "validation"
            or type(trial.get("direction_index")) is not int
            or type(trial.get("repetition")) is not int
            or type(observation) is not int or type(anchor_tick) is not int):
        errors.append("invalid_b10_trial_identity")

    dispatches = [
        row for row in trace_rows
        if row.get("record_type") == "dispatch"
        and (_action(row) or {}).get("episode_id") == episode
        and (_action(row) or {}).get("observation_sequence_id") == observation
    ]
    dispatch = _one(dispatches, "decision_send", errors)
    action = None if dispatch is None else _action(dispatch)
    request = None if action is None else action.get("request_sequence_id")
    if type(request) is not int:
        errors.append("request_sequence_invalid")

    received = []
    receipts = []
    for row in trace_rows:
        backend = _backend(row)
        if backend is None:
            continue
        observed = backend.get("observation")
        receipt = backend.get("receipt")
        if (type(observed) is dict and observed.get("episode_id") == episode
                and observed.get("sequence_id") == observation):
            received.append(row)
        if (type(receipt) is dict and receipt.get("episode_id") == episode
                and receipt.get("request_sequence_id") == request):
            receipts.append(row)
    _one(received, "python_observation_receive", errors)
    receipt_row = _one(receipts, "first_receipt", errors)
    backend = None if receipt_row is None else _backend(receipt_row)
    receipt = None if backend is None else backend.get("receipt")
    response_observation = None if backend is None else backend.get("observation")
    generation = None if type(receipt) is not dict else receipt.get("generation_id")
    if (type(generation) is not int or generation != observation + 1
            or type(response_observation) is not dict
            or response_observation.get("episode_id") != episode
            or response_observation.get("sequence_id") != generation):
        errors.append("response_generation_mismatch")

    matching_client = {}
    for kind in _KINDS:
        rows = [
            row for row in client_rows
            if row.get("schema_version") == "mc2p.client-ftick-event.v1"
            and row.get("kind") == kind
            and row.get("episode_id") == episode
            and row.get("request_sequence_id") == request
        ]
        matching_client[kind] = _one(rows, kind.lower(), errors)
    frame = matching_client["CLIENT_FRAME_RECEIVED"]
    sampled = matching_client["INPUT_SAMPLED"]
    applied = matching_client["MOVEMENT_APPLIED"]
    sent = matching_client["CLIENT_OBSERVATION_SENT"]
    for row in (sampled, applied):
        if row is not None and row.get("observation_sequence_id") not in {None, observation}:
            errors.append("client_observation_identity_mismatch")
    if frame is not None and frame.get("observation_sequence_id") != observation:
        errors.append("client_frame_observation_mismatch")
    if sent is not None and sent.get("observation_sequence_id") != observation:
        errors.append("client_sent_observation_mismatch")
    if sent is not None and sent.get("generation_id") != generation:
        errors.append("client_generation_mismatch")
    ordered = [row.get("event_sequence") for row in (frame, sampled, applied, sent)
               if row is not None]
    if len(ordered) == 4 and (any(type(value) is not int for value in ordered)
                              or ordered != sorted(ordered)
                              or len(set(ordered)) != 4):
        errors.append("client_event_order_invalid")

    applications = [] if type(receipt) is not dict else receipt.get("input_applications")
    if type(applications) is not list:
        errors.append("receipt_applications_invalid")
        applications = []
    applications = [item for item in applications
                    if type(item) is dict
                    and item.get("episode_id") == episode
                    and item.get("request_sequence_id") == request]
    application = _one(applications, "first_application", errors)
    movement_tick = None if application is None else application.get("movement_tick_id")
    if type(movement_tick) is not int:
        errors.append("movement_tick_invalid")
    for row in (sampled, applied, sent):
        if row is not None and row.get("movement_tick_id") != movement_tick:
            errors.append("client_movement_tick_mismatch")
    if type(receipt) is dict and receipt.get("dropped_input_samples") not in {None, 0}:
        errors.append("input_receipt_overflow")
    movement = None if action is None else action.get("movement")
    if (type(movement) is not dict or application is None
            or any(movement.get(key) != application.get(key) for key in _INPUT_KEYS)):
        errors.append("b10_input_regression")
    if (trial.get("landed") is not True
            or trial.get("horizontal_collisions") != 0
            or trial.get("jump_pulses") != 1
            or not isinstance(trial.get("horizontal_error_blocks"), (int, float))
            or trial.get("horizontal_error_blocks") > .2
            or not isinstance(trial.get("level_error_blocks"), (int, float))
            or trial.get("level_error_blocks") > .03):
        errors.append("b10_safety_or_landing_regression")

    offset = (movement_tick - anchor_tick
              if type(movement_tick) is int and type(anchor_tick) is int else None)
    if offset is not None and offset < 1:
        errors.append("movement_tick_not_after_observation")
    classification = (
        "INCOMPLETE" if errors else
        "NEXT_MOVEMENT_TICK" if offset == 1 else
        "LATER_MOVEMENT_TICK"
    )
    direction = trial.get("direction_index")
    return {
        "trial": trial.get("name"),
        "direction": (_DIRECTION_NAMES[direction]
                      if type(direction) is int and 0 <= direction < 4 else None),
        "repetition": trial.get("repetition"),
        "episode_id": episode,
        "observation_sequence_id": observation,
        "generation_id": generation,
        "request_sequence_id": request,
        "anchor_movement_tick_id": anchor_tick,
        "movement_tick_id": movement_tick,
        "movement_tick_offset": offset,
        "classification": classification,
        "errors": list(dict.fromkeys(errors)),
    }


def summarize_ftick(timelines: tuple[dict, ...], *, seed: int) -> dict:
    by_coordinate: dict[tuple[str, int], list[dict]] = {}
    for row in timelines:
        coordinate = (row.get("direction"), row.get("repetition"))
        by_coordinate.setdefault(coordinate, []).append(row)
    normalized = []
    for direction in _DIRECTION_NAMES:
        for repetition in range(10):
            matches = by_coordinate.get((direction, repetition), [])
            if len(matches) == 1:
                normalized.append(matches[0])
                continue
            if matches:
                row = dict(matches[0])
                row["classification"] = "INCOMPLETE"
                row["errors"] = list(dict.fromkeys(
                    [*row.get("errors", ()), "trial_duplicate"]
                ))
                normalized.append(row)
                continue
            normalized.append({
                "trial": f"validation-{_DIRECTION_NAMES.index(direction)}-{repetition}",
                "direction": direction,
                "repetition": repetition,
                "episode_id": "fabric-21001-0",
                "observation_sequence_id": None,
                "generation_id": None,
                "request_sequence_id": None,
                "anchor_movement_tick_id": None,
                "movement_tick_id": None,
                "movement_tick_offset": None,
                "classification": "INCOMPLETE",
                "errors": ["trial_missing"],
            })
    classifications = Counter(row.get("classification") for row in normalized)
    observed_directions = Counter(row.get("direction") for row in timelines)
    directions = Counter(row.get("direction") for row in normalized)
    expected_directions = {name: 10 for name in _DIRECTION_NAMES}
    counts = {name: directions.get(name, 0) for name in _DIRECTION_NAMES}
    valid = (
        seed == 21001
        and len(timelines) == 40
        and counts == expected_directions
        and classifications.get("INCOMPLETE", 0) == 0
        and all(row.get("repetition") in range(10) for row in timelines)
        and len({(row.get("direction"), row.get("repetition"))
                 for row in timelines}) == 40
    )
    return {
        "schema_version": "mc2p.d105-ftick-summary.v1",
        "seed": seed,
        "status": "passed" if valid else "failed",
        "timeline_count": len(normalized),
        "observed_timeline_count": len(timelines),
        "direction_counts": counts,
        "observed_direction_counts": {
            name: observed_directions.get(name, 0) for name in _DIRECTION_NAMES
        },
        "classifications": {
            name: classifications.get(name, 0)
            for name in ("NEXT_MOVEMENT_TICK", "LATER_MOVEMENT_TICK", "INCOMPLETE")
        },
        "timelines": normalized,
    }


def _jsonl(path: Path) -> tuple[dict, ...]:
    return tuple(json.loads(line) for line in path.read_text("utf-8").splitlines()
                 if line.strip())


def _client_sidecar(path: Path) -> tuple[tuple[dict, ...], bool, int, str]:
    try:
        rows = tuple(iter_segmented_jsonl(path))
    except (OSError, ValueError, json.JSONDecodeError):
        return (), False, 0, "unsealed"
    summaries = [row for row in rows
                 if row.get("schema_version") == "mc2p.client-ftick-summary.v1"]
    events = tuple(row for row in rows
                   if row.get("schema_version") == "mc2p.client-ftick-event.v1")
    if (len(summaries) != 1 or rows[-1] is not summaries[0]
            or summaries[0].get("event_count") != len(events)
            or type(summaries[0].get("dropped_event_count")) is not int):
        return events, False, 0, "invalid_summary"
    return events, True, summaries[0]["dropped_event_count"], "sealed"


def _ordered_source_release(
        trace_rows: tuple[dict, ...], result: dict | None = None,
) -> str:
    """Classify explicit owner release or Runtime-close registry clearing."""
    if type(result) is dict:
        lifecycle = result.get("b10_lifecycle")
        release = (
            lifecycle.get("ordered_source_release")
            if type(lifecycle) is dict else None
        )
        if type(release) is dict:
            native = release.get("status")
            if native in {"explicitly_released", "released"}:
                return "explicitly_released"
            if native == "cleared_on_runtime_close":
                return "cleared_on_close"
            if native == "not_released":
                return "not_released"
            return "invalid_status"
    registrations = []
    releases = []
    for row in trace_rows:
        payload = row.get("payload")
        if type(payload) is not dict:
            continue
        source = payload.get("source")
        if type(source) is not dict:
            continue
        if (row.get("record_type") == "ordered_source_registered"
                and payload.get("label") == "b10-gap-solver"):
            registrations.append(source.get("source_id"))
        elif row.get("record_type") == "ordered_source_unregistered":
            releases.append(source.get("source_id"))
    if len(registrations) != 1:
        return "registration_invalid"
    return ("explicitly_released"
            if releases.count(registrations[0]) == 1 else "not_released")


def _source_release_is_accepted(status: str) -> bool:
    return status in {"explicitly_released", "cleared_on_close"}


def analyze_run(run_dir: Path) -> dict:
    root = Path(run_dir).resolve()
    client = root / "client-0"
    result = json.loads((root / "result.json").read_text("utf-8"))
    trials = tuple(row for row in _jsonl(client / "b10-gap-solver-trials.jsonl")
                   if row.get("group") == "validation")
    trace_rows = _jsonl(client / "trace.jsonl")
    source_release_status = _ordered_source_release(trace_rows, result)
    client_rows, sealed, dropped, sidecar_status = _client_sidecar(
        client / "ftick-events",
    )
    timelines = tuple(classify_tick_trace(
        trial, trace_rows, client_rows,
        sidecar_sealed=sealed, dropped_events=dropped,
    ) for trial in trials)
    summary = summarize_ftick(timelines, seed=result.get("seed"))
    failures = []
    if result.get("status") != "passed":
        failures.append("b10_probe_regression")
    if sidecar_status != "sealed":
        failures.append("client_sidecar_" + sidecar_status)
    if not _source_release_is_accepted(source_release_status):
        failures.append("ordered_source_" + source_release_status)
    if failures:
        summary["status"] = "failed"
    summary.update(
        source_run=str(root),
        b10_probe_status=result.get("status"),
        b10_primary_failure=result.get("primary_failure"),
        sidecar_status=sidecar_status,
        sidecar_dropped_events=dropped,
        ordered_source_release_status=source_release_status,
        failures=failures,
    )
    receiver_path = client / "b10-receiver-diagnostics.jsonl"
    if receiver_path.is_file():
        receiver_rows = list(_jsonl(receiver_path))
        summary["receiver_diagnostics"] = _receiver_diagnostic_summary(
            receiver_rows,
        )
    else:
        summary["receiver_diagnostics"] = {
            "schema_version": "mc2p.b10-receiver-diagnostic-summary.v1",
            "status": "unavailable",
            "reason": "b10_receiver_diagnostics_missing",
        }
    return summary


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_evidence(output: Path, summary: dict, *, command: list[str],
                   probe_return_code: int, stdout: str = "", stderr: str = "",
                   evidence_mode: str = "fresh",
                   source_probe_return_code: int | None = None) -> Path:
    if evidence_mode not in {"fresh", "replay"}:
        raise ValueError("F-TICK evidence mode must be fresh or replay")
    target = Path(output).resolve()
    target.mkdir(parents=True, exist_ok=False)
    timelines = tuple(summary.get("timelines", ()))
    with (target / "timelines.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for row in timelines:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    write_json_atomic(target / "summary.json", summary)
    (target / "probe-stdout.txt").write_text(stdout, "utf-8")
    (target / "probe-stderr.txt").write_text(stderr, "utf-8")
    root = Path(__file__).resolve().parents[1]
    source_names = (
        "scripts/d105_ftick.py",
        "scripts/probe_fabric_deployment_observation.py",
        "scripts/b10_gap_solver_runtime.py",
        "scripts/fabric_deployment_sandbox.py",
        "mc2p/backends/runtime_overlays/mc121_diagnostics/ClientFtickDiagnostics.java",
        "mc2p/backends/runtime_overlays/mc121_diagnostics/ClientPhysicsTickDiagnostics.java",
        "mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java",
        "mc2p/backends/runtime_overlays/mc121_structured/BehaviorPlayerMixin.java",
        "deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/mixin/BehaviorPlayerMixin.java",
    )
    files = tuple(target / name for name in (
        "timelines.jsonl", "summary.json", "probe-stdout.txt", "probe-stderr.txt",
    ))
    raw_files = {}
    source_run = summary.get("source_run")
    if type(source_run) is str:
        run = Path(source_run)
        candidates = [
            run / "result.json",
            run / "supervision.json",
            run / "client-0/b10-gap-solver-trials.jsonl",
            run / "client-0/b10-receiver-diagnostics.jsonl",
            run / "client-0/trace.jsonl",
            run / "client-0/ftick-events/complete.json",
            run / "client-0/ftick-events/manifest.jsonl",
            *(run / "client-0/ftick-events").glob("segment-*.jsonl"),
        ]
        for path in candidates:
            if path.is_file():
                raw_files[path.relative_to(run).as_posix()] = {
                    "sha256": _sha256(path), "bytes": path.stat().st_size,
                }
    index = {
        "schema_version": "mc2p.d105-ftick-index.v1",
        "stage": "T3",
        "seed": 21001,
        "formal_platform": "Windows",
        "platform": platform.platform(),
        "evidence_mode": evidence_mode,
        "command": command,
        "analysis_return_code": probe_return_code,
        "source_probe_return_code": (
            probe_return_code if source_probe_return_code is None
            else source_probe_return_code
        ),
        "status": summary.get("status"),
        "source_run": summary.get("source_run"),
        "raw_files": raw_files,
        "sources": {name: _sha256(root / name) for name in source_names},
        "files": {
            path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
            for path in files
        },
    }
    write_json_atomic(target / "index.json", index)
    return target / "index.json"


def _run_dir(stdout: str) -> Path | None:
    prefix = "FABRIC_DEPLOYMENT_RUN_DIR="
    values = [line[len(prefix):].strip() for line in stdout.splitlines()
              if line.startswith(prefix)]
    return Path(values[-1]) if len(values) == 1 else None


def _run_probe(command: list[str], cwd: Path):
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _source_probe_return_code(run_dir: Path) -> int | None:
    supervision = Path(run_dir) / "supervision.json"
    if not supervision.is_file():
        return None
    value = json.loads(supervision.read_text("utf-8")).get("return_code")
    return value if type(value) is int else None


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--server-port", type=int, default=25597)
    parser.add_argument("--ipc-port", type=int, default=8140)
    args = parser.parse_args(argv)
    probe_command = fabric_probe_command(
        server_port=args.server_port, ipc_port=args.ipc_port,
    )
    stdout = stderr = ""
    return_code = 0
    run_dir = args.run_dir
    evidence_mode = "replay" if run_dir is not None else "fresh"
    if run_dir is None:
        completed = _run_probe(probe_command, Path(__file__).resolve().parents[1])
        stdout, stderr = completed.stdout, completed.stderr
        return_code = completed.returncode
        run_dir = _run_dir(stdout)
    command = (probe_command if evidence_mode == "fresh" else [
        sys.executable, str(Path(__file__).resolve()),
        "--output", str(args.output), "--run-dir", str(args.run_dir),
    ])
    source_probe_return_code = (
        return_code if evidence_mode == "fresh" or run_dir is None
        else _source_probe_return_code(run_dir)
    )
    if run_dir is None:
        summary = {
            "schema_version": "mc2p.d105-ftick-summary.v1",
            "seed": 21001,
            "status": "failed",
            "timeline_count": 0,
            "direction_counts": {name: 0 for name in _DIRECTION_NAMES},
            "classifications": {
                "NEXT_MOVEMENT_TICK": 0,
                "LATER_MOVEMENT_TICK": 0,
                "INCOMPLETE": 0,
            },
            "timelines": [],
            "source_run": None,
            "b10_probe_status": "not_started",
            "sidecar_status": "missing",
            "sidecar_dropped_events": 0,
            "ordered_source_release_status": "not_started",
            "failures": ["probe_run_directory_missing"],
        }
    else:
        try:
            summary = analyze_run(run_dir)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            summary = {
                "schema_version": "mc2p.d105-ftick-summary.v1",
                "seed": 21001,
                "status": "failed",
                "timeline_count": 0,
                "direction_counts": {name: 0 for name in _DIRECTION_NAMES},
                "classifications": {
                    "NEXT_MOVEMENT_TICK": 0,
                    "LATER_MOVEMENT_TICK": 0,
                    "INCOMPLETE": 0,
                },
                "timelines": [],
                "source_run": str(Path(run_dir).resolve()),
                "b10_probe_status": "unreadable",
                "sidecar_status": "unreadable",
                "sidecar_dropped_events": 0,
                "ordered_source_release_status": "unreadable",
                "failures": [f"{type(error).__name__}: {error}"],
            }
    analysis_return_code = 0 if summary["status"] == "passed" and return_code == 0 else 2
    write_evidence(
        args.output, summary, command=command,
        probe_return_code=analysis_return_code, stdout=stdout, stderr=stderr,
        evidence_mode=evidence_mode,
        source_probe_return_code=source_probe_return_code,
    )
    print(json.dumps({
        "status": summary["status"],
        "output": str(args.output.resolve()),
        "source_run": summary.get("source_run"),
    }, ensure_ascii=False))
    return analysis_return_code


if __name__ == "__main__":
    raise SystemExit(main())
