"""Collect S0-R behavior without installing any path/performance probes."""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
import gzip
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.navigation_structure_baseline import NORMALIZER_VERSION, product_index, structure_signature
from scripts.navigation_coordination_metrics import source_fingerprint
from scripts.navigation_migration_evidence import _encode

SETS = ("product", "coordination", "faults", "follow", "world_changes")
OWNED_VERIFICATION_SCHEMA = "mc2p.s0r-owned-verification.v2"


def signature_schema(group):
    return OWNED_VERIFICATION_SCHEMA if group in ("coordination", "faults") else None

FIELDS = ("movement_tick", "tick", "position", "velocity", "on_ground", "pose",
          "applied_movement", "input_window", "source_bound", "source_owned",
          "controller_ids", "controller_phase", "action_kind", "actual_applied")


def case_jobs(group):
    if group == "product":
        from scripts.navigation_coordination_metrics import load_manifest
        manifest, _ = load_manifest(ROOT / "tests/sim/manifests/navigation-product-r28-v7.json")
        for family in manifest["groups"]:
            for seed in range(manifest["seed_start"], manifest["seed_start"] + manifest["seed_count"]):
                yield f"{family['id']}-{seed:06d}", family["id"], (manifest, family, seed)
    elif group == "coordination":
        from scripts.navigation_migration_evidence import jobs
        yield from jobs()
    elif group == "faults":
        from tests.sim.migration_faults import FAULT_CASES
        for name in FAULT_CASES:
            yield "faults/" + name, "faults", name
        for follow in (False, True):
            for phase in ("walking", "braking", "airborne"):
                for retryable in (False, True):
                    data = {"follow": follow, "phase": phase, "retryable": retryable}
                    yield f"io/{'follow' if follow else 'point'}/{phase}/{retryable}", "io", data
    elif group == "follow":
        from tests.sim.known_world_following import SCENARIOS
        for scenario in SCENARIOS:
            yield scenario.name, "follow", asdict(scenario)
    elif group == "world_changes":
        for follow in (False, True):
            for tick in (4, 10, 18):
                for material in ("grass", "obstacle", "slab"):
                    data = {"follow": follow, "edit_tick": tick, "material": material}
                    yield f"world/{'follow' if follow else 'point'}/{tick}/{material}", "world", data
        yield "world/r1-dependency-recovery", "faults", "active_route_dependency_retry"
    else:
        raise ValueError(group)


def representative_jobs(group):
    selected, seen = [], set()
    for job in case_jobs(group):
        # Product and coordination sample every declared family. Small sets
        # run in full so all I/O/body phases and world premises are exercised.
        family = job[1]
        if group == "coordination" and family == "async":
            family += "/" + job[2][0]["name"]
        if group not in ("product", "coordination") or family not in seen:
            selected.append(job)
            seen.add(family)
    return selected


