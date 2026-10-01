"""Which ticks does the v2 extractor count as 'walking' net stalls, and who holds the body then?

    python stall_attribution.py <unpacked r28-baseline-v2 baseline.tar.gz>

1. Per layer: net-stall ticks by category as stored in the archive (extractor v2).
2. For 'walking' net-stall ticks: the session's driver reason, action kind and controller list.
3. Across all traces: ticks where navigation applied non-neutral input while the diagnostic
   controller list was empty.
"""
import collections
import gzip
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
runs = [json.loads(l) for l in (root / "runs.jsonl").read_text("utf-8").splitlines()]

totals = collections.Counter()
for r in runs:
    for category, ticks in r["metrics"]["net_stall_ticks"].items():
        totals[(r["group"], category)] += ticks
print("1. net-stall ticks by layer and category (non-zero only)")
for key in sorted(totals):
    if totals[key]:
        print(f"   {key[0]:<14}{key[1]:<20}{totals[key]:>5}")

walking = collections.Counter()
unowned = collections.Counter()
unowned_tasks = collections.Counter()
for r in runs:
    rows = json.load(gzip.open(root / r["trace_file"]))["trace"]
    by_tick = {row["movement_tick"]: row for row in rows}
    for interval in r["metrics"]["net_stall_intervals"]:
        if interval["category"] != "walking":
            continue
        for tick in range(interval["start_tick"], interval["end_tick"] + 1):
            row = by_tick.get(tick)
            if row:
                walking[(r["group"], row["driver_reason"], row["action_kind"], tuple(row["controller_ids"]))] += 1
    hit = False
    for row in rows:
        movement = row["applied_movement"]
        if (isinstance(movement, dict) and (movement.get("forward") or movement.get("strafe") or movement.get("jump"))
                and not row["controller_ids"] and row["source_bound"]):
            unowned[(r["group"], row["session_state"], row["driver_reason"])] += 1
            hit = True
    unowned_tasks[r["group"]] += hit

print("\n2. 'walking' net-stall ticks: (layer, driver reason, action kind, controllers) -> ticks")
for key, value in walking.most_common():
    print(f"   {value:>5} {key}")
print("\n3. non-neutral navigation input with an empty controller list: (layer, state, reason) -> ticks")
for key, value in unowned.most_common():
    print(f"   {value:>5} {key}")
print("   tasks affected:", {k: v for k, v in unowned_tasks.items() if v})
