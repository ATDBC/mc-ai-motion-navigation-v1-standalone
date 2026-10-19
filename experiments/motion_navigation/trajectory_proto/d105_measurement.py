"""D105 T1-A3 Windows M0 gates over the frozen D101 workload."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import platform
import subprocess
import sys

from .d101_continuous_probe import FROZEN_INPUT_HASH, SingleJumpProbeSpec
from .d101_input_stream import StreamStartBranch
from .d101_measurement import (
    M0Summary,
    NEGATIVE_EXPECTED,
    SOURCE_KIND,
    _file_hash,
    _read_gzip_jsonl,
    _summarize,
    _write_gzip_jsonl,
    _write_json,
)
from .d105_continuous_probe import run_sound_single_jump_probe
from .physics import _canonical, trajectory_digest
from .scenarios import representative_fixture


ROOT = Path(__file__).resolve().parents[3]
SCHEMA_VERSION = "d105-sound-sparse-m0-v1"
FORMAL_ROUNDS = 100
FORMAL_RUNS = 204
FORMAL_DECISIONS = 4710


def source_hashes():
    paths = set()
    for module in tuple(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename:
            path = Path(filename).resolve()
            if path.suffix == ".py" and path.is_relative_to(ROOT) and ".venv" not in path.parts:
                paths.add(path)
    for relative in (
        "scripts/measure_sound_sparse_motion_d105.py",
        "tests/motion_nav/test_d105_sound_sparse_m0.py",
        "tests/motion_nav/test_d101_continuous_probe.py",
        "tests/motion_nav/test_d101_measurement.py",
    ):
        paths.add(ROOT / relative)
    return {path.relative_to(ROOT).as_posix(): _file_hash(path) for path in sorted(paths)}


def _with_d105_gates(summary: M0Summary, *, rounds: int) -> M0Summary:
    failures = list(summary.gate_failures)
    if rounds == FORMAL_ROUNDS:
        if sum(group["runs"] for group in summary.positive_groups.values()) + len(
                summary.negative_classifications) != FORMAL_RUNS:
            failures.append("formal_run_count_not_204")
        if summary.decision_count != FORMAL_DECISIONS:
            failures.append("formal_decision_count_not_4710")
        if summary.full_proof_calls != FORMAL_RUNS:
            failures.append("formal_full_proof_count_not_204")
    failures = tuple(dict.fromkeys(failures))
    return replace(
        summary,
        schema_version=SCHEMA_VERSION,
        passed=not failures,
        gate_failures=failures,
    )


def summarize_m0(results, *, rounds: int, hashes=None, source_kind=SOURCE_KIND) -> M0Summary:
    runs = [_canonical(result) for result in results]
    decisions = [row for run in runs for row in run.pop("decisions")]
    summary = _summarize(
        runs,
        decisions,
        rounds=rounds,
        hashes=source_hashes() if hashes is None else hashes,
        source_kind=source_kind,
    )
    return _with_d105_gates(summary, rounds=rounds)


def run_m0(*, rounds: int, output: Path) -> M0Summary:
    if platform.system() != "Windows":
        raise ValueError("formal D105 M0 requires the project's Windows environment")
    if type(rounds) is not int or rounds < 1:
        raise ValueError("M0 rounds must be positive")
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("refusing to overwrite existing D105 evidence")

    results = []
    for branch in StreamStartBranch:
        results.extend(run_sound_single_jump_probe(SingleJumpProbeSpec(branch, rounds)))
    for injection in NEGATIVE_EXPECTED:
        results.extend(run_sound_single_jump_probe(SingleJumpProbeSpec(
            StreamStartBranch.ON_TIME, 1, injection)))
    hashes = source_hashes()
    summary = summarize_m0(tuple(results), rounds=rounds, hashes=hashes)

    output.mkdir(parents=True, exist_ok=True)
    runs = [_canonical(result) for result in results]
    decisions = [row for run in runs for row in run.pop("decisions")]
    _write_gzip_jsonl(output / "runs.jsonl.gz", runs)
    _write_gzip_jsonl(output / "decisions.jsonl.gz", decisions)
    fixture = representative_fixture("jump_gap_1")
    facts = tuple((position, fixture.world.cell(position)) for position in
                  ((x, y, z) for x in range(-3, 4)
                   for y in range(-50, 8) for z in range(-3, 13)))
    _write_gzip_jsonl(output / "fixture.jsonl.gz", ({
        "request": _canonical(fixture.request),
        "world_facts": _canonical(facts),
        "inputs": _canonical(results[0].inputs),
    },))
    _write_json(output / "source-hashes.json", hashes)
    _write_json(output / "summary.json", summary)
    files = {path.name: _file_hash(path)
             for path in sorted(output.iterdir()) if path.is_file()}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "stage": "T1-A3/M0",
        "source_kind": SOURCE_KIND,
        "git_parent_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_tree_hash": trajectory_digest(hashes),
        "source_hashes": hashes,
        "input_hash": FROZEN_INPUT_HASH,
        "rounds": rounds,
        "platform": platform.platform(),
        "python": sys.version,
        "scanner": "SoundSparseProof",
        "legacy_d101_default_changed": False,
        "deviation_tail_policy": (
            "execute only a SAFE_STOP tail already stored in the immutable proof; "
            "no runtime reproof"
        ),
        "timing_scope": (
            "D101 complete-decision scope with SoundSparseProof initial scan; "
            "excludes fixture/input preparation and evidence writes"
        ),
        "percentile": "nearest-rank",
        "thresholds_ms": {"p95_max": 8, "p99_max": 15, "max_strictly_below": 30},
        "gzip_mtime": 0,
        "artifacts": files,
        "passed": summary.passed,
        "gate_failures": summary.gate_failures,
        "formal_round_count_met": rounds == FORMAL_ROUNDS,
        "formal_run_count": len(runs),
        "formal_decision_count": len(decisions),
        "historical_evidence_rewritten": False,
    }
    _write_json(output / "manifest.json", manifest)
    if recompute_m0(output) != summary:
        raise ValueError("written D105 M0 evidence does not reproduce its summary")
    return summary


def recompute_m0(output: Path) -> M0Summary:
    output = Path(output)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["source_kind"] != SOURCE_KIND
            or manifest["schema_version"] != SCHEMA_VERSION):
        raise ValueError("not a project Windows D105 M0 artifact")
    for filename, expected in manifest["artifacts"].items():
        if _file_hash(output / filename) != expected:
            raise ValueError(f"D105 evidence hash mismatch: {filename}")
    hashes = json.loads((output / "source-hashes.json").read_text(encoding="utf-8"))
    base = _summarize(
        _read_gzip_jsonl(output / "runs.jsonl.gz"),
        _read_gzip_jsonl(output / "decisions.jsonl.gz"),
        rounds=manifest["rounds"],
        hashes=hashes,
        source_kind=manifest["source_kind"],
    )
    return _with_d105_gates(base, rounds=manifest["rounds"])
