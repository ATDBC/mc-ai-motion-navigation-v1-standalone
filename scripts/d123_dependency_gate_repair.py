"""D123-0 review fix 1: append formal D122 gate results to historical probes.

run-1/offline_dependency_gate was only a knowledge check: it did not check
mismatches and is not a formal dependency gate or Runtime admission result.
This repair reuses inspect_fabric_dependency_knowledge without replaying physics.
"""
from __future__ import annotations

import argparse
from collections import Counter
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts import d123_dependency_probe as probe
from scripts.d122_fabric_timing import inspect_fabric_dependency_knowledge


class DependencyGateStatus(StrEnum):
    PASS = "PASS"
    NEEDS_WORLD = "NEEDS_WORLD"
    FACT_MISMATCH = "FACT_MISMATCH"
    NEEDS_WORLD_AND_FACT_MISMATCH = "NEEDS_WORLD_AND_FACT_MISMATCH"


def formal_gate(case, changes):
    """Use the existing D122 checker against the unmodified expected fixture."""
    _, fixture, _, _, snapshot = case
    canonical = probe._canonical_fabric_dependency_manifest(fixture.trial.direction_index)
    source = dict(snapshot.facts)
    manifest = {
        "facts": {position: {
            "knowledge": source[position].knowledge.value,
            "material": None if source[position].block is None else source[position].block.material_key,
        } for position in canonical["required"]},
        "categories": canonical["categories"],
    }
    checked = inspect_fabric_dependency_knowledge(probe.world_for(snapshot, changes), manifest)
    unknown, mismatch = bool(checked["unknown"]), bool(checked["mismatched"])
    if unknown and mismatch:
        status = DependencyGateStatus.NEEDS_WORLD_AND_FACT_MISMATCH
    elif unknown:
        status = DependencyGateStatus.NEEDS_WORLD
    elif mismatch:
        status = DependencyGateStatus.FACT_MISMATCH
    else:
        status = DependencyGateStatus.PASS
    return {**checked, "unknown_count": len(checked["unknown"]),
            "mismatch_count": len(checked["mismatched"]), "status": status,
            "accepted": status is DependencyGateStatus.PASS}


def changes_for(case, row):
    snapshot = case[4]
    if row["kind"] == "baseline":
        return {}
    if row["kind"] == "support_mutation":
        position = tuple(row["position"])
        above = (position[0], position[1] + 1, position[2])
        return {position: probe.UNKNOWN_CELL,
                above: probe.fact_for(snapshot, above, row["support"])}
    positions = row.get("positions", [row.get("position")])
    return {tuple(position): probe.fact_for(snapshot, tuple(position), row["mode"])
            for position in positions}


def repair(raw, output, d120_source):
    if output.exists():
        raise ValueError("append-only repair output must not exist")
    raw_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in raw.iterdir() if p.is_file()}
    output.mkdir(parents=True)
    historical = probe.d120_comparison(d120_source, output)
    mutations = json.loads((raw / "mutations.json").read_text(encoding="utf-8"))
    cases = {direction: probe.fixed_case(direction) for direction in range(4)}
    rows = []
    for index, row in enumerate(mutations):
        case = cases[row["direction"]]
        result = formal_gate(case, changes_for(case, row))
        rows.append({"source_row_index": index, "direction": row["direction"],
                     "kind": row["kind"], "position": row.get("position"),
                     "positions": row.get("positions"), "mode": row.get("mode"),
                     "support": row.get("support"),
                     "historical_knowledge_only_gate": row["result"]["offline_dependency_gate"],
                     "formal_dependency_gate": result})
    for name, expected in raw_hashes.items():
        if hashlib.sha256((raw / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"historical evidence changed: {name}")
    probe.write(output / "formal-dependency-gates.json", {
        "schema": "d123-0-formal-gate-repair-v1", "checker":
        "scripts.d122_fabric_timing.inspect_fabric_dependency_knowledge",
        "source_mutations_sha256": raw_hashes["mutations.json"], "rows": rows,
        "historical_field_semantics": "run-1 offline_dependency_gate is knowledge-only; known never proved absence of mismatch and is not formal admission",
        "scope": "baseline and all existing single/support/adjacent/material mutations; no new physics or Fabric experiment"})
    probe.write(output / "summary.json", {
        "status": "PASS_REVIEW_FIXES_ONLY", "rows": len(rows),
        "kinds": dict(Counter(r["kind"] for r in rows)),
        "formal_statuses": dict(Counter(str(r["formal_dependency_gate"]["status"]) for r in rows)),
        "historical_known_but_formally_rejected": sum(
            r["historical_knowledge_only_gate"] == "known" and not r["formal_dependency_gate"]["accepted"]
            for r in rows), "historical_raw_hashes_unchanged": raw_hashes,
        "d120": historical, "production_changed": False, "fabric_run": False,
        "deletion_proof": "NOT_SIGNED",
        "d123_1_recommendation": "Existing evidence supports retaining all 12 cells and choosing actor-only observation calibration; no claim that observation is infeasible."})
    print(json.dumps({"rows": len(rows), "statuses": dict(Counter(
        str(r["formal_dependency_gate"]["status"]) for r in rows)), "d120": historical}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--d120-source", required=True, type=Path)
    args = parser.parse_args()
    repair(args.raw, args.output, args.d120_source)


if __name__ == "__main__":
    main()
