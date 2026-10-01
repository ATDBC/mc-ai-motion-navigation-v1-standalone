"""Compare a local partial re-run of the R28-0 baseline with the archive (c9f7d41).

    python scripts/navigation_coordination_metrics.py baseline \
        --manifest tests/sim/manifests/navigation-product-r28.json --output <rerun> --workers 4 --seed-count 48
    python reproduction_check.py <unpacked archive> <rerun>
"""
import collections
import gzip
import json
import pathlib
import sys

archive, rerun = (pathlib.Path(p) for p in sys.argv[1:3])
old = {json.loads(l)["id"]: json.loads(l) for l in (archive / "runs.jsonl").read_text("utf-8").splitlines()}
new = [json.loads(l) for l in (rerun / "runs.jsonl").read_text("utf-8").splitlines()]
metrics = reasons = strict_equal = strict_total = 0
strict_fields = collections.Counter()
strict_groups = collections.Counter()
for record in new:
    base = old[record["id"]]
    metrics += record["metrics"] == base["metrics"]
    reasons += record["reason"] == base["reason"]
    a = json.load(gzip.open(archive / record["trace_file"]))["strict_trace"]
    b = json.load(gzip.open(rerun / record["trace_file"]))["strict_trace"]
    if a is None:
        continue
    strict_total += 1
    if a == b:
        strict_equal += 1
        continue
    strict_groups[record["group"]] += 1
    for ra, rb in zip(a, b):
        strict_fields.update(k for k in ra if ra[k] != rb[k])
    if len(a) != len(b):
        strict_fields["length"] += 1
print(f"re-run tasks: {len(new)}; identical metrics: {metrics}; identical end reason: {reasons}")
print(f"strict traces identical: {strict_equal}/{strict_total}; differing by layer: {dict(strict_groups)}; "
      f"differing fields (tick count): {dict(strict_fields)}")