def stable_identifiers(value):
    identities = {}
    fields = {"incumbent_route_id", "route_id", "owner", "successor"}
    def visit(item, key=None):
        if key in fields and isinstance(item, str):
            return identities.setdefault(item, len(identities))
        if isinstance(item, dict):
            return {k: visit(v, k) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [visit(v) for v in item]
        return item
    return visit(value)


def execute_case(job, *, sign=True, signature_schema_version=OWNED_VERIFICATION_SCHEMA):
    identifier, kind, data = job
    if kind in ("interrupt", "events", "async", "faults"):
        from scripts.navigation_migration_evidence import _execute
        raw = _execute(kind, data)
        payload = {"input": data, "outcome": raw.get("task_outcome"),
                   "reason": raw.get("reason"), "passed": raw["passed"],
                   "verification_complete": raw.get("verification", {}).get("complete"),
                   "events": raw.get("events"),
                   "violations": raw.get("violations", raw.get("body_violations")),
                   "trace": [{key: row[key] for key in FIELDS if key in row} for row in raw["trace"]]}
        if signature_schema_version == OWNED_VERIFICATION_SCHEMA:
            verification = raw.get("verification")
            if not isinstance(verification, dict) or not {"status", "coverage", "gaps"}.issubset(verification):
                raise ValueError("owned verification evidence is missing its typed assessment")
            payload.pop("verification_complete")
            payload["verification"] = verification
    elif kind == "io":
        from tests.sim.runtime_faults import run_io_case
        raw = payload = run_io_case(**data)
    elif kind == "world":
        from tests.sim.s0r_world_changes import run_world_change
        raw = payload = run_world_change(**data)
    elif kind == "follow":
        from tests.sim.known_world_following import FollowScenario, run_scenario_with_trace
        from tests.test_player_runtime import _RecordingTrace
        data = dict(data)
        data["missing_observation_ticks"] = tuple(data["missing_observation_ticks"])
        data["initial_unknown_cells"] = tuple(data["initial_unknown_cells"])
        trace = []
        raw = run_scenario_with_trace(FollowScenario(**data), _RecordingTrace(), trajectory_sink=trace.append)
        payload = {**raw, "trace": trace}
        raw = payload
    else:
        raise ValueError(kind)
    # Transport conversion matches product/migration archives: enum scalars
    # become strings and tuple fields become JSON lists before signing.
    payload = stable_identifiers(json.loads(json.dumps(payload, default=_encode)))
    result = {"id": identifier, "kind": kind, "input": data, "passed": raw["passed"], "raw": raw}
    if sign:
        signed = {"id": identifier, **payload}
        if signature_schema_version is not None:
            signed = {"signature_schema_version": signature_schema_version, **signed}
        result["signature"] = structure_signature(signed)
    return result


def _store_case(job):
    case, root, group = job
    result = execute_case(case, signature_schema_version=signature_schema(group))
    with gzip.open(Path(root) / (case[0].replace("/", "__") + ".json.gz"), "wt", encoding="utf-8") as stream:
        json.dump(result, stream, default=_encode)
    return {k: result[k] for k in ("id", "passed", "signature")}


def collect(group, output, workers=4, representative=False):
    if output.exists():
        raise FileExistsError(output)
    started = time.perf_counter()
    tasks = representative_jobs(group) if representative else list(case_jobs(group))
    if group == "product":
        from scripts.navigation_coordination_metrics import baseline
        baseline(ROOT / "tests/sim/manifests/navigation-product-r28-v7.json", output,
                 workers=workers, seed_count=1 if representative else None)
        index = product_index(output)
        records = [json.loads(line) for line in (output / "runs.jsonl").read_text().splitlines()]
        results = {"success": sum(row["metrics"]["success"] for row in records),
                   "exceptions": sum(row["exception"] is not None for row in records),
                   "safety_violation_tasks": sum(bool(row["metrics"]["safety_events"]) for row in records)}
        assert results["exceptions"] == results["safety_violation_tasks"] == 0
    else:
        output.mkdir(parents=True)
        rows = []
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for row in pool.map(_store_case, [(job, str(output), group) for job in tasks]):
                rows.append(row)
                if not row["passed"]:
                    raise AssertionError(f"formal scenario failed: {row['id']}")
                if len(rows) % 100 == 0:
                    print(f"{group}: {len(rows)}/{len(tasks)}", flush=True)
        index = {"kind": group, "cases": [{k: row[k] for k in ("id", "signature")} for row in rows]}
        results = {"passed": len(rows), "failed": 0}
    index = {"schema_version": "mc2p.navigation-structure-baseline.v1",
             "normalizer_version": NORMALIZER_VERSION, **index}
    if signature_schema(group) is not None:
        index["signature_schema_version"] = signature_schema(group)
    (output / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    summary = {"set": group, "cases": len(index["cases"]), "results": results,
               "elapsed_seconds": time.perf_counter() - started,
               "representative_only": representative, "path_probes_installed": False,
               "signature_schema_version": signature_schema(group),
               "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
               "source_worktree_clean": not subprocess.check_output(["git", "status", "--porcelain"], text=True).strip(),
               "platform": platform.platform(), "python": sys.version,
               "production": source_fingerprint(["mc2p/**/*.py", "config/motion-navigation/*.json"]),
               "harness": source_fingerprint(["tests/sim/**/*.py", "scripts/navigation_s0r*.py"])}
    (output / "s0r-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", choices=SETS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--representative", action="store_true")
    args = parser.parse_args(argv)
    result = collect(args.set, args.output, args.workers, args.representative)
    print(json.dumps({k: v for k, v in result.items() if k not in ("production", "harness")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
