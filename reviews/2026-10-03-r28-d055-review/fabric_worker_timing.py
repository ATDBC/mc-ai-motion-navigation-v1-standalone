"""Worker timing per strict-action job in the archived D055 Fabric batches (7260f42 evidence).

    python fabric_worker_timing.py <evidence/motion_navigation/r28-baseline-v6/fabric>

For every submitted job: scenario, operation, preparation prefix length (ticks), source tick,
proved start window, result status, worker compute time, and submit->result wall time in ms
(the archive's own client clock).  At 20 ticks/s one tick is 50 ms.
"""
import collections
import gzip
import json
import pathlib
import statistics
import sys

root = pathlib.Path(sys.argv[1])
for batch in sorted(p.name for p in root.iterdir() if p.is_dir()):
    path = root / batch / "motion-start-timing.jsonl.gz"
    if not path.exists():
        continue
    rows = [json.loads(line) for line in gzip.open(path)]
    pending, jobs = {}, collections.defaultdict(list)
    for r in rows:
        key = json.dumps(r["identity"], sort_keys=True)
        if r["event"] == "submit":
            pending[key] = r
        elif r["event"] == "result" and key in pending:
            s = pending.pop(key)
            jobs[r["trial"]].append(dict(
                op=s["operation"], prefix=len(s["prefix"]), source=s["source_tick"],
                window=(s["window"]["earliest_start_tick"], s["window"]["latest_start_tick"]),
                status=r["status"], compute_ms=r["compute_ns"] / 1e6,
                round_trip_ms=(r["at_ns"] - s["at_ns"]) / 1e6))
    print(f"== {batch}: {len(jobs)} trials")
    per_kind = collections.defaultdict(list)
    for trial, items in jobs.items():
        kind = trial.removeprefix("start-delivery-").rsplit("-", 2)[0]
        per_kind[kind].extend(i["compute_ms"] for i in items if i["op"] == "solve" and i["status"] == "solved")
    shapes = collections.defaultdict(collections.Counter)
    for trial, items in jobs.items():
        kind = trial.removeprefix("start-delivery-").rsplit("-", 2)[0]
        shapes[kind][" -> ".join(i["op"] for i in items)] += 1
    for kind, values in sorted(per_kind.items()):
        if values:
            print(f"   {kind:<16} solved 'solve' jobs: {len(values):>3}, compute median {statistics.median(values):7.1f} ms, max {max(values):7.1f} ms;"
                  f" job sequences per trial: {dict(shapes[kind])}")
    for trial, items in jobs.items():
        if batch.startswith("failed") or "drop" in trial:
            print(f"   {trial}")
            for i in items:
                print(f"      {i['op']:<10} prefix={i['prefix']} source={i['source']} window={i['window']} -> {i['status']:<12}"
                      f" compute {i['compute_ms']:7.2f} ms, round trip {i['round_trip_ms']:6.1f} ms")
