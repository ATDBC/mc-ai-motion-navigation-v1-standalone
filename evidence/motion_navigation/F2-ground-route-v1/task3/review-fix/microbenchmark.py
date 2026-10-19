"""Serial actual-position authorization and outside-stop performance."""
from pathlib import Path
from dataclasses import asdict
import json
import platform
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))
from scripts.f2_ground_route_evidence import MANIFEST, digest, run_route, timing_summary
from scripts.navigation_coordination_metrics import source_fingerprint
from mc2p.motion_nav.fixed_route import FixedRouteController
from tests.motion_nav.test_f2_ground_route_edge_guard import GroundRouteEdgeGuardTests


def main():
    destination = Path(sys.argv[1])
    if destination.exists():
        raise FileExistsError(destination)
    families = {"offset", "diagonal", "centre_to_offset", "tangent", "corner", "declared_edge"}
    cases = [c for c in json.loads(MANIFEST.read_text("utf-8"))["tasks"] if c["family"] in families]
    task2 = {r["id"]: r for r in map(json.loads, (ROOT / "evidence/motion_navigation/F2-ground-route-v1/task2/runs.jsonl").read_text("utf-8").splitlines())}
    outside_tests = {
        "outside_release": "test_reverse_displacement_releases_from_actual_position_then_replans",
        "outside_retention": "test_outside_retention_is_neutral_proved_and_bounded",
    }
    for c in cases:
        run_route(c)
    for name in outside_tests.values():
        getattr(GroundRouteEdgeGuardTests(name), name)()
    samples, runs = [], []
    outside_traces = {name: [] for name in outside_tests}
    outside_hashes = {name: [] for name in outside_tests}
    original = FixedRouteController.decide
    current_family = None
    def measured(controller, *args, **kwargs):
        decision = original(controller, *args, **kwargs)
        samples.append((decision.control_time_ns / 1.e6, decision.full_candidates,
                        decision.physics_steps, current_family, decision.edge_guard_phase.value))
        if current_family in outside_traces:
            outside_traces[current_family].append({
                "position": args[0].body.position, "movement": asdict(decision.movement),
                "state": decision.state.value, "phase": decision.edge_guard_phase.value,
                "full_candidates": decision.full_candidates, "physics_steps": decision.physics_steps})
        return decision
    with patch.object(FixedRouteController, "decide", measured):
        for repeat in range(3):
            for c in cases:
                current_family = c["family"]
                row = run_route(c)
                assert row["success"] and not row["gate_violations"], row
                if c["family"] != "declared_edge":
                    assert row["behavior_sha256"] == task2[c["id"]]["behavior_sha256"], row
                if c["family"] in {"offset", "diagonal", "centre_to_offset"}:
                    assert row["full_candidates_max"] == 0, row
                runs.append({"repeat": repeat, "id": row["id"], "behavior_sha256": row["behavior_sha256"]})
            for current_family, name in outside_tests.items():
                outside_traces[current_family] = []
                getattr(GroundRouteEdgeGuardTests(name), name)()
                outside_hashes[current_family].append(digest(outside_traces[current_family]))
    for c in cases:
        assert len({r["behavior_sha256"] for r in runs if r["id"] == c["id"]}) == 1
    assert all(len(set(values)) == 1 for values in outside_hashes.values())
    subsets = {
        "all": samples,
        "fast": [r for r in samples if r[1] == 0],
        "slow": [r for r in samples if r[1] > 0],
        "declared_edge": [r for r in samples if r[3] == "declared_edge"],
        "outside_release": [r for r in samples if r[3] == "outside_release" and r[4].startswith("outside_")],
        "outside_retention": [r for r in samples if r[3] == "outside_retention" and r[4].startswith("outside_")],
    }
    report = {"schema": "mc2p.f2-task3-review-fix-serial-performance.v1", "workers": 1,
              "warmup_cases": len(cases)+len(outside_tests),
              "measured_cases": len(runs)+3*len(outside_tests), "frames": len(samples),
              "platform": platform.platform(), "python": sys.version,
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "source_worktree_clean": not subprocess.check_output(["git", "status", "--porcelain"], text=True).strip(),
              "production": source_fingerprint(["mc2p/**/*.py", "config/motion-navigation/*.json"]),
              "control_ms": {name: timing_summary([r[0] for r in rows]) for name, rows in subsets.items()},
              "frame_counts": {name: len(rows) for name, rows in subsets.items()},
              "full_candidates_max": max(r[1] for r in samples),
              "physics_steps_max": max(r[2] for r in samples), "physics_steps_total": sum(r[2] for r in samples),
              "task2_behavior_unchanged": True, "repeat_behavior_identical": True,
              "outside_behavior_hashes": outside_hashes,
              "runs": runs, "evidence_boundary": "TEST_ORACLE component and public-controller loops; no Fabric or deadline evidence."}
    report["passed"] = (all(row["p95"] <= 8 for row in report["control_ms"].values())
                        and report["full_candidates_max"] <= 3)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"production", "runs", "python"}}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
