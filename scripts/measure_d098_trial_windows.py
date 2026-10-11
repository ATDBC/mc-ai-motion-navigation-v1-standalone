"""Run the unchanged f5e43e7 timing harness on Windows, outside its timing regions.

This is an explicit review-input runner, never imported by the action runtime.
Use the project's Python 3.11 environment. T2/T3 arguments are deliberately fixed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import runpy
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TRIAL_ARGV = {
    "T2": ("perf_record", "--no-setd"),
    "T3": ("perf_record", "--passes", "1"),
}
REFERENCE_FILES = {
    "T2": ("T2_performance_record_AC.json", "T2_performance_record_samples_AC.json"),
    "T3": ("T3_performance_record_ACD.json", "T3_performance_record_samples_ACD.json.gz"),
}
RECORD_OUTPUTS = ("performance_record.json", "performance_record_samples.json")
RECORD_CONFIGS = ("strict", "strict_runahead", "full_scan")
# d097a-m2-perf-record-v1 calls its wall-clock millisecond column "ms".
WALL_TIME_COLUMN = "ms"


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def initialize_load(getter, sleeper=time.sleep):
    """psutil starts Windows's 5-second sampling thread on its first call."""
    try:
        getter()
        if platform.system() == "Windows":
            sleeper(5.5)
        return getter()
    except (OSError, NotImplementedError):
        return None


