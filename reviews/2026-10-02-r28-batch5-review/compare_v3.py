"""Paired comparison of a v3 re-run against the archived r28-baseline-v3, with a per-case view of
the player-spot layers.

    python compare_v3.py <unpacked archived v3> <candidate run directory>
"""
import collections
import json
import pathlib
import sys

old = {json.loads(l)["id"]: json.loads(l) for l in pathlib.Path(sys.argv[1], "runs.jsonl").read_text("utf-8").splitlines()}
new = {json.loads(l)["id"]: json.loads(l) for l in pathlib.Path(sys.argv[2], "runs.jsonl").read_text("utf-8").splitlines()}
assert old.keys() == new.keys(), "paired task IDs differ"
layers = collections.defaultdict(collections.Counter)
arrival = collections.defaultdict(list)
cases = collections.Counter()
for key, a in old.items():
    b = new[key]
    c = layers[a["group"]]
    c["tasks"] += 1
    c["before"] += a["metrics"]["success"]
    c["after"] += b["metrics"]["success"]
    c["b"] += a["metrics"]["success"] and not b["metrics"]["success"]
    c["c"] += (not a["metrics"]["success"]) and b["metrics"]["success"]
    c["changed"] += a["metrics"] != b["metrics"]
    if a["metrics"]["success"] and b["metrics"]["success"]:
        arrival[a["group"]].append(b["metrics"]["arrival_ticks"] - a["metrics"]["arrival_ticks"])
    if a["group"].startswith("player"):
        cases[(a["parameters"]["case"], "before")] += a["metrics"]["success"]
        cases[(a["parameters"]["case"], "after")] += b["metrics"]["success"]
        cases[(a["parameters"]["case"], "tasks")] += 1
print(f"{'layer':<14}{'before':>10}{'after':>10}{'b':>4}{'c':>5}{'changed':>9}  arrival delta (both succeed) min/mean/max")
for g in sorted(layers):
    c, d = layers[g], arrival[g]
    span = f"{min(d)}/{sum(d) / len(d):+.2f}/{max(d)}" if d else "-"
    print(f"{g:<14}{c['before']:>6}/{c['tasks']:<3}{c['after']:>6}/{c['tasks']:<3}{c['b']:>4}{c['c']:>5}{c['changed']:>9}  {span}")
print("\nplayer-spot cases (normal + late together):")
for case in sorted({k[0] for k in cases}):
    print(f"  {case:<24}{cases[(case, 'before')]:>4} -> {cases[(case, 'after')]:<4} of {cases[(case, 'tasks')]}")
print("\nsuccess -> failure:")
for key, a in old.items():
    if a["metrics"]["success"] and not new[key]["metrics"]["success"]:
        print(f"  {key} {a['parameters']['case']} {a['parameters']['direction']} -> {new[key]['reason']}")
