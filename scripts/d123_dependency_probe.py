"""D123-0 read-only dependency experiment. Never starts Runtime or Fabric.

All overlays are detached copies. Production query/solver behavior is unchanged.
The extended-height material vector is a contract-boundary counterexample, not a
claim that a two-block-tall honey block exists in vanilla Minecraft.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, fields, is_dataclass, replace
from enum import Enum
import hashlib
import inspect
import json
from pathlib import Path
import platform
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.motion_nav.motion_solver import _replay_verified_commands, bind_fixed_successor
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.prepared_motion_proof import canonical_motion_digest
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, CellFact, CellKnowledge, UNKNOWN_CELL, WorldView,
)
from scripts import d120_momentum_entry_calibration as d120
from scripts.d122_fabric_timing import _canonical_fabric_dependency_manifest
from tests.motion_nav.test_d121_momentum_handoff import _snapshot_with_change
from tests.motion_nav.test_d121_successor_binding import fixed_case

FROZEN_MANIFEST = "89c5ee269da0b8ac969d69be781a589c8f28449faf0b153fe69a6b2c2a433dd6"


def plain(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    return value


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plain(value), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")) + "\n", encoding="utf-8")


def world_for(snapshot, changes):
    facts = dict(snapshot.facts)
    facts.update(changes)
    return PhysicsWorldView(WorldView.detached(snapshot.session, 0, 0, facts), JAVA_1_21_RULESET)


def fact_for(snapshot, position, mode):
    stamp = dict(snapshot.facts)[position].stamp
    if mode == "unknown":
        return UNKNOWN_CELL
    if mode == "air":
        return CellFact(CellKnowledge.AIR, stamp)
    geometry = {
        "cube": BlockGeometry.full_cube("minecraft:stone"),
        "bottom_slab": BlockGeometry("minecraft:stone", "boxes", (Aabb(0, 0, 0, 1, .5, 1),)),
        "top_slab": BlockGeometry("minecraft:stone", "boxes", (Aabb(0, .5, 0, 1, 1, 1),)),
        "tall_ordinary": BlockGeometry("minecraft:stone", "boxes", (Aabb(0, 0, 0, 1, 2, 1),)),
        "tall_material_boundary": BlockGeometry("minecraft:honey_block", "boxes", (Aabb(0, 0, 0, 1, 2, 1),)),
    }[mode]
    return CellFact(CellKnowledge.BLOCK, stamp, geometry)


@contextmanager
def reads_for(lower):
    """Observe real shapes/cell callers, including rejected fallback attempts."""
    records = defaultdict(lambda: {"count": 0, "paths": set()})
    shapes_original, cell_original = PhysicsWorldView.shapes, PhysicsWorldView.cell

    def record(position, operation):
        if position not in lower:
            return
        frame = inspect.currentframe().f_back.f_back
        path = []
        for _ in range(9):
            if frame is None:
                break
            filename = Path(frame.f_code.co_filename)
            try:
                relative = filename.relative_to(ROOT).as_posix()
            except ValueError:
                relative = filename.name
            if relative.startswith("mc2p/"):
                path.append(f"{relative}:{frame.f_lineno}:{frame.f_code.co_name}")
            frame = frame.f_back
        key = (position, operation, path[0] if path else "probe")
        records[key]["count"] += 1
        records[key]["paths"].add(" <- ".join(path))

    def shapes(view, positions):
        for position in positions:
            record(position, "shape_owner")
        return shapes_original(view, positions)

    def cell(view, position):
        record(position, "direct_cell")
        return cell_original(view, position)

    with patch.object(PhysicsWorldView, "shapes", shapes), patch.object(PhysicsWorldView, "cell", cell):
        yield records


def replay(case, changes):
    request, fixture, proof, spec, snapshot = case
    world = world_for(snapshot, changes)
    variant, failure = _replay_verified_commands(
        proof.entry_state, proof.commands, world, request, start_tick=1)
    changed_snapshot = snapshot
    for position, fact in changes.items():
        changed_snapshot = _snapshot_with_change(changed_snapshot, position, fact)
    binding = bind_fixed_successor(proof, request, spec, changed_snapshot,
                                   damage_remaining_points=0.)
    binding_proof = binding.proof
    # This is the formal D122 offline dependency gate, never Runtime admission.
    deps = set(proof.world_dependencies)
    deps.update(p for tail in proof.fallback_tails for p in tail.dependencies)
    if binding_proof is not None:
        deps.update(p for p, _ in binding_proof.successor_binding.dependency_versions)
    else:
        deps.update(_canonical_fabric_dependency_manifest(fixture.trial.direction_index)["required"])
    gate_unknown = sorted(p for p in deps if world.cell(p).knowledge is CellKnowledge.UNKNOWN)
    result = {
        "parent_status": "solved" if failure is None else failure.status.value,
        "parent_failure": plain(failure),
        "successor_binding_status": binding.status.value,
        "successor_failure_reasons": binding.reasons,
        "offline_dependency_gate": "NEEDS_WORLD" if gate_unknown else "known",
        "gate_unknown": gate_unknown,
        "dependency_signature": digest(sorted(deps)),
        "dependencies": sorted(deps),
    }
    if variant:
        unsafe = sorted(set(range(len(proof.commands))) - set(variant.release_safe_command_indices))
        result.update({
            "main_signature": digest((variant.trajectory, variant.step_events, variant.tick_inputs)),
            "exit_signature": digest(variant.exit_state),
            "risk_boundary_indices": unsafe,
            "risk_signature": digest((unsafe, [(t.boundary_index, t.recovery_boundary_index,
                                               t.candidate.terminal_mode) for t in variant.fallback_tails])),
            "damage": max((t.expected_damage_points for t in variant.fallback_tails), default=0.),
            "fallback_signature": digest(variant.fallback_tails),
            "fallback_behavior_signature": digest([
                (t.boundary_index, t.recovery_boundary_index, t.candidate, t.inputs,
                 t.trajectory, t.expected_damage_points, t.release_witness_input,
                 t.release_witness_state, t.main_suffix_digest) for t in variant.fallback_tails]),
            "parent_dependency_signature": digest(variant.world_dependencies),
        })
    return result


def d120_comparison(source, output):
    manifest = d120.build_manifest()
    if manifest["sha256"] != FROZEN_MANIFEST:
        raise ValueError("D120 frozen manifest identity changed")
    published = json.loads((ROOT / "evidence/motion_navigation/d120-momentum-entry-v1/offline/source-index.json").read_text())
    rows = []
    for direction in range(4):
        path = source / f"coverage-{direction}.json"
        if not path.exists():
            raise FileNotFoundError(f"exact D120 raw coverage unavailable: {path}")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != published["files"][path.name]:
            raise ValueError(f"D120 historical raw hash mismatch: {path}")
        old = json.loads(path.read_text())
        d120.validate_coverage(manifest, old)
        new = d120.scan_direction(manifest, direction)
        d120.validate_coverage(manifest, new)
        comparisons = [{"point_index": a["point_index"], "old": digest(a), "new": digest(b),
                        "equal": a == b} for a, b in zip(old["samples"], new["samples"])]
        row = {"direction": direction, "raw_source": str(path), "raw_sha256": actual_hash,
               "old_signature": digest(old), "new_signature": digest(new), "equal": old == new,
               "sample_count": len(comparisons), "sample_signatures": comparisons,
               "old_success_regressions": sum(not b["suffixes"][k]["passed"] for b in new["samples"] for k in d120.SUFFIX_LIMITS)}
        write(output / f"d120-direction-{direction}.json", row)
        rows.append(row)
    return {"manifest_sha256": manifest["sha256"], "entrances": 972, "suffixes": 2916,
            "all_exactly_equal": all(r["equal"] for r in rows),
            "old_success_regressions": sum(r["old_success_regressions"] for r in rows)}


def run(output, d120_source):
    if output.exists():
        raise ValueError("append-only evidence: output must not exist")
    output.mkdir(parents=True)
    write(output / "environment.json", {"platform": platform.platform(), "python": sys.version,
          "executable": sys.executable, "cwd": str(ROOT), "scope": "detached_read_only_no_runtime_no_fabric"})
    # Fail before reduced experiments if the exact prerequisite cannot be recovered.
    try:
        historical = d120_comparison(d120_source, output)
    except (FileNotFoundError, ValueError) as error:
        write(output / "summary.json", {"status": "BLOCKED", "reason": str(error)})
        return 2
    stop = json.loads((ROOT / "evidence/motion_navigation/d122-client-timing-v1/d122-4/fabric-smoke-v8/observation-stop.json").read_text())
    all_rows, all_reads, freezes, shape_rows = [], [], [], []
    for direction in range(4):
        case = fixed_case(direction)
        request, fixture, proof, spec, snapshot = case
        canonical = _canonical_fabric_dependency_manifest(direction)
        lower = {p for p in canonical["required"] if p[1] == 62}
        assert len(canonical["required"]) == 67 and len(lower) == 12
        if direction == 0:
            assert sorted((x + 16, y - 64, z + 33) for x, y, z in lower) == sorted(tuple(p) for p in stop["unknown_positions"])
        bound = bind_fixed_successor(proof, request, spec, snapshot, damage_remaining_points=0.)
        freeze = {"direction": direction, "request": request, "parent": proof,
                  "successor_spec": spec, "bound_parent": bound.proof,
                  "fallback": proof.fallback_tails, "snapshot_digest": snapshot.identity.digest,
                  "dependencies": canonical["required"], "categories": canonical["categories"],
                  "lower": sorted(lower), "proof_digest": canonical_motion_digest(bound.proof)}
        write(output / f"frozen-direction-{direction}.json", freeze)
        freezes.append({"direction": direction, "proof_digest": freeze["proof_digest"],
                        "dependency_count": 67, "lower_count": 12})
        with reads_for(lower) as records:
            baseline = replay(case, {})
        for (position, operation, caller), item in sorted(records.items()):
            all_reads.append({"direction": direction, "position": position, "operation": operation,
                              "caller": caller, "count": item["count"], "paths": sorted(item["paths"])})
        all_rows.append({"direction": direction, "kind": "baseline", "result": baseline})
        for position in sorted(lower):
            above = (position[0], position[1] + 1, position[2])
            for mode in ("air", "cube", "unknown"):
                result = replay(case, {position: fact_for(snapshot, position, mode)})
                all_rows.append({"direction": direction, "kind": "single", "position": position,
                                 "mode": mode, "above": plain(dict(snapshot.facts)[above]), "result": result})
            for support in ("air", "bottom_slab"):
                result = replay(case, {position: UNKNOWN_CELL, above: fact_for(snapshot, above, support)})
                all_rows.append({"direction": direction, "kind": "support_mutation", "position": position,
                                 "support": support, "result": result})
            # Controlled one-step query places the body inside the previously protected volume.
            for support, feet in (("air", 64.), ("bottom_slab", 63.4), ("air", 62.9)):
                changes = {position: UNKNOWN_CELL, above: fact_for(snapshot, above, support)}
                state = replace(proof.entry_state,
                    position=(position[0] + .5, feet, position[2] + .5),
                    velocity_blocks_per_tick=(0., -.0784000015258789, 0.), on_ground=False)
                calculated = step(state, TickInput(0., 0., False, False, False, state.yaw_radians),
                                  world_for(snapshot, changes), JAVA_1_21_RULESET)
                shape_rows.append({"direction": direction, "position": position, "support": support,
                                   "feet_y": feet, "status": calculated.status.value,
                                   "dependencies": calculated.dependencies, "missing": calculated.missing_cells})
        pairs = [(a, b) for a in sorted(lower) for b in sorted(lower) if a < b
                 and sum(abs(x-y) for x, y in zip(a, b)) == 1]
        for pair in pairs:
            for mode in ("cube", "unknown"):
                result = replay(case, {p: fact_for(snapshot, p, mode) for p in pair})
                all_rows.append({"direction": direction, "kind": "adjacent_pair", "positions": pair,
                                 "mode": mode, "result": result})
        # Include the exact supported maximum-extent shape, with both ordinary and
        # unmodeled material, to prevent collision equivalence hiding material reads.
        protected = [p for p in sorted(lower) if dict(snapshot.facts)[(p[0], p[1]+1, p[2])].knowledge is CellKnowledge.BLOCK]
        for position in protected:
            for mode in ("tall_ordinary", "tall_material_boundary"):
                all_rows.append({"direction": direction, "kind": "material_contract_boundary",
                    "position": position, "mode": mode,
                    "result": replay(case, {position: fact_for(snapshot, position, mode)})})
        print(f"direction {direction}: frozen 67/12, mutations recorded", flush=True)
    write(output / "mutations.json", all_rows)
    write(output / "read-paths.json", all_reads)
    write(output / "sweep-boundary-mutations.json", shape_rows)
    write(output / "freeze-index.json", freezes)
    singles = [r for r in all_rows if r["kind"] == "single"]
    signature_fields = ("main_signature", "exit_signature", "risk_signature", "damage", "fallback_behavior_signature")
    classifications = []
    for direction in range(4):
        baseline = next(r["result"] for r in all_rows if r["kind"] == "baseline" and r["direction"] == direction)
        for position in sorted({tuple(r["position"]) for r in singles if r["direction"] == direction}):
            rows = [r for r in singles if r["direction"] == direction and tuple(r["position"]) == position]
            unknown = next(r["result"] for r in rows if r["mode"] == "unknown")
            same = all(all(r["result"].get(k) == baseline.get(k) for k in signature_fields) for r in rows)
            classifications.append({"direction": direction, "position": position,
                "air_cube_unknown_main_risk_damage_fallback_equal": same,
                "collision_read": "barrier_candidate_only" if same else "required_observe",
                "material_read": "insufficient_evidence_at_maximum_shape_extent" if same else "required_observe",
                "dependency_collection": "retained_by_current_contract_must_observe",
                "unknown_parent": unknown["parent_status"],
                "unknown_successor": unknown["successor_binding_status"],
                "unknown_gate": unknown["offline_dependency_gate"]})
    write(output / "classifications.json", classifications)
    summary = {"status": "STOPPED_INSUFFICIENT_EVIDENCE", "d120": historical,
               "four_directions": 4, "single_rows": len(singles), "all_mutation_rows": len(all_rows)-4,
               "shape_sweep_mutations": len(shape_rows), "read_reasons": len(all_reads),
               "per_direction_lower_cells": 12, "per_direction_dependencies": 67,
               "material_boundary_counterexamples": sum(r["kind"] == "material_contract_boundary"
                   and r["mode"] == "tall_material_boundary" and r["result"]["parent_status"] == "unsupported" for r in all_rows),
               "production_changes": False, "fabric_run": False,
               "recommendation": "Do not authorize D123-1 blanket pruning. Keep necessary gap reads; resolve maximum-extent material/contact assumptions before removing protected-cell material reads, or use existing actor observation for all retained dependencies."}
    write(output / "summary.json", summary)
    hashes = {str(p.relative_to(output)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in output.rglob("*") if p.is_file()}
    write(output / "manifest.json", {"schema": "d123-0-read-only-v1", "files": hashes,
          "inputs": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in [ROOT / "scripts/d123_dependency_probe.py", ROOT / "scripts/d120_momentum_entry_calibration.py",
                        ROOT / "scripts/d122_fabric_timing.py", ROOT / "tests/motion_nav/test_d121_successor_binding.py",
                        ROOT / "tests/motion_nav/test_d121_momentum_handoff.py", ROOT / "mc2p/motion_nav/physics_1_21.py",
                        ROOT / "mc2p/motion_nav/physics_adapter.py", ROOT / "mc2p/motion_nav/geometry.py",
                        ROOT / "mc2p/motion_nav/motion_solver.py", ROOT / "mc2p/motion_nav/world_model.py"]}})
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--d120-source", type=Path, required=True)
    args = parser.parse_args()
    return run(args.output, args.d120_source)


if __name__ == "__main__":
    raise SystemExit(main())
