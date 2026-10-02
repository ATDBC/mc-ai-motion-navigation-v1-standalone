"""What did the terminal-approach screening actually decide in the v3 player-spot tasks?  (7348b64)

    python screening_audit.py <unpacked r28-baseline-v3/baseline.tar.gz> <unpacked player-before.tar.gz>

1. Per player case and outcome: the screening statuses recorded in the trace.
2. Failure timing before and after batch 5: end tick, and whether the robot moved before failing
   (an early, typed rejection would end before any movement).
3. Screening budget use over all 2,000 tasks.
"""
import collections
import json
import pathlib
import statistics
import sys

new_root, before_root = (pathlib.Path(p) for p in sys.argv[1:3])
runs = {json.loads(l)["id"]: json.loads(l) for l in (new_root / "runs.jsonl").read_text("utf-8").splitlines()}
before = {json.loads(l)["id"]: json.loads(l) for l in (before_root / "runs.jsonl").read_text("utf-8").splitlines()}


def trace(root, run):
    path = root / run["trace_file"]
    if not path.exists():
        path = root / run["trace_file"].removesuffix(".gz")
    if path.suffix == ".gz":
        import gzip
        return json.load(gzip.open(path))["trace"]
    return json.loads(path.read_text("utf-8"))["trace"]


def screenings(rows):
    """Distinct screening results; the trace repeats the latest result on every row."""
    found, seen = [], set()
    for row in rows:
        value = row.get("terminal_screening")
        if value:
            for group in (value if isinstance(value, list) else [value]):
                for item in (group if isinstance(group, list) else [group]):
                    key = json.dumps(item, sort_keys=True)
                    if key not in seen:
                        seen.add(key)
                        found.append(item)
    return found


print("1. Screening status by player case (case, task success, screening statuses) -> tasks")
table = collections.Counter()
elapsed, ticks, candidates = [], [], []
all_status = collections.Counter()
for run_id, run in runs.items():
    items = screenings(trace(new_root, run))
    for item in items:
        all_status[item.get("status")] += 1
        if item.get("elapsed_ns") is not None:
            elapsed.append(item["elapsed_ns"] / 1e6)
        if item.get("status") == "budget_exhausted":
            ticks.append(item.get("rollout_ticks"))
            candidates.append(item.get("candidate_count"))
    if run["group"].startswith("player"):
        table[(run["parameters"]["case"], run["metrics"]["success"],
               tuple(sorted({item.get("status") for item in items})))] += 1
for key in sorted(table, key=str):
    print(f"   {table[key]:>4}  {key}")

print("\n2. Failure timing, before vs after batch 5 (median end tick; tasks that moved before ending)")
rows = collections.defaultdict(list)
for run_id, old in before.items():
    new = runs[run_id]
    rows[(new["parameters"]["case"], old["reason"], new["reason"])].append(
        (old["metrics"]["terminal_ticks"], new["metrics"]["terminal_ticks"],
         old["metrics"].get("first_movement_ticks") is not None, new["metrics"].get("first_movement_ticks") is not None))
for key, values in sorted(rows.items()):
    if key[2] == "goal_state_satisfied" and key[1] == "goal_state_satisfied":
        continue
    old_end = statistics.median(v[0] for v in values if v[0] is not None)
    new_end = statistics.median(v[1] for v in values if v[1] is not None)
    print(f"   {key[0]:<24} {key[1]:<36} -> {key[2]:<38} n={len(values):<3} end {old_end:>5} -> {new_end:<5} "
          f"moved {sum(v[2] for v in values)} -> {sum(v[3] for v in values)}")

print("\n3. Distinct screening results over all 2,000 tasks:", dict(all_status))
if elapsed:
    ordered = sorted(elapsed)
    pick = lambda p: ordered[min(len(ordered) - 1, int(p * len(ordered) + 0.999999) - 1)]
    print(f"   elapsed ms: n={len(ordered)} P50={pick(.5):.1f} P95={pick(.95):.1f} max={ordered[-1]:.1f}")
if ticks:
    print(f"   budget_exhausted: rollout ticks reached median {statistics.median(ticks)}, "
          f"candidates checked {dict(collections.Counter(candidates))}")
