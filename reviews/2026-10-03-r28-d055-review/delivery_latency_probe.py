"""Does the product simulator see background-delivery latency?  (7260f42)

The 2,000-task product run uses InlineMotionWorker: every motion job submitted on one tick is
returned on the next poll.  On Fabric the same job goes through a real worker process (warm
round trip P95 34 ms, cold start 326 ms, i.e. about 1 and 7 ticks).  This probe re-runs product
scenarios unchanged except for the motion worker, which now returns each job a fixed number of
extra polls later (FIFO, no wall clock, fully deterministic).

    PYTHONPATH=. python -B delivery_latency_probe.py [--groups drop-normal,height-normal] [--seeds 40]

Run it from a checkout of 7260f42, or of 7260f42 with one of the proto-*.diff files applied.

Modes:
  inline    the product default (result on the next poll)
  +k        every job k extra polls later
  cold+6    only the first job of the run is 6 polls late (a cold worker), the rest inline
"""
from __future__ import annotations

import argparse
import collections
import json
import multiprocessing
import pathlib
import statistics

from mc2p.motion_nav import motion_worker
from tests.sim.product_cases import product_scenario
from tests.sim.product_metrics import extract_metrics
from tests.sim.runner import InlineMotionWorker, ObservedAsyncActivity, run

MANIFEST = pathlib.Path("tests/sim/manifests/navigation-product-r28-v4.json")
MODES = ("inline", "+1", "+2", "+3", "+6", "cold+6")


def delayed_worker(mode: str):
    class DelayedMotionWorker(InlineMotionWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._poll = 0
            self._submitted = 0
            self._queue = []                      # (ready_poll, job), FIFO like the real worker

        def submit(self, job) -> bool:
            if mode == "cold+6":
                extra = 6 if self._submitted == 0 else 0
            else:
                extra = int(mode[1:])
            self._submitted += 1
            self._queue.append((self._poll + 1 + extra, job))
            self.activity.append(ObservedAsyncActivity(job.work_identity, "submit"))
            return True

        def poll_available(self):
            self._poll += 1
            done = []
            while self._queue and self._queue[0][0] <= self._poll:
                _, job = self._queue.pop(0)
                done.append(motion_worker._execute_job(job))
                self.activity.append(ObservedAsyncActivity(job.work_identity, "poll"))
            return tuple(done)

        def close(self) -> None:
            self._queue = []

    return InlineMotionWorker if mode == "inline" else DelayedMotionWorker


def run_one(job):
    manifest, group, seed, mode = job
    scenario, parameters = product_scenario(manifest, group, seed)
    result = run(scenario, motion_factory=delayed_worker(mode))
    metrics = extract_metrics(result.trace, start_tick=1, start_position=scenario.start,
                              outcome=result.outcome, violations=result.violations)
    jobs = {json.dumps(event["identity"], sort_keys=True)
            for row in result.trace for event in (row.get("async_events") or ())
            if (event.get("identity") or {}).get("work_kind") == "motion_solve"}
    return {"group": group["id"], "seed": seed, "mode": mode, "case": parameters["case"],
            "success": metrics["success"], "reason": result.reason, "motion_jobs": len(jobs),
            "violations": len(result.violations), "arrival": metrics.get("arrival_ticks")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", default="drop-normal,height-normal")
    parser.add_argument("--seeds", type=int, default=40)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--jsonl")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    groups = [g for g in manifest["groups"] if g["id"] in args.groups.split(",")]
    jobs = [(manifest, g, seed, mode) for g in groups
            for seed in range(manifest["seed_start"], manifest["seed_start"] + args.seeds) for mode in MODES]
    with multiprocessing.Pool(args.workers) as pool:
        rows = pool.map(run_one, jobs, chunksize=1)
    if args.jsonl:
        pathlib.Path(args.jsonl).write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    table = collections.defaultdict(collections.Counter)
    arrivals = collections.defaultdict(list)
    reasons = collections.defaultdict(collections.Counter)
    for r in rows:
        key = (r["group"], r["case"], r["mode"])
        table[key]["n"] += 1
        table[key]["ok"] += r["success"]
        table[key]["violations"] += r["violations"]
        table[key]["jobs"] += r["motion_jobs"]
        if r["success"] and r["arrival"] is not None:
            arrivals[key].append(r["arrival"])
        if not r["success"]:
            reasons[key][r["reason"]] += 1
    print(f"product scenarios, first {args.seeds} seeds per group, only the motion worker's delivery changed\n")
    print(f"{'group':<15}{'case':<12}" + "".join(f"{m:>10}" for m in MODES))
    for group in groups:
        for case in group["cases"]:
            cells = []
            for mode in MODES:
                c = table[(group["id"], case, mode)]
                cells.append(f"{c['ok']}/{c['n']}" + ("!" if c["violations"] else ""))
            print(f"{group['id']:<15}{case:<12}" + "".join(f"{c:>10}" for c in cells))
    print("\nmean background motion jobs per task:")
    print(f"{'group':<15}{'case':<12}" + "".join(f"{m:>10}" for m in MODES))
    for group in groups:
        for case in group["cases"]:
            cells = [f"{table[(group['id'], case, m)]['jobs'] / max(1, table[(group['id'], case, m)]['n']):.2f}"
                     for m in MODES]
            print(f"{group['id']:<15}{case:<12}" + "".join(f"{c:>10}" for c in cells))
    print("\nmedian arrival ticks of completed tasks:")
    print(f"{'group':<15}{'case':<12}" + "".join(f"{m:>10}" for m in MODES))
    for group in groups:
        for case in group["cases"]:
            cells = [(f"{statistics.median(arrivals[(group['id'], case, m)]):.0f}"
                      if arrivals[(group['id'], case, m)] else "-") for m in MODES]
            print(f"{group['id']:<15}{case:<12}" + "".join(f"{c:>10}" for c in cells))
    print("\nfailure reasons (group, case, mode):")
    for key in sorted(reasons):
        print(f"  {key}: {dict(reasons[key])}")
    print("\n'!' marks a run with safety-monitor violations.")


if __name__ == "__main__":
    main()
