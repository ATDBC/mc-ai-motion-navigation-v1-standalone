"""How many background motion jobs does each simulated drop need before it starts?  (v6 archive)

    python sim_motion_jobs.py <unpacked v6 baseline>

Counts distinct motion_solve work identities in each drop task's trace.  On Fabric every
drop in the final D055 batch needed two (a solve whose result arrived without start slack,
then a revalidation); see fabric_worker_timing-7260f42.txt.
"""
import collections
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
histogram = collections.defaultdict(collections.Counter)
for line in (root / "runs.jsonl").read_text("utf-8").splitlines():
    record = json.loads(line)
    if not record["group"].startswith("drop"):
        continue
    trace = json.loads((root / record["trace_file"].removesuffix(".gz")).read_text("utf-8"))["trace"]
    jobs = set()
    for row in trace:
        for event in row.get("async_events") or ():
            identity = event.get("identity") or {}
            if identity.get("work_kind") == "motion_solve":
                jobs.add(json.dumps(identity, sort_keys=True))
    histogram[record["group"]][len(jobs)] += 1
for group, counts in sorted(histogram.items()):
    print(f"{group:<12} motion jobs per task: " + ", ".join(f"{k} job(s): {v}" for k, v in sorted(counts.items())))
