"""Freeze R28 formal-path product evidence and compare with one external ruler.

Run from the repository root. Reports are append-only; simulation clocks never
stand in for actual Fabric latency. No production controller is modified here.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import gzip
import hashlib
import json
import math
import platform
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.sim.product_cases import product_scenario
from tests.sim.product_metrics import (
    EXTRACTOR_VERSION, compare_metrics, extract_metrics, strict_trace,
)
from tests.sim.runner import run
from tests.sim.motion_delivery import DeterministicMotionWorker, validate_profile

SCHEMA = "mc2p.navigation-product-run.v1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_fingerprint(patterns):
    paths = sorted({p for pattern in patterns for p in ROOT.glob(pattern) if p.is_file()})
    entries = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in paths}
    return {"sha256": digest(json.dumps(entries, sort_keys=True).encode()), "files": entries}


def load_manifest(path):
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") != "mc2p.navigation-product-manifest.v1":
        raise ValueError("unsupported product manifest")
    if type(manifest.get("seed_start")) is not int or type(manifest.get("seed_count")) is not int or manifest["seed_count"] < 1:
        raise ValueError("invalid frozen seed range")
    groups = manifest.get("groups", [])
    if not groups or len({g["id"] for g in groups}) != len(groups):
        raise ValueError("product groups must be present and unique")
    if "motion_delivery_profile" in manifest:
        validate_profile(manifest["motion_delivery_profile"])
    return manifest, digest(raw)


def quantile(values, probability=.95):
    values = sorted(values)
    return None if not values else values[math.ceil(probability * len(values)) - 1]


def trace_signatures(trace):
    normalized = strict_trace(trace)
    inputs = [{key: row[key] for key in ("movement_tick", "applied_movement", "input_window")}
              for row in normalized]
    return {"trajectory_sha256": digest(json.dumps(normalized, sort_keys=True).encode()),
            "inputs_sha256": digest(json.dumps(inputs, sort_keys=True).encode())}


def _run_one(job):
    manifest, group, seed, output = job
    started = time.perf_counter()
    identifier = f"{group['id']}-{seed:06d}"
    result, parameters, error = None, None, None
    raw_trace = []
    delivery = None
    try:
        scenario, parameters = product_scenario(manifest, group, seed)
        if "motion_delivery_profile" in manifest:
            delivery = DeterministicMotionWorker(manifest["motion_delivery_profile"])
            result = run(scenario, trace_sink=raw_trace.append,
                         motion_factory=lambda: delivery, control_step=delivery.control_step)
        else:
            result = run(scenario, trace_sink=raw_trace.append)
        metrics = extract_metrics(result.trace, start_tick=1, start_position=scenario.start,
                                  outcome=result.outcome, violations=result.violations)
        if not result.verification_complete:
            metrics["evidence_complete"] = False
            metrics["coverage_gaps"].append("formal_monitor_coverage_incomplete")
        strict = strict_trace(result.trace) if group["strict"] else None
    except Exception:
        error = traceback.format_exc()
        metrics = extract_metrics(raw_trace, start_tick=1, start_position=(0, 0, 0), outcome="exception")
        metrics["evidence_complete"] = False
        metrics["coverage_gaps"].append("formal_call_exception")
        strict = None
    record = {
        "id": identifier, "group": group["id"], "family": group["family"], "seed": seed,
        "parameters": parameters, "strict": group["strict"], "metrics": metrics,
        "reason": None if result is None else result.reason, "exception": error,
        "verification_complete": result is not None and result.verification_complete,
        "event_dispatches": None if result is None else result.event_dispatches,
        "wall_elapsed_seconds": time.perf_counter() - started,
        "trace_file": f"traces/{identifier}.json.gz",
    }
    if delivery is not None:
        record.update(motion_delivery_profile=delivery.profile,
                      motion_job_count=len(delivery.records), motion_jobs=delivery.records)
        record.update(trace_signatures(raw_trace))
    path = Path(output) / record["trace_file"]
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=3) as stream:
        json.dump({"record": record, "trace": raw_trace,
                   "strict_trace": strict}, stream)
    record["trace_sha256"] = digest(path.read_bytes())
    return record


def coordination_inventory():
    """AST locations are an inventory, not a semantic complexity proof."""
    inventory = {
        "navigation_session": {
            "update_goal": ("1", "NavigationHandoffCoordinator + GoalRequestLedger"),
            "_stage_goal_revision_for_body_release": ("1", "NavigationHandoffCoordinator"),
            "_resume_pending_goal": ("1", "NavigationHandoffCoordinator"),
            "cancel": ("1", "NavigationHandoffCoordinator"), "close": ("1/5", "NavigationHandoffCoordinator"),
            "_finalize_close": ("1/5", "NavigationLifecycle"),
            "handle_internal_contract_failure": ("1/5", "NavigationHandoffCoordinator"),
            "observe": ("1", "按域：Planning/Information/ExecutionSupervisor/Handoff"),
            "propose": ("1/5", "按域：Planning/Information/ExecutionSupervisor/Handoff"),
            "_proposal": ("1/5", "Session只读报告"), "diagnostics": ("1/5", "Session只读报告"),
            "_request_probe_stop": ("1", "ExecutionSupervisor"),
            "_finish_pending_probe_terminal": ("1", "NavigationHandoffCoordinator"),
            "_wait_for_active_terminal": ("1", "NavigationHandoffCoordinator"),
            "_fail_planning_or_preserve_incumbent": ("1/4", "NavigationHandoffCoordinator"),
            "_reissue_request_from_current": ("1/4", "PlanningCoordinator"),
            "_resolve_pending_retry": ("1/4", "RetryLedger + Handoff"),
            "_clear_active_execution": ("1", "ExecutionSupervisor"),
            "_retire_route": ("1", "ExecutionSupervisor"),
            "_activate_planning_route": ("1", "ExecutionSupervisor"),
            "_replace_request": ("3", "GoalRequestLedger + PlanningCoordinator"),
            "_accept_goal_request": ("3", "PlanningCoordinator"),
            "_advance_planning": ("3", "PlanningCoordinator"),
            "_end_probe_waits": ("4", "InformationAcquisitionState"),
            "_end_session_waits": ("4", "RetryLedger"),
            "_check_recovery_wait": ("4", "RetryLedger"),
            "_probe_movement": ("4", "InformationAcquisitionState"),
            "_information_look": ("4", "InformationAcquisitionState"),
            "_begin_action_acquisition": ("4", "InformationAcquisitionState"),
            "_transition": ("1/5", "NavigationLifecycle"),
        },
        "planning_coordinator": {name: ("3" if name in {"advance", "_submit", "_record_admission"} else "4", "PlanningCoordinator + RetryLedger")
                                 for name in ("advance", "_submit", "_record_admission", "retry_from_current", "_retry_or_fail")},
        "motion_coordination": {name: ("3", "共同异步接纳 + 动作领域核验")
                                for name in ("_accept_result", "_retire_work", "_record_admission")},
        "retry_ledger": {name: ("4", "RetryLedger") for name in ("begin_recovery", "record_progress")},
        "safe_ground_control": {"verified_ground_rollout": ("2可选", "保留物理、安全尾迹核验")},
    }
    rows, sizes = [], {}
    modules = set(inventory) | {"navigation_handoff", "navigation_owners", "execution_supervisor", "navigation_lifecycle", "async_work"}
    files = [ROOT / f"mc2p/motion_nav/{name}.py" for name in sorted(modules)]
    files += [ROOT / f"mc2p/skills/{name}.py" for name in ("navigation_session_driver", "moving_melee_driver")]
    for path in files:
        text = path.read_text("utf-8")
        tree = ast.parse(text)
        nodes = list(ast.walk(tree))
        sizes[path.relative_to(ROOT).as_posix()] = {
            "lines": len(text.splitlines()), "sha256": digest(path.read_bytes()),
            "branches": sum(isinstance(node, (ast.If, ast.Match, ast.For, ast.While, ast.Try)) for node in nodes),
            "self_assignments": sum(isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                                    and isinstance(node.value, ast.Name) and node.value.id == "self" for node in nodes),
        }
        wanted = inventory.get(path.stem, {})
        functions = {node.name: node for node in nodes if isinstance(node, ast.FunctionDef)}
        for name, (step, owner) in wanted.items():
            if name not in functions:
                raise ValueError(f"migration function disappeared without inventory update: {path.name}:{name}")
            node = functions[name]
            def branch_hint(child):
                attributes = {item.attr for item in ast.walk(child)
                              if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name)
                              and item.value.id == "self"}
                categories = set()
                for attribute in attributes:
                    if any(word in attribute for word in ("planning", "snapshot", "worker")):
                        categories.add("PlanningCoordinator")
                    elif any(word in attribute for word in ("information", "probe", "acquisition")):
                        categories.add("InformationAcquisitionState")
                    elif any(word in attribute for word in ("supervisor", "executor", "tracker", "active_route")):
                        categories.add("ExecutionSupervisor")
                    elif any(word in attribute for word in ("retry", "risk", "budget", "wait")):
                        categories.add("TaskBudgetOwners")
                    elif any(word in attribute for word in ("goal", "request", "handoff", "terminal", "state")):
                        categories.add("Goal/Handoff/Lifecycle")
                return sorted(categories)
            rows.append({"id": f"{path.stem}.{name}", "file": path.relative_to(ROOT).as_posix(),
                         "line": node.lineno, "end_line": node.end_lineno, "step": step,
                         "current_owner": path.stem, "target_owner": owner, "status": "not_migrated",
                         "preserve": "领域事实、窗口、证明和身体责任不能因迁移删除",
                         "branches": [{"line": child.lineno, "kind": type(child).__name__,
                                       "condition": ast.unparse(child.test) if isinstance(child, (ast.If, ast.While)) else None,
                                       "owner_hints": branch_hint(child),
                                       "status": "requires_domain_mapping_before_migration"}
                                      for child in ast.walk(node) if isinstance(child, (ast.If, ast.Match, ast.Try, ast.While))]})
    return {"modules": sizes, "functions": rows,
            "coordination_scope_lines": sum(row["lines"] for row in sizes.values()),
            "limits": "AST counts are structural hints; multi-domain branches still need explicit owner/check mapping"}


def baseline(manifest_path, output, workers=1, *, seed_count=None, groups=None):
    if output.exists():
        raise FileExistsError(f"refusing to overwrite evidence: {output}")
    manifest, manifest_hash = load_manifest(manifest_path)
    count = manifest["seed_count"] if seed_count is None else seed_count
    if count < 1 or count > manifest["seed_count"] or not 1 <= workers <= 4:
        raise ValueError("invalid run count or worker limit")
    selected = manifest["groups"] if groups is None else [g for g in manifest["groups"] if g["id"] in groups]
    if not selected or groups is not None and len(selected) != len(set(groups)):
        raise ValueError("unknown/duplicate product group")
    output.mkdir(parents=True)
    (output / "traces").mkdir()
    production = source_fingerprint(["mc2p/**/*.py", "config/motion-navigation/*.json"])
    harness = source_fingerprint(["tests/sim/**/*.py", "scripts/navigation_coordination_metrics.py"])
    metadata = {
        "schema_version": SCHEMA, "extractor_version": EXTRACTOR_VERSION,
        "manifest_sha256": manifest_hash, "manifest": manifest,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "numpy": __import__("numpy").__version__},
        "production": production, "harness": harness,
        "workers": workers, "seed_count": count, "groups": [g["id"] for g in selected],
        "start_clock_ns": 100_000_000, "tick_seconds": .05,
        "complete_manifest": count == manifest["seed_count"] and len(selected) == len(manifest["groups"]),
        "timing_scope": "fake movement clock; wall elapsed measures simulator throughput only",
    }
    if "motion_delivery_profile" in manifest:
        metadata["motion_delivery_profile"] = validate_profile(manifest["motion_delivery_profile"])
        metadata["input_identity"] = {
            "base_manifest_sha256": manifest["motion_delivery_profile"]["base_manifest_sha256"],
            "delivery_profile_sha256": digest(json.dumps(metadata["motion_delivery_profile"], sort_keys=True).encode()),
            "manifest_sha256": manifest_hash,
        }
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", "utf-8")
    (output / "migration-inventory.json").write_text(json.dumps(coordination_inventory(), ensure_ascii=False, indent=2) + "\n", "utf-8")
    jobs = [(manifest, group, seed, str(output)) for group in selected
            for seed in range(manifest["seed_start"], manifest["seed_start"] + count)]
    started = time.perf_counter()
    records = []
    pool = None if workers == 1 else ProcessPoolExecutor(max_workers=workers)
    try:
        results = map(_run_one, jobs) if pool is None else pool.map(_run_one, jobs, chunksize=1)
        with (output / "runs.jsonl").open("w", encoding="utf-8") as stream:
            for record in results:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                records.append(record)
                if "motion_delivery_profile" in manifest and (
                        record["exception"] or record["metrics"]["safety_events"]
                        or not record["metrics"]["evidence_complete"]):
                    raise RuntimeError(f"v7 stopped at invalid evidence: {record['id']}")
                if len(records) % 100 == 0:
                    print(f"recorded {len(records)}/{len(jobs)} tasks", flush=True)
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures="motion_delivery_profile" in manifest)
    summary = {"schema_version": SCHEMA, "elapsed_seconds": time.perf_counter() - started,
               "groups": [], "records": len(records)}
    for group in selected:
        rows = [r for r in records if r["group"] == group["id"]]
        arrival = [r["metrics"]["arrival_ticks"] for r in rows if r["metrics"]["arrival_ticks"] is not None]
        summary["groups"].append({
            "group": group["id"], "tasks": len(rows),
            "success": sum(r["metrics"]["success"] for r in rows),
            "exceptions": sum(r["exception"] is not None for r in rows),
            "insufficient_evidence": sum(not r["metrics"]["evidence_complete"] for r in rows),
            "safety_violation_tasks": sum(bool(r["metrics"]["safety_events"]) for r in rows),
            "arrival_ticks_p95": quantile(arrival),
            "first_movement_ticks_p95": quantile([r["metrics"]["first_movement_ticks"] for r in rows if r["metrics"]["first_movement_ticks"] is not None]),
            "planning_requests": sum(r["metrics"]["planning_requests"] for r in rows),
            "zero_displacement_ticks": sum(r["metrics"]["zero_displacement_ticks"] for r in rows),
            "controller_switches": sum(r["metrics"]["controller_switches"] for r in rows),
        })
        if "motion_delivery_profile" in manifest:
            group_summary = summary["groups"][-1]
            group_summary["arrival_ticks_p50"] = quantile(arrival, .5)
            group_summary["motion_job_count_distribution"] = dict(Counter(r["motion_job_count"] for r in rows))
            group_summary["motion_operations"] = dict(Counter(
                job["operation"] for r in rows for job in r["motion_jobs"]))
            group_summary["followup_after_cold_tasks"] = sum(r["motion_job_count"] > 1 for r in rows)
            group_summary["tasks_with_motion_work"] = sum(r["motion_job_count"] > 0 for r in rows)
            group_summary["failure_reasons"] = dict(Counter(r["reason"] for r in rows if not r["metrics"]["success"]))
            group_summary["jobs_by_action"] = {
                action: dict(Counter(sum(job["action"] == action for job in row["motion_jobs"])
                                     for row in rows if any(job["action"] == action for job in row["motion_jobs"])))
                for action in sorted({job["action"] for row in rows for job in row["motion_jobs"]})}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return summary


def _delivery_evidence(root):
    # Reuse checksum/transport validation, without the old equivalence claim.
    from scripts.r28_baseline_alignment import records
    metadata, rows = records(root)
    transport_path = root / "trace-transport.json"
    transport = json.loads(transport_path.read_text("utf-8")) if transport_path.exists() else None
    return metadata, rows, transport


def compare_delivery(old_dir, new_dir, output):
    if output.exists():
        raise FileExistsError(f"refusing to overwrite comparison: {output}")
    before, old, old_transport = _delivery_evidence(old_dir)
    after, new, new_transport = _delivery_evidence(new_dir)
    manifest = dict(after["manifest"])
    profile = validate_profile(manifest.pop("motion_delivery_profile", None))
    if manifest != before["manifest"]:
        raise ValueError("delivery comparison task parameters differ beyond the model")
    if before["manifest_sha256"] != profile["base_manifest_sha256"]:
        raise ValueError("delivery comparison does not use the declared v6 input hash")
    for key in ("extractor_version", "start_clock_ns", "tick_seconds"):
        if before[key] != after[key]:
            raise ValueError(f"delivery comparison basis differs: {key}")
    if not new.keys() <= old.keys():
        raise ValueError("delivery comparison has unpaired tasks")
    changed = {name for name in before["harness"]["files"].keys() | after["harness"]["files"].keys()
               if before["harness"]["files"].get(name) != after["harness"]["files"].get(name)}
    allowed = {"tests/sim/motion_delivery.py", "scripts/navigation_coordination_metrics.py"}
    legacy = changed - allowed
    if legacy:
        from scripts.r28_baseline_alignment import review_harness_files
        review_harness_files({name: before["harness"]["files"].get(name) for name in legacy},
                             {name: after["harness"]["files"].get(name) for name in legacy})
    for name in changed & allowed:
        if digest((ROOT / name).read_bytes()) != after["harness"]["files"].get(name):
            raise ValueError(f"delivery harness source changed after collection: {name}")
    from scripts.r28_baseline_alignment import raw_record
    pairs, blocked = [], []
    for identity, current in new.items():
        previous = old[identity]
        if any(previous[key] != current[key] for key in ("parameters", "strict", "family", "group", "seed")):
            raise ValueError(f"paired task inputs/injections differ: {identity}")
        if current.get("motion_delivery_profile") != profile:
            raise ValueError(f"record delivery profile differs: {identity}")
        first = raw_record(old_dir, previous, old_transport)
        second = raw_record(new_dir, current, new_transport)
        old_trace, new_trace = trace_signatures(first["trace"]), trace_signatures(second["trace"])
        if (current["exception"] or current["metrics"]["safety_events"]
                or not current["metrics"]["evidence_complete"]):
            blocked.append(identity)
        pairs.append({"id": identity, "baseline_outcome": previous["metrics"]["outcome"],
                      "candidate_outcome": current["metrics"]["outcome"],
                      "reason": [previous["reason"], current["reason"]],
                      "arrival_ticks": [previous["metrics"]["arrival_ticks"], current["metrics"]["arrival_ticks"]],
                      "motion_job_count": [previous.get("motion_job_count"), current["motion_job_count"]],
                      "motion_jobs": current["motion_jobs"],
                      "inputs_changed": old_trace["inputs_sha256"] != new_trace["inputs_sha256"],
                      "trajectory_changed": old_trace["trajectory_sha256"] != new_trace["trajectory_sha256"],
                      "metrics_changed": previous["metrics"] != current["metrics"]})
    report = {"pairs": len(pairs), "input_parameters_verified": True,
              "full_original_denominator": old.keys() == new.keys(), "blocking_evidence": blocked,
              "motion_delivery_profile": profile, "harness_changed_files": sorted(changed),
              "baseline_motion_job_count_available": all("motion_job_count" in row for row in old.values()),
              "differences": pairs,
              "scope": "Changed deterministic transport; reports timing differences without an equivalence/regression claim."}
    output.mkdir(parents=True)
    (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return report


def verify_repeat(old_dir, new_dir, output):
    if output.exists():
        raise FileExistsError(f"refusing to overwrite repeat check: {output}")
    before, old, old_transport = _delivery_evidence(old_dir)
    after, new, new_transport = _delivery_evidence(new_dir)
    for key in ("manifest_sha256", "extractor_version", "groups", "seed_count", "harness", "environment"):
        if before[key] != after[key]:
            raise ValueError(f"repeat check basis differs: {key}")
    if old.keys() != new.keys():
        raise ValueError("repeat check task denominator differs")
    from scripts.r28_baseline_alignment import raw_record
    differences = []
    fields = ("parameters", "metrics", "reason", "exception", "verification_complete",
              "event_dispatches", "motion_delivery_profile", "motion_job_count", "motion_jobs")
    for identity, previous in old.items():
        current = new[identity]
        first = raw_record(old_dir, previous, old_transport)
        second = raw_record(new_dir, current, new_transport)
        changed = [key for key in fields if previous.get(key) != current.get(key)]
        if trace_signatures(first["trace"]) != trace_signatures(second["trace"]):
            changed.append("inputs_or_trajectory")
        if changed:
            differences.append({"id": identity, "fields": changed})
    report = {"pairs": len(new), "differences": differences,
              "scope": "Exact task results, reasons, delivery records, input and physical trajectory after random-ID normalization."}
    output.mkdir(parents=True)
    (output / "repeat.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return report


def compare(old_dir, new_dir, output):
    if output.exists():
        raise FileExistsError(f"refusing to overwrite comparison: {output}")
    metadata = [json.loads((p / "metadata.json").read_text("utf-8")) for p in (old_dir, new_dir)]
    if any(metadata[0][key] != metadata[1][key] for key in ("manifest_sha256", "extractor_version", "groups", "seed_count", "harness", "environment")):
        raise ValueError("comparison requires the same frozen manifest, extractor and harness")
    def read_records(folder):
        records = [json.loads(line) for line in (folder / "runs.jsonl").read_text("utf-8").splitlines()]
        result = {r["id"]: r for r in records}
        if len(result) != len(records):
            raise ValueError("duplicate task IDs")
        expected = {f"{group}-{seed:06d}" for group in metadata[0]["groups"]
                    for seed in range(metadata[0]["manifest"]["seed_start"],
                                      metadata[0]["manifest"]["seed_start"] + metadata[0]["seed_count"])}
        if result.keys() != expected:
            raise ValueError("task records do not cover the frozen denominator")
        for record in records:
            trace_path = folder / record["trace_file"]
            if digest(trace_path.read_bytes()) != record["trace_sha256"]:
                raise ValueError("raw evidence checksum mismatch")
            with gzip.open(trace_path, "rt", encoding="utf-8") as stream:
                raw = json.load(stream)
            if raw["record"] != {k: v for k, v in record.items() if k != "trace_sha256"}:
                raise ValueError("summary record disagrees with raw evidence")
            record["_strict_trace"] = raw["strict_trace"]
        return result
    old, new = read_records(old_dir), read_records(new_dir)
    if old.keys() != new.keys():
        raise ValueError("paired task IDs differ; failures cannot disappear")
    pairs = []
    for key, b in old.items():
        c = new[key]
        if b["parameters"] != c["parameters"]:
            raise ValueError("paired task inputs/injections differ")
        result = compare_metrics(b["metrics"], c["metrics"], tick_tolerance=metadata[0]["manifest"]["ground_tick_tolerance"])
        if b["strict"] and result["status"] != "insufficient_evidence":
            old_trace, new_trace = b["_strict_trace"], c["_strict_trace"]
            if old_trace is None or new_trace is None:
                result["status"] = "insufficient_evidence"
            elif old_trace != new_trace:
                result["status"] = "different"
                result["differences"].append("strict_trace")
        pairs.append({"id": key, **result})
    report = {"pairs": len(pairs), "equivalent": sum(p["status"] == "equivalent" for p in pairs),
              "differences": [p for p in pairs if p["status"] != "equivalent"],
              "scope": "behaviour-preserving migration; not a behaviour-changing statistical release gate"}
    output.mkdir(parents=True)
    (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return report


def reextract(source: Path, output: Path):
    """Write a new ruler report from immutable old traces, keeping the originals."""
    if output.exists():
        raise FileExistsError(f"refusing to overwrite evidence: {output}")
    metadata = json.loads((source / "metadata.json").read_text("utf-8"))
    records = [json.loads(line) for line in (source / "runs.jsonl").read_text("utf-8").splitlines()]
    output.mkdir(parents=True)
    (output / "traces").mkdir()
    metadata["reextracted_from"] = str(source)
    metadata["source_extractor_version"] = metadata["extractor_version"]
    metadata["extractor_version"] = EXTRACTOR_VERSION
    metadata["harness"] = source_fingerprint(["tests/sim/**/*.py", "scripts/navigation_coordination_metrics.py"])
    with (output / "runs.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            path = source / record["trace_file"]
            if digest(path.read_bytes()) != record["trace_sha256"]:
                raise ValueError("raw evidence checksum mismatch")
            with gzip.open(path, "rt", encoding="utf-8") as raw_stream:
                raw = json.load(raw_stream)
            if raw["record"] != {k: v for k, v in record.items() if k != "trace_sha256"}:
                raise ValueError("summary record disagrees with raw evidence")
            trace = raw["trace"]
            # Reconstruct only the frozen input, never actor state or permissions.
            group = next(group for group in metadata["manifest"]["groups"] if group["id"] == record["group"])
            scenario, _ = product_scenario(metadata["manifest"], group, record["seed"])
            metrics = extract_metrics(trace, start_tick=1, start_position=scenario.start,
                outcome=record["metrics"]["outcome"], violations=record["metrics"]["safety_events"])
            if not record["verification_complete"]:
                metrics["evidence_complete"] = False
                metrics["coverage_gaps"].append("formal_monitor_coverage_incomplete")
            record = dict(record, metrics=metrics, source_trace_sha256=record["trace_sha256"])
            with gzip.open(output / record["trace_file"], "wt", encoding="utf-8", compresslevel=3) as raw_stream:
                json.dump({"record": {k: v for k, v in record.items() if k != "trace_sha256"},
                           "trace": trace, "strict_trace": raw["strict_trace"]}, raw_stream)
            record["trace_sha256"] = digest((output / record["trace_file"]).read_bytes())
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return {"records": len(records), "extractor": EXTRACTOR_VERSION, "source_preserved": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    base = sub.add_parser("baseline")
    base.add_argument("--manifest", type=Path, required=True)
    base.add_argument("--output", type=Path, required=True)
    base.add_argument("--workers", type=int, default=1)
    base.add_argument("--seed-count", type=int, help="exploration only; subset is marked incomplete")
    base.add_argument("--groups", nargs="+")
    pair = sub.add_parser("compare")
    pair.add_argument("--baseline", type=Path, required=True)
    pair.add_argument("--candidate", type=Path, required=True)
    pair.add_argument("--output", type=Path, required=True)
    for command in ("delivery-compare", "verify-repeat"):
        timing = sub.add_parser(command)
        timing.add_argument("--baseline", type=Path, required=True)
        timing.add_argument("--candidate", type=Path, required=True)
        timing.add_argument("--output", type=Path, required=True)
    ruler = sub.add_parser("reextract")
    ruler.add_argument("--source", type=Path, required=True)
    ruler.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "reextract":
        result = reextract(args.source, args.output)
    elif args.command in {"delivery-compare", "verify-repeat"}:
        operation = compare_delivery if args.command == "delivery-compare" else verify_repeat
        result = operation(args.baseline, args.candidate, args.output)
    else:
        result = baseline(args.manifest, args.output, args.workers, seed_count=args.seed_count, groups=args.groups) if args.command == "baseline" else compare(args.baseline, args.candidate, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return (int(bool(result["differences"])) if args.command in {"compare", "verify-repeat"}
            else int(bool(result["blocking_evidence"])) if args.command == "delivery-compare" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
