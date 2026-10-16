"""Frozen D101 M0 Windows gates and auditable, deterministic evidence files."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

from .d101_continuous_probe import (
    FROZEN_INPUT_HASH, SingleJumpProbeSpec, run_single_jump_probe,
)
from .d101_input_stream import StreamStartBranch
from .m0_probe import DurationSummary, summarize_durations
from .physics import _canonical, trajectory_digest
from .scenarios import representative_fixture


ROOT = Path(__file__).resolve().parents[3]
SCHEMA_VERSION = "d101-m0-v1"
SOURCE_KIND = "PROJECT_WINDOWS"
NEGATIVE_EXPECTED = {
    "first_late_two": "CONTRACT_REJECTED",
    "middle_late_one": "BOUNDED_DEVIATION",
    "duplicate_first": "CONTRACT_REJECTED",
    "out_of_order_first": "CONTRACT_REJECTED",
}


@dataclass(frozen=True, slots=True)
class M0Summary:
    schema_version: str
    source_kind: str
    source_hashes: dict[str, str]
    source_tree_hash: str
    input_hash: str
    rounds: int
    positive_groups: dict[str, dict[str, int]]
    negative_classifications: dict[str, str]
    decision_count: int
    compare_only_count: int
    key_node_comparisons: int
    full_proof_calls: int
    key_node_full_proof_calls: int
    physics_steps: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    safety_violations: int
    safety_events: dict[str, int]
    wrong_branch_survivals: int
    unowned_advances: int
    wrong_generation_acceptances: int
    passed: bool
    gate_failures: tuple[str, ...]


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    paths = set()
    # Record the actual local Python import closure, including the unchanged
    # production calculator. These files are read, never modified by D101.
    for module in tuple(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.suffix == ".py" and path.is_relative_to(ROOT) and ".venv" not in path.parts:
                paths.add(path)
    for relative in (
        "scripts/measure_continuous_motion_d101.py",
        "tests/motion_nav/test_d101_input_stream.py",
        "tests/motion_nav/test_d101_continuous_probe.py",
        "tests/motion_nav/test_d101_measurement.py",
    ):
        paths.add(ROOT / relative)
    return {path.relative_to(ROOT).as_posix(): _file_hash(path) for path in sorted(paths)}


def summarize_decision_times(rows) -> DurationSummary:
    values = tuple((row["duration_ns"] if isinstance(row, dict) else row.duration_ns) / 1000000.
                   for row in rows)
    return summarize_durations(values)


def _summarize(runs, decisions, *, rounds, hashes, source_kind):
    if source_kind != SOURCE_KIND:
        raise ValueError("E13/E14 and other reference sources cannot enter the Windows M0 gate")
    positives = [run for run in runs if run["injection"] == "none"]
    groups = {}
    failures = []
    if rounds != 100:
        failures.append("formal_rounds_not_100")
    for branch, label in (("ON_TIME", "on_time"), ("LATE_ONE", "late_one")):
        group = [run for run in positives if run["start_branch"] == branch]
        completed = sum(run["terminal_status"] == "COMPLETED" and run["final_state"]["on_ground"]
                        for run in group)
        groups[label] = {"runs": len(group), "completed": completed}
        if len(group) != rounds or completed != rounds:
            failures.append(f"{label}_completion")
    negatives = {run["injection"]: run["terminal_status"] for run in runs if run["injection"] != "none"}
    if negatives != NEGATIVE_EXPECTED or len(runs) - len(positives) != len(NEGATIVE_EXPECTED):
        failures.append("negative_classification")
    times = summarize_decision_times(decisions)
    safety = Counter(event for run in runs for event in run["safety_events"])
    unknown = sum("UNKNOWN" in row["reason"] and row["actual_effect_tick"] is not None for row in decisions)
    if unknown:
        safety["unknown_advance"] += unknown
    run_by_generation = {run["generation"]["generation_id"]: run for run in runs}
    unowned = wrong_generation = wrong_branches = 0
    seen_first = set()
    sequences = {}
    for row in decisions:
        gid = row["generation_id"]
        run = run_by_generation.get(gid)
        if row["actual_effect_tick"] is not None:
            unowned += int(not row["owner_id"] or not row["evidence_hash"]
                           or row["advance_source"] not in ("candidate", "proved_start_prelude", "proved_stop_tail")
                           or row["applied_control_sequence"] is None)
            wrong_generation += int(run is None or row["owner_id"] != run["generation"]["owner_id"])
            sequence = row["applied_control_sequence"]
            if sequence in sequences and sequences[sequence] != gid:
                wrong_generation += 1
            sequences[sequence] = gid
        if run and run["injection"] == "none":
            if row["first_receipt"] is not None:
                seen_first.add(gid)
            if gid in seen_first:
                wrong_branches += int(row["surviving_branches"] != [run["start_branch"]])
    if any(run["generation"]["generation_id"] not in seen_first for run in positives):
        failures.append("first_receipt_missing")
    keys = [check for row in decisions for check in row["key_checks"]]
    compare_count = sum(check["kind"] == "COMPARE_ONLY" for check in keys)
    key_full = sum(check["full_proof_calls"] for check in keys)
    if len(keys) != 2 * len(positives) or compare_count != len(keys) or key_full:
        failures.append("key_node_compare_only")
    if safety:
        failures.append("safety_violation")
    if unowned:
        failures.append("unowned_advance")
    if wrong_generation:
        failures.append("wrong_generation_acceptance")
    if wrong_branches:
        failures.append("wrong_start_branch_survival")
    if times.p95_ms > 8.:
        failures.append("p95_above_8ms")
    if times.p99_ms > 15.:
        failures.append("p99_above_15ms")
    if times.max_ms >= 30.:
        failures.append("max_not_below_30ms")
    input_hashes = {run["input_hash"] for run in runs}
    if input_hashes != {FROZEN_INPUT_HASH}:
        failures.append("frozen_input_changed")
    full_count = sum(row["full_proof_calls"] for row in decisions)
    physics_count = sum(row["physics_steps"] for row in decisions)
    if full_count != sum(run["full_proof_calls"] for run in runs) or physics_count != sum(run["physics_steps"] for run in runs):
        failures.append("raw_counter_mismatch")
    return M0Summary(
        SCHEMA_VERSION, source_kind, hashes, trajectory_digest(hashes), FROZEN_INPUT_HASH,
        rounds, groups, negatives, len(decisions), compare_count, len(keys), full_count, key_full,
        physics_count, times.p50_ms, times.p95_ms, times.p99_ms, times.max_ms,
        sum(safety.values()), dict(sorted(safety.items())), wrong_branches, unowned,
        wrong_generation, not failures, tuple(failures),
    )


def summarize_m0(results, *, rounds, source_kind=SOURCE_KIND):
    if source_kind != SOURCE_KIND:
        raise ValueError("reference data cannot substitute for PROJECT_WINDOWS")
    runs = [_canonical(result) for result in results]
    decisions = [row for run in runs for row in run.pop("decisions")]
    return _summarize(runs, decisions, rounds=rounds, hashes=source_hashes(), source_kind=source_kind)


def _write_json(path, value):
    path.write_text(json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8", newline="\n")


def _write_gzip_jsonl(path, values):
    with path.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
        for value in values:
            text = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False) + "\n"
            stream.write(text.encode("utf-8"))


def _read_gzip_jsonl(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def run_m0(*, rounds: int, output: Path) -> M0Summary:
    if platform.system() != "Windows":
        raise ValueError("formal D101 M0 requires the project's Windows environment")
    if type(rounds) is not int or rounds < 1:
        raise ValueError("M0 rounds must be positive")
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("refusing to overwrite existing D101 evidence")
    results = []
    for branch in StreamStartBranch:
        results.extend(run_single_jump_probe(SingleJumpProbeSpec(branch, rounds)))
    for injection in NEGATIVE_EXPECTED:
        results.extend(run_single_jump_probe(SingleJumpProbeSpec(StreamStartBranch.ON_TIME, 1, injection)))
    summary = summarize_m0(tuple(results), rounds=rounds)
    hashes = summary.source_hashes
    output.mkdir(parents=True, exist_ok=True)
    runs = [_canonical(result) for result in results]
    decisions = [row for run in runs for row in run.pop("decisions")]
    _write_gzip_jsonl(output / "runs.jsonl.gz", runs)
    _write_gzip_jsonl(output / "decisions.jsonl.gz", decisions)
    fixture = representative_fixture("jump_gap_1")
    facts = tuple((pos, fixture.world.cell(pos)) for pos in
                  ((x, y, z) for x in range(-3, 4) for y in range(-50, 8) for z in range(-3, 13)))
    _write_gzip_jsonl(output / "fixture.jsonl.gz", ({"request": _canonical(fixture.request),
                                                   "world_facts": _canonical(facts),
                                                   "inputs": _canonical(results[0].inputs)},))
    _write_json(output / "source-hashes.json", hashes)
    _write_json(output / "summary.json", summary)
    files = {path.name: _file_hash(path) for path in sorted(output.iterdir()) if path.is_file()}
    manifest = {
        "schema_version": SCHEMA_VERSION, "stage": "M0", "source_kind": SOURCE_KIND,
        "git_parent_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_tree_hash": summary.source_tree_hash, "source_hashes": hashes,
        "input_hash": FROZEN_INPUT_HASH, "rounds": rounds,
        "platform": platform.platform(), "python": sys.version,
        "timing_scope": "complete decisions: initial full proof, protocol, dependency/permission checks, physical advancement, key-node comparison and result construction; excludes fixture/input preparation and evidence writes",
        "in_flight_premise": "no future command has been sampled at decision boundaries; scanner verifies empty irrevocable-input declarations",
        "input_producer": "m0_probe.run_case('jump_gap_1', 'A3', options=FORMAL_OPTIONS).inputs; hash asserted before measurement",
        "sequence_allocation": "monotonic process-global generation serial; disjoint reserved control_sequence ranges of 128",
        "negative_injection": "middle input is offered after its deadline and rejected before application; due tick executes the pre-proved stop tail",
        "gzip_mtime": 0, "artifacts": files,
        "worker_created": 0, "worker_alive_at_end": 0,
        "passed": summary.passed, "gate_failures": summary.gate_failures,
        "formal_round_count_met": rounds == 100,
    }
    _write_json(output / "manifest.json", manifest)
    if recompute_m0(output) != summary:
        raise ValueError("written M0 evidence does not reproduce its summary")
    return summary


def recompute_m0(output: Path) -> M0Summary:
    """Verify every artifact hash and reconstruct gates without new timings."""
    output = Path(output)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if manifest["source_kind"] != SOURCE_KIND or manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("not a project Windows D101 M0 artifact")
    for filename, expected in manifest["artifacts"].items():
        if _file_hash(output / filename) != expected:
            raise ValueError(f"D101 evidence hash mismatch: {filename}")
    hashes = json.loads((output / "source-hashes.json").read_text(encoding="utf-8"))
    return _summarize(_read_gzip_jsonl(output / "runs.jsonl.gz"),
                      _read_gzip_jsonl(output / "decisions.jsonl.gz"),
                      rounds=manifest["rounds"], hashes=hashes, source_kind=manifest["source_kind"])
