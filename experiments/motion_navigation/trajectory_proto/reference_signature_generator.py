"""Rebuild the independent D098 scanner signatures from frozen inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from . import m0_probe
from .commitment import scan_commitment
from .physics import trajectory_digest
from .reference_search import _boundary_evidence


ROOT = Path(__file__).resolve().parents[3]
DATA = Path(__file__).resolve().parent / "data"
BASELINE_COMMIT = "d01d378f"
SCANNER = Path(__file__).resolve().parent / "commitment.py"
INPUT = DATA / "d097a-set-a-sample.json"
OUTPUT = DATA / "d098-reference-signatures-v1.json"
COMMAND = ".venv/python.exe scripts/generate_d098_reference_signatures.py --check"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tail_signature(tail) -> str:
    return trajectory_digest((tail.boundary, tail.status, tail.inputs, tail.states,
                              tail.dependencies, tail.damage_points, tail.absolute_tick))


def generate_payload() -> dict:
    frozen = json.loads(INPUT.read_text(encoding="utf-8"))["inputs"]
    rows = []
    for case, raw_inputs in frozen.items():
        scenario, tier = case.split(":")
        fixture = m0_probe._fixture_for(scenario, tier or None)
        inputs = tuple(type(fixture.request.stop_input)(*row) for row in raw_inputs)
        result = scan_commitment(fixture.request, inputs, fixture.world,
                                 _boundary_evidence(fixture.request, inputs))
        branches = []
        for branch in result.proof.branches:
            branches.append({
                "damage": branch.damage_points,
                "risk": [[item.last_abandon_boundary, item.first_committed_boundary,
                          item.recovered_boundary, item.last_abandon_tick,
                          item.first_committed_tick, item.commitment_effect_tick,
                          item.recovered_tick] for item in branch.risk_intervals],
                "states_hash": trajectory_digest(branch.states),
                "tail_hashes": [_tail_signature(tail) for tail in branch.tails],
            })
        dependencies = [[list(position), trajectory_digest(fact)]
                        for position, fact in result.proof.dependency_facts]
        rows.append({
            "branches": branches,
            "case": case,
            "counts": {
                "nodes": result.counts.nodes,
                "physics_steps": result.counts.physics_steps,
                "tail_ticks": result.counts.tail_ticks,
            },
            "dependencies": dependencies,
            "input_hash": trajectory_digest(inputs),
            "reason": None if result.reason is None else result.reason.value,
            "status": result.status.value,
            "trajectory_hash": result.proof.trajectory_hash,
        })
    return {
        "generated_from": "independent batch scanner before shared P1 rollout implementation",
        "provenance": {
            "baseline_commit": BASELINE_COMMIT,
            "command": COMMAND,
            "input_path": str(INPUT.relative_to(ROOT)).replace("\\", "/"),
            "input_sha256": _sha256(INPUT),
            "scanner_path": str(SCANNER.relative_to(ROOT)).replace("\\", "/"),
            "scanner_sha256": _sha256(SCANNER),
        },
        "rows": rows,
        "schema": "d098-independent-reference-signatures-v2",
    }


def serialized_payload() -> str:
    return json.dumps(generate_payload(), sort_keys=True, indent=1) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    text = serialized_payload()
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != text:
            raise SystemExit("reference signature file is stale; regenerate it")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8", newline="\n")
    return 0
