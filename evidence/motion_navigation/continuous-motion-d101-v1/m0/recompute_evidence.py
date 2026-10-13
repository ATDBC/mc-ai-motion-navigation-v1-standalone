"""Independent raw-record audit. Replays facts; never collects new timings."""
from collections import Counter
from dataclasses import replace
import gzip
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, PhysicsEffect, PhysicsState, TickInput
from mc2p.motion_nav.world_model import WorldSessionId
from experiments.motion_navigation.trajectory_proto.commitment import scan_commitment, ScanStatus
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.physics import _canonical, trajectory_digest
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.scenarios import representative_fixture


def read(name):
    with gzip.open(Path(__file__).parent / name, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def state(value):
    value = dict(value)
    value["session"] = WorldSessionId(**value["session"])
    value["position"] = tuple(value["position"])
    value["velocity_blocks_per_tick"] = tuple(value["velocity_blocks_per_tick"])
    value["status_effects"] = tuple(PhysicsEffect(**effect) for effect in value["status_effects"])
    return PhysicsState(**value)


def stats(rows):
    times = sorted(row["duration_ns"] / 1e6 for row in rows)
    return {"samples": len(times), "p50_ms": times[math.ceil(len(times) * .5) - 1],
            "p95_ms": times[math.ceil(len(times) * .95) - 1],
            "p99_ms": times[math.ceil(len(times) * .99) - 1], "max_ms": times[-1]}


def main():
    output = Path(__file__).parent
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    for name, digest in manifest["artifacts"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest, name
    for name, digest in manifest["source_hashes"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    runs, rows = read("runs.jsonl.gz"), read("decisions.jsonl.gz")
    fixture = representative_fixture("jump_gap_1")
    frozen = read("fixture.jsonl.gz")[0]
    assert _canonical(fixture.request) == frozen["request"]
    positions = ((x, y, z) for x in range(-3, 4) for y in range(-50, 8) for z in range(-3, 13))
    assert _canonical(tuple((pos, fixture.world.cell(pos)) for pos in positions)) == frozen["world_facts"]
    inputs = tuple(TickInput(**command) for command in frozen["inputs"])
    assert trajectory_digest(inputs) == summary["input_hash"]
    by_generation = {}
    for row in rows:
        by_generation.setdefault(row["generation_id"], []).append(row)
    applied = 0
    used_sequences = {}
    recomputed_proof_steps = 0
    for run in runs:
        generation = run["generation"]
        gid = generation["generation_id"]
        sequence = generation["first_control_sequence"]
        serial = sequence // 128
        preludes = tuple(tuple(replace(fact, control_sequence=sequence - 1,
                                      evidence_id=f"d101-wait-{serial}") for fact in prelude)
                         for prelude in fixture.request.branch_preludes)
        request = replace(fixture.request, request_id=generation["request_id"],
                          input_ledger_id=f"d101-ledger-{serial}", branch_preludes=preludes,
                          first_candidate_control_sequence=sequence,
                          budget=SearchBudget(10000, 100000, 40, 2))
        evidence = tuple(tuple(replace(row, irrevocable_inputs=(), applications=()) for row in branch)
                         for branch in _boundary_evidence(request, inputs))
        scan = scan_commitment(request, inputs, fixture.world, evidence)
        assert scan.status is ScanStatus.VERIFIED_CANDIDATE
        assert scan.proof.trajectory_hash == run["proof_hash"]
        assert generation["dependency_identity"][1] == ["request", trajectory_digest(request)]
        assert generation["dependency_identity"][2] == ["proof", scan.proof.trajectory_hash]
        recomputed_proof_steps += scan.counts.physics_steps
        previous = _canonical(request.anchor_state)
        seen_first = False
        group = by_generation[gid]
        for index, row in enumerate(group):
            assert row["decision_index"] == index
            assert row["state_before"] == previous
            assert row["owner_id"] == generation["owner_id"]
            assert row["evidence_hash"] == run["proof_hash"]
            if row["actual_effect_tick"] is not None:
                calculated = step(state(previous), TickInput(**row["applied_command"]), fixture.world, JAVA_1_21_RULESET)
                assert calculated.status is CalculationStatus.OK
                assert _canonical(calculated.next_state) == row["observed_state"]
                assert row["actual_effect_tick"] == row["state_before"]["movement_tick_id"] + 1
                control = row["applied_control_sequence"]
                assert control not in used_sequences
                used_sequences[control] = gid
                applied += 1
            else:
                assert row["observed_state"] == previous
            if row["first_receipt"] is not None:
                seen_first = True
            if run["injection"] == "none" and seen_first:
                assert row["surviving_branches"] == [run["start_branch"]]
            for name, envelope, check in zip(row["key_nodes"], row["envelopes"], row["key_checks"]):
                branch = scan.proof.branches[int(run["start_branch"] == "LATE_ONE")]
                risk = branch.risk_intervals[0]
                boundary = risk.first_committed_boundary if name == "commitment" else risk.recovered_boundary
                reference = _canonical(branch.states[boundary])
                assert envelope["reference_state"] == reference == row["observed_state"]
                assert envelope["generation"] == generation
                assert envelope["start_branch"] == run["start_branch"]
                assert envelope["position_min"] == envelope["position_max"] == reference["position"]
                assert envelope["velocity_min"] == envelope["velocity_max"] == reference["velocity_blocks_per_tick"]
                assert check["kind"] == "COMPARE_ONLY" and check["full_proof_calls"] == 0
            previous = row["observed_state"]
        assert previous == run["final_state"]
        assert sum(row["physics_steps"] for row in group) == run["physics_steps"]
    all_stats = stats(rows)
    for name in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
        assert all_stats[name] == summary[name]
    assert len(rows) == summary["decision_count"]
    assert sum(row["full_proof_calls"] for row in rows) == summary["full_proof_calls"]
    assert sum(row["physics_steps"] for row in rows) == summary["physics_steps"]
    assert recomputed_proof_steps + applied == summary["physics_steps"]
    assert sum(len(row["key_checks"]) for row in rows) == summary["compare_only_count"]
    positive_counts = Counter(run["start_branch"] for run in runs if run["injection"] == "none" and run["terminal_status"] == "COMPLETED")
    assert positive_counts == {"ON_TIME": 100, "LATE_ONE": 100}
    assert not any(run["safety_events"] for run in runs)
    result = {"audit_passed": True, "formal_m0_passed": summary["passed"],
              "record_replay_count": applied, "proof_recalculation_count": len(runs),
              "recalculated_proof_steps": recomputed_proof_steps, "all_decisions": all_stats,
              "initial_proof_decisions": stats([row for row in rows if row["full_proof_calls"]]),
              "other_decisions": stats([row for row in rows if not row["full_proof_calls"]]),
              "manifest_sha256": hashlib.sha256((output / "manifest.json").read_bytes()).hexdigest(),
              "new_timing_samples": 0}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
