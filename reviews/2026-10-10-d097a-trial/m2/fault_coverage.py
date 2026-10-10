"""Supplementary to G9: how many harness candidates (not unit tests) expose each injected fault?

For every fault, run lazy_prove(faults={fault}) over Sets A, C and a deterministic subset of B
(every 3rd input mutant, all fixture / request-level / drop / double-gap candidates; no fuzz) and count
candidates whose status, reason or risk intervals differ from the full scanner.
Output: fault_coverage.json
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence

import candidates as K
import equiv
import incremental as I

HERE = Path(__file__).resolve().parent


def main():
    cands = K.set_a() + K.set_c()
    b = K.set_b(fuzz_per_fixture=0)
    mutant_index = 0
    for c in b:
        if c.id.startswith("mutant/"):
            mutant_index += 1
            if mutant_index % 3:
                continue
        cands.append(c)
    print(len(cands), "candidates", flush=True)
    counts = {fault: collections.Counter() for fault in sorted(I.FAULTS)}
    examples = {fault: [] for fault in sorted(I.FAULTS)}
    for index, cand in enumerate(cands):
        evidence = cand.evidence if cand.evidence is not None else _boundary_evidence(cand.request, cand.inputs)
        scan = scan_commitment(cand.request, cand.inputs, cand.world, evidence)
        for fault in sorted(I.FAULTS - {"accept_stale", "swap_locked_command"}):
            lazy = I.lazy_prove(cand.request, cand.inputs, cand.world, evidence, faults=frozenset({fault}))
            row = equiv.compare(scan, lazy)
            counts[fault]["candidates"] += 1
            if not row["status_match"]:
                counts[fault]["status_differs"] += 1
            if row["critical"]:
                counts[fault]["lazy_verified_scan_not"] += 1
            if row["status_match"] and not row["reason_match"]:
                counts[fault]["reason_differs_only"] += 1
            if scan.status is ScanStatus.VERIFIED_CANDIDATE and lazy.status is ScanStatus.VERIFIED_CANDIDATE \
                    and not row["equivalent"]:
                counts[fault]["verified_but_intervals_or_tails_differ"] += 1
            if (not row["status_match"] or not row["equivalent"]) and len(examples[fault]) < 5:
                examples[fault].append({"id": cand.id, "scan": [row["scan_status"], row["scan_reason"]],
                                        "lazy": [row["lazy_status"], row["lazy_reason"]],
                                        "intervals_scan": row.get("intervals_scan"),
                                        "intervals_lazy": row.get("intervals_lazy")})
        if index % 100 == 99:
            print(index + 1, flush=True)
    (HERE / "fault_coverage.json").write_text(json.dumps(
        {"candidates": len(cands), "counts": {k: dict(v) for k, v in counts.items()}, "examples": examples,
         "note": "accept_stale and swap_locked_command act on permits/executor, covered by unit tests only"},
        indent=1), encoding="utf-8")
    print(json.dumps({k: dict(v) for k, v in counts.items()}, indent=1))


if __name__ == "__main__":
    main()
