"""Pair a Linux re-run of 7260f42 with the archived r28-baseline-v6 product run.

    python compare_v6.py <unpacked v6 baseline.tar.gz> <re-run directory>

Checks, per task: the external metrics, the business reason, and the full per-tick trace
(every recorded field, including body position, applied input and owners).  Wall-clock
fields of the harness (wall_elapsed_seconds) are not part of the trace and are ignored.
"""
import collections
import gzip
import json
import pathlib
import sys

archive, rerun = (pathlib.Path(p) for p in sys.argv[1:3])


def runs(root):
    return {json.loads(l)["id"]: json.loads(l) for l in (root / "runs.jsonl").read_text("utf-8").splitlines()}


def trace(root, record):
    path = root / record["trace_file"]
    if path.exists():
        return json.load(gzip.open(path, "rt", encoding="utf-8"))
    return json.loads((root / record["trace_file"].removesuffix(".gz")).read_text("utf-8"))


old, new = runs(archive), runs(rerun)
assert old.keys() == new.keys(), (len(old), len(new))
layers = collections.defaultdict(collections.Counter)
mismatch = []
for key in sorted(old):
    a, b = old[key], new[key]
    c = layers[a["group"]]
    c["tasks"] += 1
    c["archive"] += a["metrics"]["success"]
    c["rerun"] += b["metrics"]["success"]
    same_metrics = a["metrics"] == b["metrics"] and a["reason"] == b["reason"]
    ta, tb = trace(archive, a), trace(rerun, b)
    same_trace = ta["trace"] == tb["trace"] and ta["strict_trace"] == tb["strict_trace"]
    c["same_metrics"] += same_metrics
    c["same_trace"] += same_trace
    if not (same_metrics and same_trace):
        mismatch.append((key, same_metrics, same_trace))
print(f"{'group':<15}{'archive':>9}{'re-run':>9}{'same metrics':>14}{'same trace':>12}")
total = collections.Counter()
for g in sorted(layers):
    c = layers[g]
    total.update(c)
    print(f"{g:<15}{c['archive']:>5}/{c['tasks']:<3}{c['rerun']:>5}/{c['tasks']:<3}{c['same_metrics']:>10}/{c['tasks']:<3}{c['same_trace']:>8}/{c['tasks']}")
print(f"{'total':<15}{total['archive']:>5}/{total['tasks']}{total['rerun']:>5}/{total['tasks']}"
      f"{total['same_metrics']:>9}/{total['tasks']}{total['same_trace']:>7}/{total['tasks']}")
print(f"\ntasks with any difference: {len(mismatch)}")
print("note: raw traces embed uuid4 intent/work ids, so 'same trace' is expected to be 0 here;")
print("      trace_field_diff.py masks those ids and reports the remaining numeric differences.")
for item in mismatch[:40]:
    print("  ", item)