@contextmanager
def execution_record(path, metadata):
    started = time.perf_counter()
    metadata.update(execution="running", started_utc=datetime.now(timezone.utc).isoformat())
    write_json(path, metadata)
    try:
        yield metadata
    except BaseException as exc:
        metadata.update(execution="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        metadata["execution"] = "completed"
    finally:
        metadata["elapsed_seconds"] = time.perf_counter() - started
        write_json(path, metadata)


def invoke_trial(trial, label, load_getter):
    script = trial / "m2" / "run_m2.py"
    before = script.read_bytes()
    outputs = [script.parent / name for name in RECORD_OUTPUTS]
    for path in outputs:
        path.unlink(missing_ok=True)
    old_argv, old_path = sys.argv[:], sys.path[:]
    had_load = hasattr(os, "getloadavg")
    previous_load = getattr(os, "getloadavg", None)
    try:
        sys.path[:0] = [str(ROOT), str(trial), str(trial / "m2")]
        sys.argv = [str(script), *TRIAL_ARGV[label]]
        if platform.system() == "Windows" and not had_load:
            os.getloadavg = load_getter
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
    finally:
        sys.argv, sys.path[:] = old_argv, old_path
        if had_load:
            os.getloadavg = previous_load
        elif hasattr(os, "getloadavg"):
            del os.getloadavg
        if script.read_bytes() != before:
            raise RuntimeError("Trial script bytes changed")
    if not all(path.is_file() for path in outputs):
        raise RuntimeError("Trial did not generate both fresh outputs")


def deterministic_gzip(raw):
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as handle:
        handle.write(raw)
    return out.getvalue()


def _sample_layout(payload):
    columns = payload.get("columns")
    required = {"pass", "config", "cand", "kind", "steps", WALL_TIME_COLUMN}
    if (not isinstance(columns, list) or not all(isinstance(c, str) for c in columns)
            or len(set(columns)) != len(columns) or not required.issubset(columns)):
        raise ValueError("Sample columns must declare the record schema and its ms wall column")
    return columns, {name: columns.index(name) for name in required}


def sample_signature(payload):
    columns, indices = _sample_layout(payload)
    non_time = [i for i in range(len(columns)) if i != indices[WALL_TIME_COLUMN]]
    digest = hashlib.sha256()
    digest.update(json.dumps(columns, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")
    for row in payload["rows"]:
        if len(row) != len(columns):
            raise ValueError("Sample row does not match its declared columns")
        digest.update(json.dumps([row[i] for i in non_time], separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return {"columns": columns, "wall_time_column": WALL_TIME_COLUMN,
            "rows": len(payload["rows"]), "non_time_sha256": digest.hexdigest()}


def candidate_coverage(payload, population):
    """Infer actual candidate runs from named sample columns, not summary assertions."""
    columns, indices = _sample_layout(payload)
    passes, candidates = population["passes"], population["candidates"]
    if type(passes) is not int or type(candidates) is not int or min(passes, candidates) < 1:
        raise ValueError("Record population must contain positive integer counts")
    pairs = {config: set() for config in RECORD_CONFIGS}
    rows = {config: 0 for config in RECORD_CONFIGS}
    occurrences = {config: Counter() for config in RECORD_CONFIGS}
    invalid_rows, full_scan_kinds, unexpected = 0, True, set()
    for row in payload["rows"]:
        if len(row) != len(columns):
            raise ValueError("Sample row does not match its declared columns")
        config = row[indices["config"]]
        if config not in pairs:
            unexpected.add(str(config))
            continue
        pass_index, cand = row[indices["pass"]], row[indices["cand"]]
        if type(pass_index) is not int or not 0 <= pass_index < passes or not isinstance(cand, str) or not cand:
            invalid_rows += 1
            continue
        pairs[config].add((pass_index, cand))
        rows[config] += 1
        kind = row[indices["kind"]]
        if kind == ("full_scan" if config == "full_scan" else "plan"):
            occurrences[config][(pass_index, cand)] += 1
        if config == "full_scan" and row[indices["kind"]] != "full_scan":
            full_scan_kinds = False
    pool = {cand for pass_index, cand in pairs["full_scan"] if pass_index == 0}
    expected = {(pass_index, cand) for pass_index in range(passes) for cand in pool}
    expected_count = candidates * passes
    configs = {config: {"rows": rows[config], "candidate_runs": len(pairs[config]),
                        "candidate_occurrences": sum(occurrences[config].values()),
                        "missing_candidate_runs": len(expected - pairs[config]),
                        "extra_candidate_runs": len(pairs[config] - expected)} for config in RECORD_CONFIGS}
    full_counts = occurrences["full_scan"]
    pass_counts = [Counter({cand: count for (p, cand), count in full_counts.items() if p == pass_index})
                   for pass_index in range(passes)]
    occurrence_match = all(occurrences[config] == full_counts for config in RECORD_CONFIGS)
    complete = (not invalid_rows and not unexpected and full_scan_kinds
                and all(sum(counts.values()) == candidates and counts == pass_counts[0]
                        for counts in pass_counts)
                and rows["full_scan"] == expected_count
                and occurrence_match
                and all(pairs[config] == expected for config in RECORD_CONFIGS))
    return {"complete": complete, "expected_candidate_runs": expected_count,
            "candidate_ids_per_pass": len(pool),
            "repeated_candidate_id_occurrences_per_pass": candidates - len(pool),
            "candidate_occurrence_counts_match": occurrence_match, "configs": configs,
            "invalid_rows": invalid_rows, "unexpected_configs": sorted(unexpected),
            "full_scan_kinds_valid": full_scan_kinds}


def inventory(trial):
    paths = sorted(trial.rglob("*.py"))
    paths += [trial / p for p in ("m1/table.json", "m1/accepted_candidates.jsonl",
                                 "m2/set_a_inputs.json", "m2/set_c_inputs.json")]
    return {p.relative_to(trial).as_posix(): sha256(p.read_bytes()) for p in paths}


def analyze_record(trial, label, raw_summary, raw_samples, compressed):
    """Audit existing record bytes; this function does not run or time the trial."""
    summary, samples = json.loads(raw_summary), json.loads(raw_samples)
    reference_summary_name, reference_samples_name = REFERENCE_FILES[label]
    reference_summary_bytes = (trial / "timing" / reference_summary_name).read_bytes()
    reference_samples_bytes = (trial / "timing" / reference_samples_name).read_bytes()
    if reference_samples_name.endswith(".gz"):
        reference_samples_bytes = gzip.decompress(reference_samples_bytes)
    reference = json.loads(reference_summary_bytes)
    reference_signature = sample_signature(json.loads(reference_samples_bytes))
    actual_signature = sample_signature(samples)
    coverage = candidate_coverage(samples, summary["population"])
    steps_match = all(
        config["steps"] == reference["configs"][name][kind]["steps"]
        for name, values in summary["configs"].items() for kind, config in values.items())
    checks = {
        "population_match": summary["population"] == reference["population"],
        "trial_code_hashes_match": summary["code_sha256"] == reference["code_sha256"],
        "sample_columns_match_reference": actual_signature["columns"] == reference_signature["columns"],
        "all_configurations_cover_complete_candidate_runs": coverage["complete"],
        "all_candidates_verified_in_original_harness": (
            coverage["complete"] and summary["code_sha256"] == reference["code_sha256"]
            and actual_signature == reference_signature),
        "step_totals_equal_across_three_configs": summary["step_total_check"]["all_equal"],
        "step_total_check_matches_reference": summary["step_total_check"] == reference["step_total_check"],
        "every_sample_non_time_fields_match_reference": actual_signature == reference_signature,
        "all_step_distributions_match_reference": steps_match,
        "not_smoke": summary["SMOKE_TEST_NOT_A_RECORD"] is False,
    }
    view = summary["gate_view"]["strict_runahead"]
    result = {
        "integrity_schema": "d098-w0-sample-audit-v2",
        "integrity_checks": checks, "reproduction_integrity_passed": all(checks.values()),
        "candidate_coverage": coverage,
        "classification_check_source": (
            "Complete named-column (pass,cand) coverage in all three configurations, full_scan row count "
            "equal to candidates*passes, plan/full_scan occurrence counters matching even for repeated cand "
            "names, frozen asserting-harness code hashes and column-aware non-time "
            "sample signature equal to the frozen record; no independent verdict column is present."),
        "actual_sample_signature": actual_signature, "reference_sample_signature": reference_signature,
        "reference_summary_sha256": sha256(reference_summary_bytes),
        "reference_samples_uncompressed_sha256": sha256(reference_samples_bytes),
        "samples_uncompressed_sha256": sha256(raw_samples), "samples_uncompressed_bytes": len(raw_samples),
        "samples_gzip_sha256": sha256(compressed), "samples_gzip_bytes": len(compressed),
        "summary_sha256": sha256(raw_summary),
        "F1_exploration_gate": view,
        "performance_baseline_passed": all(view[k] for k in
                                          ("tick_le_10ms", "commit_le_50ms", "tick_samples_ge_1000")),
        "scope": "W0 exploration only; not the formal 8/15/30 ms gate",
    }
    return result


def collect_result(trial, label, output):
    raw_summary = (trial / "m2" / RECORD_OUTPUTS[0]).read_bytes()
    raw_samples = (trial / "m2" / RECORD_OUTPUTS[1]).read_bytes()
    compressed = deterministic_gzip(raw_samples)
    result = analyze_record(trial, label, raw_summary, raw_samples, compressed)
    (output / f"{label}-summary.json").write_bytes(raw_summary)
    (output / f"{label}-samples.json.gz").write_bytes(compressed)
    write_json(output / f"{label}-result.json", result)
    if not result["reproduction_integrity_passed"]:
        raise RuntimeError(f"{label} reproduction integrity failed; stop W0")
    return result


def _analyze_saved_evidence(trial, label, output):
    """Read and recompute the record without writing any file."""
    previous_bytes = (output / f"{label}-result.json").read_bytes()
    previous = json.loads(previous_bytes)
    raw_summary = (output / f"{label}-summary.json").read_bytes()
    compressed = (output / f"{label}-samples.json.gz").read_bytes()
    raw_samples = gzip.decompress(compressed)
    for actual, expected in ((sha256(raw_summary), previous["summary_sha256"]),
                             (sha256(compressed), previous["samples_gzip_sha256"]),
                             (sha256(raw_samples), previous["samples_uncompressed_sha256"])):
        if actual != expected:
            raise RuntimeError("Saved record hash mismatch; do not recheck modified evidence")
    result = analyze_record(trial, label, raw_summary, raw_samples, compressed)
    if not result["reproduction_integrity_passed"]:
        raise RuntimeError(f"{label} reproduction integrity failed in saved-evidence check")
    return previous_bytes, previous, result


def recheck_evidence(trial, label, output):
    """Daily read-only verification; keep the signed result and its provenance unchanged."""
    _, previous, result = _analyze_saved_evidence(trial, label, output)
    if ("audit_provenance" not in previous
            and previous.get("integrity_schema") != "d098-w0-sample-audit-v2"):
        raise RuntimeError("No existing v2 audit; use --audit-evidence once")
    signed_result = {key: value for key, value in previous.items() if key != "audit_provenance"}
    if result != signed_result:
        raise RuntimeError("Saved audit does not match recomputed evidence")
    return previous


def audit_evidence(trial, label, output):
    """Generate the v2 audit once, refusing to overwrite an existing signed audit."""
    previous = json.loads((output / f"{label}-result.json").read_bytes())
    if ("audit_provenance" in previous
            or previous.get("integrity_schema") == "d098-w0-sample-audit-v2"):
        raise RuntimeError("Saved v2 audit already exists; use --recheck-evidence")
    previous_bytes, _, result = _analyze_saved_evidence(trial, label, output)
    execution = json.loads((output / f"{label}-execution.json").read_bytes())
    result["audit_provenance"] = {
        "kind": "reanalysis_of_saved_summary_and_gzip", "timing_rerun": False,
        "previous_result_sha256": sha256(previous_bytes),
        "original_execution_wrapper_sha256": execution["wrapper_sha256"],
        "original_execution_started_utc": execution["started_utc"],
        "recheck_wrapper_sha256": sha256(Path(__file__).read_bytes()),
        "raw_summary_and_gzip_unchanged": True,
        "fresh_output_guard_retroactively_verified": False,
        "note": "Execution metadata keeps its historical v1 audit; this result is the current v2 audit.",
    }
    write_json(output / f"{label}-result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", choices=TRIAL_ARGV)
    parser.add_argument("--trial-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    audit_modes = parser.add_mutually_exclusive_group()
    audit_modes.add_argument("--recheck-evidence", action="store_true",
                             help="verify the existing v2 audit read-only; never modify evidence")
    audit_modes.add_argument("--audit-evidence", action="store_true",
                             help="generate the v2 audit once; refuse to overwrite an existing audit")
    args = parser.parse_args(argv)
    if args.recheck_evidence or args.audit_evidence:
        check = recheck_evidence if args.recheck_evidence else audit_evidence
        result = check(args.trial_dir.resolve(), args.label, args.output_dir.resolve())
        print(json.dumps({"label": args.label, "timing_rerun": False, "read_only": args.recheck_evidence,
                          "reproduction_integrity_passed": result["reproduction_integrity_passed"]}))
        return 0
    if platform.system() != "Windows":
        parser.error("W0 requires the formal Windows platform")
    import psutil
    trial, output = args.trial_dir.resolve(), args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    metadata = {
        "label": args.label, "argv": ["run_m2.py", *TRIAL_ARGV[args.label]],
        "wrapper_sha256": sha256(Path(__file__).read_bytes()),
        "project_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "platform": platform.platform(), "python": sys.version, "python_executable": sys.executable,
        "processor": platform.processor(), "logical_cpus": os.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False), "psutil_version": psutil.__version__,
        "load_provider": "psutil.getloadavg", "load_initialization_seconds": 5.5,
    }
    with execution_record(output / f"{args.label}-execution.json", metadata):
        metadata["loadavg_before"] = initialize_load(psutil.getloadavg)
        if metadata["loadavg_before"] is None:
            raise RuntimeError("Windows load interface unavailable; recorded null")
        before = inventory(trial)
        metadata["trial_sha256_before"] = before
        reference = json.loads((trial / "timing" / REFERENCE_FILES[args.label][0]).read_bytes())
        if any(before[f"m2/{name}"] != digest for name, digest in reference["code_sha256"].items()):
            raise RuntimeError("Frozen trial code hash mismatch; do not run")
        invoke_trial(trial, args.label, psutil.getloadavg)
        metadata["loadavg_after"] = psutil.getloadavg()
        metadata["trial_sha256_after"] = inventory(trial)
        if before != metadata["trial_sha256_after"]:
            raise RuntimeError("Frozen trial source or input bytes changed")
        metadata["result"] = collect_result(trial, args.label, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
