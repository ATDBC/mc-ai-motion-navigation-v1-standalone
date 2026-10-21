"""Append final per-read D123-0 classification to unchanged raw experiments."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
from scripts.d123_dependency_probe import digest, write


def classify(raw):
    mutations = json.loads((raw / "mutations.json").read_text(encoding="utf-8"))
    reads = json.loads((raw / "read-paths.json").read_text(encoding="utf-8"))
    cells, reasons = [], []
    for direction in range(4):
        rows = [r for r in mutations if r["direction"] == direction]
        singles = [r for r in rows if r["kind"] == "single"]
        for position in sorted({tuple(r["position"]) for r in singles}):
            point = [r for r in singles if tuple(r["position"]) == position]
            unknown_row = next(r for r in point if r["mode"] == "unknown")
            unknown = unknown_row["result"]
            both_solved = unknown["parent_status"] == unknown["successor_binding_status"] == "solved"
            covered = unknown_row["above"]["knowledge"] == "block"
            material = next((r["result"] for r in rows if r["kind"] == "material_contract_boundary"
                            and tuple(r["position"]) == position and r["mode"] == "tall_material_boundary"), None)
            material_counterexample = material is not None and (
                material["parent_status"] != "solved" or material["successor_binding_status"] != "solved")
            cell = {"direction": direction, "position": position,
                "upper_known_collision": covered,
                "unknown_parent_status": unknown["parent_status"],
                "unknown_successor_status": unknown["successor_binding_status"],
                "whole_chain_unknown_solved": both_solved,
                "collision": "insufficient_evidence_for_removal" if covered and both_solved else "required_observe",
                "material": "required_under_current_geometry_contract" if material_counterexample else "retained_not_proved_irrelevant",
                "dependency": "retained_observe", "removal_authorized": False,
                "material_counterexample": material_counterexample,
                "single_signatures": {r["mode"]: digest(r["result"]) for r in point}}
            cells.append(cell)
            for read in reads:
                if read["direction"] != direction or tuple(read["position"]) != position:
                    continue
                if read["operation"] == "shape_owner":
                    classification, reason = cell["collision"], "collision owner below sweep; inherited into step dependencies"
                elif "_contacts_unmodeled_material" in read["caller"]:
                    classification, reason = cell["material"], "independent material/contact check over collision dependencies"
                else:
                    classification, reason = cell["dependency"], "D122 offline dependency knowledge gate; not Runtime admission"
                reasons.append({**read, "classification": classification, "reason": reason,
                                "removal_authorized": False})
    return {"status": "STOPPED_INSUFFICIENT_EVIDENCE", "cells": cells, "read_reasons": reasons,
            "whole_chain_required_gap_cells": sum(not c["whole_chain_unknown_solved"] for c in cells),
            "protected_candidates_without_removal_permission": sum(c["upper_known_collision"] for c in cells),
            "material_counterexamples_in_parent_or_successor": sum(c["material_counterexample"] for c in cells),
            "removed_dependencies": 0,
            "raw_classification_limit": "run-1 classifications.json compares parent behavior only; final classification also requires successor and upper geometry. Use this final file for conclusions.",
            "limits": ["No production pruning candidate was implemented or authorized.",
                "The height-2 honey vector is allowed by BlockGeometry, not a claim about vanilla honey geometry.",
                "AIR/cube/UNKNOWN equivalence alone does not prove a read irrelevant for every accepted collision/material fact.",
                "D120 comparison replays the unchanged production baseline; it does not validate a removed-dependency algorithm.",
                "The D122 v8 record stores dependency coordinates but no historical serialized proof digest; current deterministic fixed_case proofs are frozen with constructors, current hashes and the 67/12 identity check."]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("append-only analysis output must not exist")
    write(args.output, classify(args.raw))
    print(args.output)


if __name__ == "__main__":
    main()
