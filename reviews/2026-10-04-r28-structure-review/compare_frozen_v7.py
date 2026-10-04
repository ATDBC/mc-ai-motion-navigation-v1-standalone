"""Compare a re-run of v7 on b1d43c2 with the frozen r28-baseline-v7 (published at 3c15744).

    python compare_frozen_v7.py <frozen runs.jsonl> <re-run directory>

R28-4 and R28-3 publish no runs in Git; their acceptance text says the current code gives
1,710/2,000 with five late-drop improvements, no success->failure, and extra planning
submissions in target-late.  This lists every task whose outcome, reason, motion jobs,
input hash or planning submissions differ, grouped by layer.
"""
import collections
import json
import pathlib
import sys

old = {json.loads(l)["id"]: json.loads(l) for l in pathlib.Path(sys.argv[1]).read_text("utf-8").splitlines()}
new = {json.loads(l)["id"]: json.loads(l) for l in pathlib.Path(sys.argv[2], "runs.jsonl").read_text("utf-8").splitlines()}
assert old.keys() == new.keys()
print(f"frozen v7 completed {sum(r['metrics']['success'] for r in old.values())}/2000; "
      f"b1d43c2 re-run completed {sum(r['metrics']['success'] for r in new.values())}/2000\n")
diff = collections.defaultdict(collections.Counter)
examples = collections.defaultdict(list)
for key in sorted(old):
    a, b = old[key], new[key]
    g = a["group"]
    flags = []
    if a["metrics"]["success"] != b["metrics"]["success"]:
        flags.append("failure->success" if b["metrics"]["success"] else "success->failure")
    if a["reason"] != b["reason"]:
        flags.append("reason")
    if a.get("motion_jobs") != b.get("motion_jobs"):
        flags.append("motion_jobs")
    if a.get("inputs_sha256") != b.get("inputs_sha256"):
        flags.append("inputs")
    if a["metrics"].get("planning_requests") != b["metrics"].get("planning_requests"):
        flags.append("planning_requests")
    if a["metrics"].get("arrival_ticks") != b["metrics"].get("arrival_ticks"):
        flags.append("arrival")
    for f in flags:
        diff[g][f] += 1
    if flags:
        diff[g]["any"] += 1
        examples[g].append((key, flags))
for g in sorted(diff):
    print(f"{g:<14} tasks with any difference {diff[g]['any']:>3}: " + ", ".join(f"{k} {v}" for k, v in diff[g].items() if k != "any"))
for g in sorted(examples):
    shown = [e for e in examples[g] if any(f.endswith("success") or f.endswith("failure") for f in e[1])]
    if shown:
        print(f"  {g} outcome changes: {shown}")
