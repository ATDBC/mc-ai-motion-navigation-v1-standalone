#!/usr/bin/env python3
"""Read D111 timing evidence and run a report-only B10 calibration.

The calibration is an experiment control for comparing two runs on the same
machine.  It is deliberately not a device-admission check and has no pass/fail
threshold.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))


SCHEMA_VERSION = "mc2p.d112-delivery-baseline.v1"


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _summary(values: Sequence[float], *, include_values: bool = False) -> dict:
    result = {
        "samples": len(values),
        "minimum": min(values) if values else None,
        "p50": _percentile(values, .50),
        "p95": _percentile(values, .95),
        "p99": _percentile(values, .99),
        "maximum": max(values) if values else None,
    }
    if include_values:
        result["values"] = list(values)
    return result


def _iter_jsonl(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            yield value


def _control_prepare(trace_rows: Iterable[dict]) -> dict:
    values: list[float] = []
    missing = invalid = 0
    for row in trace_rows:
        if row.get("record_type") != "step":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            missing += 1
            continue
        received = payload.get("decision_observation_received_ns")
        dispatched = payload.get("dispatched_monotonic_ns")
        if type(received) is not int or type(dispatched) is not int:
            missing += 1
        elif dispatched < received:
            invalid += 1
        else:
            values.append((dispatched - received) / 1_000_000.0)
    return {
        **_summary(values),
        "missing": missing,
        "invalid": invalid,
        "definition": (
            "trace step dispatched_monotonic_ns minus "
            "decision_observation_received_ns"
        ),
    }


def _coordinator_cost(frames: Iterable[dict]) -> dict:
    values: list[float] = []
    missing = invalid = 0
    for row in frames:
        proposal = row.get("proposal_elapsed_ns")
        outside = row.get("runtime_outside_backend_ns")
        if type(proposal) is not int or type(outside) is not int:
            missing += 1
        elif proposal < 0 or outside < 0:
            invalid += 1
        else:
            values.append((proposal + outside) / 1_000_000.0)
    return {
        **_summary(values, include_values=True),
        "missing": missing,
        "invalid": invalid,
        "definition": "proposal_elapsed_ns plus runtime_outside_backend_ns",
    }


def summarize_formal_delivery(
    receiver_rows: Iterable[dict], coordinator_frames: Iterable[dict],
) -> dict:
    """Join first candidate consumption to formal command windows by trial."""
    chains: dict[str, dict[str, dict]] = {}
    consumes: dict[str, int] = {}
    for row in receiver_rows:
        trial, identity = row.get("trial"), row.get("identity")
        if isinstance(trial, str) and isinstance(identity, str):
            chains.setdefault(trial, {}).setdefault(identity, {})[
                str(row.get("event"))
            ] = row
        if row.get("event") != "first_control_consume":
            continue
        tick = row.get("movement_tick")
        if isinstance(trial, str) and type(tick) is int:
            consumes[trial] = min(tick, consumes.get(trial, tick))

    by_trial: dict[str, list[dict]] = {}
    all_verified_window_misses = 0
    exact_window_applications = 0
    verified_application_count = 0
    for row in coordinator_frames:
        trial = row.get("trial")
        command_index = row.get("verified_command_index")
        earliest, latest = (
            row.get("expected_movement_tick"), row.get("latest_movement_tick"),
        )
        actual_ticks = row.get("actual_movement_ticks")
        if (not isinstance(trial, str) or type(command_index) is not int
                or type(earliest) is not int or type(latest) is not int
                or not isinstance(actual_ticks, list)
                or not actual_ticks or any(type(value) is not int for value in actual_ticks)):
            continue
        actual = min(actual_ticks)
        verified_application_count += 1
        # A command at latest is in-window, including a one-tick exact window.
        if actual > latest:
            all_verified_window_misses += 1
        if earliest == latest and actual == latest:
            exact_window_applications += 1
        by_trial.setdefault(trial, []).append({
            "command_index": command_index,
            "grant_movement_tick": row.get("grant_movement_tick"),
            "motion_identity": row.get("motion_identity"),
            "control_sequence_id": row.get("control_sequence_id"),
            "earliest": earliest,
            "latest": latest,
            "actual": actual,
        })

    leads: list[int] = []
    margins: list[int] = []
    first_misses = 0
    incomplete: list[str] = []
    details: list[dict] = []
    for trial in sorted(set(consumes) | set(by_trial)):
        first = min(by_trial.get(trial, ()), key=lambda row: row["command_index"], default=None)
        consume = consumes.get(trial)
        if first is None or consume is None or first["command_index"] != 0:
            incomplete.append(trial)
            continue
        lead = first["earliest"] - consume
        margin = first["latest"] - first["actual"]
        missed = first["actual"] > first["latest"]
        leads.append(lead)
        margins.append(margin)
        first_misses += int(missed)
        expected_identity = first.get("motion_identity")
        consume_rows = tuple(
            (identity, events)
            for identity, events in chains.get(trial, {}).items()
            if (
                isinstance(expected_identity, str)
                and identity == expected_identity
            ) or (
                expected_identity is None
                and events.get("first_control_consume", {}).get("movement_tick")
                    == consume
            )
        )
        identity, events = consume_rows[0] if len(consume_rows) == 1 else (None, {})
        submit = events.get("worker_submit", {})
        observed = events.get("receiver_observed_result", {})
        publish = events.get("registry_publish", {})
        completed_ns = observed.get("worker_completed_monotonic_ns")
        observed_ns = observed.get("monotonic_ns")
        complete_to_receiver = (
            observed_ns - completed_ns
            if type(completed_ns) is int and completed_ns > 0
            and type(observed_ns) is int and observed_ns >= completed_ns
            else None
        )
        identity_consume = events.get("first_control_consume", {}).get(
            "movement_tick",
        )
        if type(identity_consume) is int:
            consume = identity_consume
            lead = first["earliest"] - consume
            leads[-1] = lead
        complete_chain = all(
            type(value) is int for value in (
                submit.get("movement_tick"), observed.get("movement_tick"),
                publish.get("movement_tick"), identity_consume,
                first.get("grant_movement_tick"), first["actual"],
                first.get("control_sequence_id"),
            )
        ) and (
            submit["movement_tick"] <= observed["movement_tick"]
            <= publish["movement_tick"] <= identity_consume
            <= first["grant_movement_tick"] < first["actual"]
        ) and complete_to_receiver is not None
        details.append({
            "trial": trial,
            "identity": identity,
            "worker_submit_tick": submit.get("movement_tick"),
            "worker_elapsed_ns": observed.get("worker_elapsed_ns"),
            "worker_complete_to_receiver_ns": complete_to_receiver,
            "receiver_observed_tick": observed.get("movement_tick"),
            "registry_publish_tick": publish.get("movement_tick"),
            "first_candidate_consume_tick": consume,
            "grant_movement_tick": first.get("grant_movement_tick"),
            "control_sequence_id": first.get("control_sequence_id"),
            "earliest_tick": first["earliest"],
            "latest_tick": first["latest"],
            "first_actual_tick": first["actual"],
            "consume_to_earliest_tick_lead": lead,
            "first_input_to_latest_tick_margin": margin,
            "window_missed": missed,
            "complete_identity_chain": complete_chain,
        })
    return {
        "trial_count": len(details),
        "complete_identity_chain_count": sum(
            item["complete_identity_chain"] for item in details
        ),
        "incomplete_trials": incomplete,
        "consume_to_earliest_tick_lead": _summary(leads, include_values=True),
        "first_input_to_latest_tick_margin": _summary(margins, include_values=True),
        "first_input_window_miss_count": first_misses,
        "verified_application_count": verified_application_count,
        "all_verified_window_miss_count": all_verified_window_misses,
        "exact_window_application_count": exact_window_applications,
        "window_rule": "miss only when actual_movement_tick > latest_movement_tick",
        "trials": details,
    }


def analyze_client_directory(client: Path) -> dict:
    client = Path(client)
    required = {
        "trace": client / "trace.jsonl",
        "frames": client / "b10-coordinator-control-frames.jsonl",
        "receiver": client / "b10-receiver-diagnostics.jsonl",
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing D111 evidence files: {', '.join(missing)}")
    frames = tuple(_iter_jsonl(required["frames"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "client_directory": client.as_posix(),
        "trace_control_prepare_ms": _control_prepare(_iter_jsonl(required["trace"])),
        "coordinator_proposal_plus_runtime_outside_ms": _coordinator_cost(frames),
        "formal_candidate_delivery": summarize_formal_delivery(
            _iter_jsonl(required["receiver"]), frames,
        ),
    }


def _calibration_fixture():
    from mc2p.motion_nav.online_motion import (
        CandidateExecutionWindow, MotionTickPhase, StateAnchor,
    )
    from mc2p.motion_nav.physics_adapter import PhysicsWorldView
    from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, PhysicsState
    from mc2p.motion_nav.world_model import (
        BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
    )
    from mc2p.motion_nav.motion_solver import GapSolveRequest, LandingRegion

    session = WorldSessionId("d112-reference-calibration")
    stamp = ObservationStamp(session, 0, 0, "d112", 0)
    knowledge = WorldKnowledge(session)
    gap, target_cell = (0, 63, 1), (0, 63, 2)
    cells = tuple((x, y, z) for x in range(-4, 5)
                  for y in range(40, 71) for z in range(-4, 5))
    knowledge.confirm_air(stamp, cells)
    blocks = {
        (x, 63, z): BlockGeometry.full_cube("minecraft:grass_block")
        for x in range(-4, 5) for z in range(-4, 5)
        if (x, 63, z) not in {gap, target_cell}
    }
    blocks[target_cell] = BlockGeometry.full_cube("minecraft:grass_block")
    knowledge.observe_blocks(stamp, blocks)
    state = PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema,
        session, 10, (.5, 64., .5), (0., -.0784000015258789, .04),
        0., 0., "standing", .6, 1.8, True, False, True,
        False, False, 0, 0., .1, .6, .08, .42, 20, 5., "survival",
        (), False, False, False, False, False, False,
    )
    anchor = StateAnchor(
        session, 3, 10, MotionTickPhase.AFTER_MOVEMENT,
        None, None, JAVA_1_21_RULESET.ruleset_id,
        JAVA_1_21_RULESET.state_schema, "mc2p.input-projection.v1", state,
    )
    request = GapSolveRequest(
        (0, 1), LandingRegion(.3, .7, 2.3, 2.7, 64.),
        CandidateExecutionWindow(11, 12), max_candidates=1, max_ticks=20,
    )
    return anchor, PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET), request


def _proof_signature(result) -> str:
    if result.proof is None:
        value = {"status": result.status.value, "reasons": result.reasons}
    else:
        value = {
            "status": result.status.value,
            "tick_inputs": [asdict(item) for item in result.proof.tick_inputs],
        }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_calibration(*, warmups: int = 3, samples: int = 20, rounds: int = 2) -> dict:
    """Run fixed R4 work; report drift without accepting or rejecting a device."""
    if min(warmups, samples, rounds) < 0 or samples == 0 or rounds == 0:
        raise ValueError("warmups must be nonnegative; samples and rounds must be positive")
    from mc2p.motion_nav.motion_solver import solve_one_cell_gap

    anchor, world, request = _calibration_fixture()
    rows = []
    for round_index in range(rounds):
        for _ in range(warmups):
            solve_one_cell_gap(anchor, world, request)
        wall_values: list[float] = []
        cpu_values: list[float] = []
        signatures: set[str] = set()
        workloads: set[tuple] = set()
        last_result = None
        for _ in range(samples):
            wall_start, cpu_start = time.perf_counter_ns(), time.thread_time_ns()
            result = solve_one_cell_gap(anchor, world, request)
            cpu_end, wall_end = time.thread_time_ns(), time.perf_counter_ns()
            wall_values.append((wall_end - wall_start) / 1_000_000.0)
            cpu_values.append((cpu_end - cpu_start) / 1_000_000.0)
            signatures.add(_proof_signature(result))
            workload = (
                result.status.value, result.candidates_evaluated,
                0 if result.proof is None else len(result.proof.commands),
                0 if result.proof is None else len(result.proof.tick_inputs),
                0 if result.proof is None else len(result.proof.trajectory),
                0 if result.proof is None else len(result.proof.world_dependencies),
            )
            workloads.add(workload)
            last_result = result
        if len(signatures) != 1 or len(workloads) != 1 or last_result is None:
            raise RuntimeError("fixed calibration produced nondeterministic work")
        workload = next(iter(workloads))
        rows.append({
            "round": round_index + 1,
            "warmups": warmups,
            "wall_ms": _summary(wall_values),
            "cpu_ms": _summary(cpu_values),
            "input_signature_sha256": next(iter(signatures)),
            "workload": {
                "status": workload[0], "candidates_evaluated": workload[1],
                "command_count": workload[2], "tick_input_count": workload[3],
                "trajectory_state_count": workload[4],
                "world_dependency_count": workload[5],
            },
        })
    medians = [row["wall_ms"]["p50"] for row in rows]
    baseline = medians[0]
    drift = [None if baseline == 0 else (value / baseline - 1.0) * 100.0
             for value in medians[1:]]
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "reference_calibration_only",
        "device_admission": False,
        "timing_note": (
            "Compare paired runs on the same machine; runtime degrades by tick "
            "margin rather than rejecting slower devices."
        ),
        "rounds": rows,
        "wall_median_drift_percent_from_first_round": drift,
    }


def _write(path: Path | None, value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(encoded, end="")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze")
    analyze.add_argument("artifact", type=Path)
    analyze.add_argument("--output", type=Path)
    calibration = subparsers.add_parser("calibration")
    calibration.add_argument("--warmups", type=int, default=3)
    calibration.add_argument("--samples", type=int, default=20)
    calibration.add_argument("--rounds", type=int, default=2)
    calibration.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "analyze":
        client = args.artifact / "client-0" if (args.artifact / "client-0").is_dir() else args.artifact
        report = analyze_client_directory(client)
    else:
        report = run_calibration(
            warmups=args.warmups, samples=args.samples, rounds=args.rounds,
        )
    _write(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
