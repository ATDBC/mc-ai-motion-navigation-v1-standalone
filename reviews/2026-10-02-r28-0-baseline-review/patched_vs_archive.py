"""Paired comparison of a patched product run against the archived R28-0 baseline.

    python patched_vs_archive.py <unpacked archive> <patched run directory>

Metrics were shown to be identical across Windows/Linux for unchanged code (reproduction-c9f7d41.txt),
so per-task differences here come from the patch.  Reports per layer: success before/after,
discordant pairs (b = baseline success & patched failure, c = baseline failure & patched success),
tasks whose shared-extractor metrics changed, and the arrival-tick change on tasks that succeed in both.
"""
import collections
import json
import pathlib
import sys

old = {json.loads(l)["id"]: json.loads(l) for l in pathlib.Path(sys.argv[1], "runs.jsonl").read_text("utf-8").splitlines()}
new = {json.loads(l)["id"]: json.loads(l) for l in pathlib.Path(sys.argv[2], "runs.jsonl").read_text("utf-8").splitlines()}
assert old.keys() == new.keys(), "paired task IDs differ"
rows = collections.defaultdict(lambda: collections.Counter())
arrival = collections.defaultdict(list)
fields = collections.defaultdict(collections.Counter)
reasons = collections.defaultdict(collections.Counter)
for key, a in old.items():
    b = new[key]
    g = a["group"]
    c = rows[g]
    c["tasks"] += 1
    c["before"] += a["metrics"]["success"]
    c["after"] += b["metrics"]["success"]
    c["b"] += a["metrics"]["success"] and not b["metrics"]["success"]
    c["c"] += (not a["metrics"]["success"]) and b["metrics"]["success"]
    changed = [k for k in a["metrics"] if a["metrics"][k] != b["metrics"].get(k)]
    c["metrics_changed"] += bool(changed)
    fields[g].update(changed)
    if a["metrics"]["success"] and b["metrics"]["success"]:
        arrival[g].append(b["metrics"]["arrival_ticks"] - a["metrics"]["arrival_ticks"])
    if not b["metrics"]["success"]:
        reasons[g][b["reason"]] += 1
print(f"{'layer':<14}{'before':>8}{'after':>8}{'b':>5}{'c':>5}{'changed':>9}  arrival delta on both-success (min/mean/max)")
for g in sorted(rows):
    c = rows[g]
    d = arrival[g]
    span = f"{min(d)}/{sum(d) / len(d):+.2f}/{max(d)}" if d else "-"
    print(f"{g:<14}{c['before']:>5}/{c['tasks']:<3}{c['after']:>4}/{c['tasks']:<3}{c['b']:>4}{c['c']:>5}{c['metrics_changed']:>9}  {span}")
print("\nremaining failures after the patch:")
for g in sorted(reasons):
    print(f"  {g}: {dict(reasons[g])}")
print("\nchanged metric fields (task counts):")
for g in sorted(fields):
    if fields[g]:
        print(f"  {g}: {dict(fields[g].most_common(6))}")
