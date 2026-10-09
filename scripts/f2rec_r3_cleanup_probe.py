"""Record R3 deletion safety, active component equivalence, and worker debt."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.f2_ground_route_evidence import MANIFEST, ROOT, run_route
from scripts.f2rec_r0_baseline import worker_lifecycle_probe


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    previous = {
        row["id"]: row for row in map(json.loads, (ROOT / (
            "evidence/motion_navigation/F2REC-recovery-v1/"
            "r2-revalidation/f2-528-index.jsonl")).read_text("utf-8").splitlines())
    }
    active, retired, differences = [], [], []
    for item in json.loads(MANIFEST.read_text("utf-8"))["tasks"]:
        if item["kind"] != "component":
            continue
        row = run_route(item)
        index = {k: row[k] for k in (
            "id", "input_sha256", "outcome", "reason", "final_position", "behavior_sha256")}
        if item["family"] == "declared_edge":
            retired.append({**index, "gate_violations": row["gate_violations"],
                            "sneak_frames": row["sneak_frames"],
                            "lost_ground_frames": row["lost_ground_frames"]})
        else:
            active.append(index)
            if index != previous[index["id"]]:
                differences.append({"id": index["id"], "before": previous[index["id"]],
                                    "after": index})
    write(args.output / "component-comparison.json", {
        "active_cases": len(active), "differences": differences,
        "production_files_sha256": {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                                    for name in ("mc2p/motion_nav/fixed_route.py",
                                                 "mc2p/motion_nav/ground_route_execution.py",
                                                 "mc2p/motion_nav/safe_ground_control.py")},
        "active_index": active, "retired_component_cases": retired,
        "retired_reason": "component-only declared-edge experiment; no production producer",
    })
    write(args.output / "worker-lifecycle.json", worker_lifecycle_probe())

    # A missing safety check must actually be detected, not merely named.
    from tests.motion_nav.test_f2rec_r3_cleanup import R3CleanupTests
    from mc2p.motion_nav import safe_ground_control
    namespace = dict(vars(safe_ground_control))
    source = (ROOT / "mc2p/motion_nav/safe_ground_control.py").read_text("utf-8")
    original = "support.support_fraction < minimum_support"
    assert source.count(original) == 2  # ordinary replay and shared recovery replay
    mutated = source.replace(original, "False", 1)
    exec(compile(mutated, "r3-support-threshold-mutant", "exec"), namespace)
    with patch("tests.motion_nav.test_f2rec_r3_cleanup.verified_ground_route_candidate",
               namespace["verified_ground_route_candidate"]):
        result = unittest.TestResult()
        R3CleanupTests("test_ordinary_replay_retains_actual_support_threshold").run(result)
    write(args.output / "mutation-check.json", {
        "mutant": "remove ordinary replay support-fraction threshold",
        "detected": bool(result.failures), "failures": [detail for _, detail in result.failures],
        "errors": [detail for _, detail in result.errors],
    })
    assert not differences, differences
    assert result.failures and not result.errors
    assert len(active) == 104 and len(retired) == 8
    assert all(not row["gate_violations"] and not row["sneak_frames"]
               and not row["lost_ground_frames"] for row in retired)
    print(json.dumps({"active_component_equivalence": len(active),
                      "retired_component_cases": len(retired), "mutation_detected": True}))


if __name__ == "__main__":
    main()
