"""Scan-vs-lazy comparison used by run_m2.py and test_m2.py."""
from __future__ import annotations

from experiments.motion_navigation.trajectory_proto.commitment import ScanStatus, scan_commitment
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence

import incremental as I


def interval_key(interval):
    return (interval.last_abandon_boundary, interval.first_committed_boundary,
            interval.recovered_boundary)


def run_pair(request, inputs, world, *, mode="strict", faults=frozenset(), evidence=None):
    evidence = _boundary_evidence(request, inputs) if evidence is None else evidence
    scan = scan_commitment(request, inputs, world, evidence)
    lazy = I.lazy_prove(request, inputs, world, evidence, mode=mode, faults=faults)
    return scan, lazy


def _reason(value):
    return None if value is None else str(value.value if hasattr(value, "value") else value)


def compare(scan, lazy) -> dict:
    row = {
        "scan_status": scan.status.value, "scan_reason": _reason(scan.reason),
        "lazy_status": lazy.status.value, "lazy_reason": _reason(lazy.reason),
        "status_match": scan.status is lazy.status,
        "reason_match": _reason(scan.reason) == _reason(lazy.reason),
        "critical": (lazy.status is ScanStatus.VERIFIED_CANDIDATE
                     and scan.status is not ScanStatus.VERIFIED_CANDIDATE),
        "scan_steps": scan.counts.physics_steps, "lazy_steps": lazy.counts.physics_steps,
        "scan_nodes": scan.counts.nodes, "lazy_nodes": lazy.counts.nodes,
    }
    if scan.status is ScanStatus.VERIFIED_CANDIDATE and lazy.status is ScanStatus.VERIFIED_CANDIDATE:
        branches = scan.proof.branches
        scan_keys = [[interval_key(i) for i in b.risk_intervals] for b in branches]
        lazy_keys = [[interval_key(i) for i in b.risk_intervals] for b in lazy.branches]
        row["intervals_scan"] = [[list(k) for k in keys] for keys in scan_keys]
        row["intervals_lazy"] = [[list(k) for k in keys] for keys in lazy_keys]
        row["interval_keys_match"] = scan_keys == lazy_keys
        row["interval_counts_match"] = [len(k) for k in scan_keys] == [len(k) for k in lazy_keys]
        row["interval_full_match"] = (
            [b.risk_intervals for b in branches] == [b.risk_intervals for b in lazy.branches])
        row["tail_status_match"] = (
            len(branches) == len(lazy.branches)
            and all([t.status for t in b.tails] == [lb.tails[i].status for i in sorted(lb.tails)]
                    and [t.inputs for t in b.tails] == [lb.tails[i].inputs for i in sorted(lb.tails)]
                    for b, lb in zip(branches, lazy.branches)))
        row["states_match"] = all(b.states == lb.states for b, lb in zip(branches, lazy.branches))
        row["dependencies_match"] = (
            frozenset(position for position, _ in scan.proof.dependency_facts) == lazy.dependencies)
        row["n_intervals"] = [len(k) for k in scan_keys]
        row["equivalent"] = (row["interval_keys_match"] and row["interval_counts_match"]
                             and row["interval_full_match"] and row["tail_status_match"]
                             and row["states_match"])
    else:
        row["equivalent"] = row["status_match"] and not row["critical"]
    return row
