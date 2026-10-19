"""Compare immutable R28 records before migrating coordination behaviour.

This is a baseline bridge, not the behaviour-preserving or statistical release
gate. Original metadata and traces are never rewritten.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.navigation_coordination_metrics import quantile
from tests.sim.product_metrics import compare_metrics

PHYSICAL_FIELDS = ("movement_tick", "position", "velocity", "on_ground", "pose",
                   "source_bound", "controller_ids", "applied_movement", "input_window")


def records(root):
    rows = [json.loads(line) for line in (root / "runs.jsonl").read_text("utf-8").splitlines()]
    indexed = {row["id"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("duplicate task identity")
    metadata = json.loads((root / "metadata.json").read_text("utf-8"))
    start = metadata["manifest"]["seed_start"]
    expected = {f"{group}-{seed:06d}" for group in metadata["groups"]
                for seed in range(start, start + metadata["seed_count"])}
    if indexed.keys() != expected:
        raise ValueError("records do not cover their declared denominator")
    return metadata, indexed


def raw_record(root, row, transport=None):
    entry = None if transport is None else transport["traces"][row["trace_file"]]
    if entry is not None and entry["source_compressed_sha256"] != row["trace_sha256"]:
        raise ValueError("transport does not match the original compressed record")
    path = (root / (row["trace_file"] if entry is None else entry["archive_path"])).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("trace path escapes evidence root")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != (row["trace_sha256"] if entry is None else entry["decompressed_sha256"]):
        raise ValueError("trace checksum mismatch")
    raw = json.loads(gzip.decompress(data) if entry is None else data)
    if raw["record"] != {key: value for key, value in row.items() if key != "trace_sha256"}:
        raise ValueError("trace and summary disagree")
    return raw


def verify_harness(old, new):
    for key in ("manifest_sha256", "extractor_version", "environment", "start_clock_ns", "tick_seconds"):
        if old[key] != new[key]:
            raise ValueError(f"comparison basis differs: {key}")
    return review_harness_files(old["harness"]["files"], new["harness"]["files"])


def review_harness_files(before, after):
    changes = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    if not changes:
        return {"changed_files": [], "original_metadata_preserved": True}
    if not set(changes) <= {"tests/sim/runner.py", "tests/sim/async_work_sequences.py"}:
        raise ValueError(f"unreviewed simulator changes: {changes}")
    for name in changes:
        data = (ROOT / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != after[name]:
            raise ValueError("harness source changed after collection")
        restored = data.decode("utf-8").replace("\r\n", "\n")
        if name == "tests/sim/runner.py":
            restored = restored.replace("planner_factory=InlinePlannerWorker,\n        motion_factory=InlineMotionWorker)",
                                        "planner_factory=InlinePlannerWorker)")
            restored = restored.replace("motion = motion_factory()", "motion = InlineMotionWorker()")
        else:
            # Product scenarios do not call this independent async fixture.
            # Reconstruct the old bytes to verify that only its worker service
            # and diagnostic return changed, without editing either metadata.
            restored = restored.replace("def run_gap_sequence(seed, *, service_followup=True):", "def run_gap_sequence(seed):")
            restored = restored.replace("    serviced_job_count = 0\n    followup_operations = []\n", "")
            restored = restored.replace("                elif delivered and service_followup and len(worker.jobs) > serviced_job_count:\n"
                "                    # Interleaving is the injected fault, not permanent worker\n"
                "                    # starvation. Service later solve/revalidation jobs through\n"
                "                    # the same real implementation, on the following frame.\n"
                "                    pending = worker.jobs[serviced_job_count:]\n"
                "                    worker.deliveries.extend(_execute_job(job) for job in pending)\n"
                "                    followup_operations.extend(job.operation.value for job in pending)\n"
                "                    serviced_job_count = len(worker.jobs)\n", "")
            restored = restored.replace("                    serviced_job_count = len(worker.jobs)\n", "")
            restored = restored.replace("                \"events\": events, \"followup_operations\": followup_operations,\n"
                                        "                \"verification\": asdict(verification), \"trace\": trace}",
                                        "                \"events\": events, \"verification\": asdict(verification), \"trace\": trace}")
        hashes = {hashlib.sha256(restored.replace("\n", newline).encode()).hexdigest()
                  for newline in ("\n", "\r\n")}
        if before[name] not in hashes:
            raise ValueError(f"harness differs beyond the reviewed fixture change: {name}")
    return {"changed_files": changes, "original_metadata_preserved": True,
            "review": "The runner retains its existing default worker. The separate async fixture now services follow-up jobs. Original hashes are reconstructed before allowing comparison.",
            "source_hashes": {name: {"baseline": before[name], "candidate": after[name]} for name in changes}}


def compare_migration(old_root, new_root):
    summaries = [json.loads((root / "summary.json").read_text("utf-8")) for root in (old_root, new_root)]
    transports = [json.loads((root / 'trace-transport.json').read_text('utf-8'))
                  if (root / 'trace-transport.json').exists() else None
                  for root in (old_root, new_root)]
    harness = review_harness_files(
        {name: value for name, value in summaries[0]["harness"]["files"].items() if name.startswith("tests/sim/")},
        {name: value for name, value in summaries[1]["harness"]["files"].items() if name.startswith("tests/sim/")})
    if [row["id"] for row in summaries[0]["cases"]] != [row["id"] for row in summaries[1]["cases"]]:
        raise ValueError("migration case denominator differs")
    differences, failures = [], []
    for old_entry, new_entry in zip(summaries[0]["cases"], summaries[1]["cases"]):
        values = []
        for root, entry, transport in ((old_root, old_entry, transports[0]),
                                       (new_root, new_entry, transports[1])):
            mapping = None if transport is None else transport['traces'][entry['file']]
            if mapping is not None and mapping['source_compressed_sha256'] != entry['sha256']:
                raise ValueError('migration transport checksum mismatch')
            path = (root / (entry['file'] if mapping is None else mapping['archive_path'])).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("migration trace escapes its root")
            data = path.read_bytes()
            expected_hash = entry['sha256'] if mapping is None else mapping['decompressed_sha256']
            if hashlib.sha256(data).hexdigest() != expected_hash:
                raise ValueError("migration trace checksum mismatch")
            values.append(json.loads(gzip.decompress(data) if mapping is None else data))
        old, new = values
        if old["input"] != new["input"] or old["id"] != new["id"]:
            raise ValueError("migration inputs differ")
        if not new["passed"] or new["exception"]:
            failures.append(new["id"])
        if old["signature"] != new["signature"]:
            differences.append({"id": new["id"], "kind": new["kind"],
                "fields": [key for key in old["signature"].keys() | new["signature"].keys()
                           if old["signature"].get(key) != new["signature"].get(key)],
                "baseline_outcome": old["signature"].get("outcome"),
                "candidate_outcome": new["signature"].get("outcome"), "trace_file": new_entry["file"]})
    return {"pairs": len(summaries[1]["cases"]), "failed": failures,
            "difference_count": len(differences), "differences": differences,
            "difference_kinds": dict(Counter(item["kind"] for item in differences)),
            "harness_review": harness, "product_denominator": False,
            "scope": "Same injected events; differences include declared motion changes and serviced follow-up validation. Original records remain immutable."}


def compare(old_root, new_root):
    old_meta, old = records(old_root)
    new_meta, new = records(new_root)
    harness = verify_harness(old_meta, new_meta)
    transports = [json.loads((root / "trace-transport.json").read_text("utf-8"))
                  if (root / "trace-transport.json").exists() else None for root in (old_root, new_root)]
    if not new.keys() <= old.keys():
        raise ValueError("candidate contains unpaired tasks")
    differences, lost, gained, blocked = [], [], [], []
    groups = {}
    for name, current in new.items():
        previous = old[name]
        if any(previous[key] != current[key] for key in ("parameters", "strict", "family", "group", "seed")):
            raise ValueError(f"task inputs differ: {name}")
        a, b = raw_record(old_root, previous, transports[0]), raw_record(new_root, current, transports[1])
        am, bm = previous["metrics"], current["metrics"]
        group = groups.setdefault(current["group"], {"pairs": 0, "baseline_success": 0, "candidate_success": 0,
                                                    "paired_arrival_before": [], "paired_arrival_after": []})
        group["pairs"] += 1
        group["baseline_success"] += am["success"]
        group["candidate_success"] += bm["success"]
        if am["success"] and not bm["success"]:
            lost.append(name)
        if not am["success"] and bm["success"]:
            gained.append(name)
        if current["exception"] or bm["safety_events"] or not bm["evidence_complete"]:
            blocked.append(name)
        if am["success"] and bm["success"]:
            group["paired_arrival_before"].append(am["arrival_ticks"])
            group["paired_arrival_after"].append(bm["arrival_ticks"])
        same_body = [[row.get(key) for key in PHYSICAL_FIELDS] for row in a["trace"]] == [
            [row.get(key) for key in PHYSICAL_FIELDS] for row in b["trace"]]
        strict_same = a["strict_trace"] == b["strict_trace"]
        metric_delta = compare_metrics(am, bm, tick_tolerance=old_meta["manifest"]["ground_tick_tolerance"])
        if not same_body or not strict_same or metric_delta["status"] != "equivalent":
            first = next((index for index, (x, y) in enumerate(zip(a["trace"], b["trace"]))
                          if any(x.get(key) != y.get(key) for key in PHYSICAL_FIELDS)),
                         min(len(a["trace"]), len(b["trace"])) if not same_body else None)
            differences.append({"id": name, "family": current["family"], "case": current["parameters"]["case"],
                                "metric_comparison": metric_delta, "physical_trace_changed": not same_body,
                                "strict_trace_changed": not strict_same, "first_physical_difference_index": first,
                                "baseline_outcome": am["outcome"], "candidate_outcome": bm["outcome"],
                                "baseline_reason": previous["reason"], "candidate_reason": current["reason"],
                                "arrival_ticks": [am["arrival_ticks"], bm["arrival_ticks"]],
                                "terminal_ticks": [am["terminal_ticks"], bm["terminal_ticks"]],
                                "trace_file": current["trace_file"]})
    for group in groups.values():
        for suffix in ("before", "after"):
            samples = group.pop(f"paired_arrival_{suffix}")
            group[f"paired_arrival_{suffix}"] = {"samples": len(samples), "p95": quantile(samples),
                                                  "mean": None if not samples else sum(samples) / len(samples)}
    return {"pairs": len(new), "full_original_denominator": new.keys() == old.keys(), "groups": groups,
            "lost_successes": lost, "gained_successes": gained, "blocking_evidence": blocked,
            "difference_count": len(differences), "difference_families": dict(Counter(d["family"] for d in differences)),
            "differences": differences, "harness_review": harness,
            "scope": "Fixed baseline bridge. Changed traces require review; no behaviour-preserving or statistical release claim."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--migration", action="store_true")
    args = parser.parse_args()
    result = compare_migration(args.baseline, args.candidate) if args.migration else compare(args.baseline, args.candidate)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in result.items() if key != "differences"}, ensure_ascii=False))
    raise SystemExit(int(bool(result.get("lost_successes") or result.get("blocking_evidence") or result.get("failed"))))
