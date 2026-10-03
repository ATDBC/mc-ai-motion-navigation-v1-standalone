"""Pair a Linux re-run of the v7 product manifest (3c15744) with the archived r28-baseline-v7 runs.jsonl.

    python compare_v7.py <evidence/motion_navigation/r28-baseline-v7/runs.jsonl> <re-run directory>

The archive keeps no frame traces in Git, only per-task results, motion-job records and two
normalised trace hashes (trajectory_sha256, inputs_sha256).  This script checks all of them.
"""
import collections
import json
import pathlib
import sys

old = {json.loads(l)["id"]: json.loads(l) for l in pathlib.Path(sys.argv[1]).read_text("utf-8").splitlines()}
new = {json.loads(l)["id"]: json.loads(l) for l in pathlib.Path(sys.argv[2], "runs.jsonl").read_text("utf-8").splitlines()}
assert old.keys() == new.keys(), (len(old), len(new))
FIELDS = ("success", "reason", "metrics", "motion_jobs", "trajectory_sha256", "inputs_sha256")


def view(record, field):
    if field == "success":
        return record["metrics"]["success"]
    if field == "motion_jobs":
        # submission/delivery polls and ticks are deterministic; nothing wall-clock is stored here
        return record.get("motion_jobs")
    return record.get(field)


table = collections.defaultdict(collections.Counter)
differ = collections.defaultdict(list)
for key in sorted(old):
    group = old[key]["group"]
    table[group]["tasks"] += 1
    table[group]["archive_ok"] += old[key]["metrics"]["success"]
    table[group]["rerun_ok"] += new[key]["metrics"]["success"]
    for field in FIELDS:
        same = view(old[key], field) == view(new[key], field)
        table[group][field] += same
        if not same:
            differ[field].append(key)
print(f"{'group':<15}{'archive':>9}{'re-run':>9}" + "".join(f"{f:>19}" for f in FIELDS[1:]))
total = collections.Counter()
for group in sorted(table):
    c = table[group]
    total.update(c)
    print(f"{group:<15}{c['archive_ok']:>5}/{c['tasks']:<3}{c['rerun_ok']:>5}/{c['tasks']:<3}"
          + "".join(f"{c[f]:>15}/{c['tasks']:<3}" for f in FIELDS[1:]))
print(f"{'total':<15}{total['archive_ok']:>5}/{total['tasks']}{total['rerun_ok']:>5}/{total['tasks']}"
      + "".join(f"{total[f]:>14}/{total['tasks']}" for f in FIELDS[1:]))
for field in FIELDS:
    if differ[field]:
        print(f"\n{field} differs in {len(differ[field])} tasks, e.g. {differ[field][:8]}")
