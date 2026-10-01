"""Audit of the archived R28-0 baseline (evidence/motion_navigation/r28-baseline-v1, c9f7d41).

Unpack baseline.tar.gz into a directory and run:   python baseline_audit.py <unpacked dir>

1. Distinct task configurations per layer: the generator derives case, direction, entry speed and
   offset from the seed index with periods 3/4/3/4 (2 cases for drops), so without late-tick
   injection the 200 seeds repeat after 144 (48 for drops).  Do duplicates give identical metrics?
2. Every fixed_route_stalled failure: is it a left/right strafe oscillation with no forward input,
   and what does the shared extractor's zero-displacement (stall) metric record?
3. The same oscillation pattern (>= 3 consecutive sign flips of strafe with forward = 0 while
   executing) in successful tasks.
"""
import collections
import gzip
import json
import math
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
runs = [json.loads(line) for line in (root / "runs.jsonl").read_text("utf-8").splitlines()]


def trace(run):
    return json.load(gzip.open(root / run["trace_file"]))["trace"]


print("1. Distinct configurations per layer")
for group in sorted({r["group"] for r in runs}):
    rows = [r for r in runs if r["group"] == group]
    configs = collections.defaultdict(list)
    for r in rows:
        p = r["parameters"]
        configs[(p["case"], p["direction"], p["speed"], p["offset"], tuple(p["late_ticks"]))].append(r)
    duplicated = [v for v in configs.values() if len(v) > 1]
    differing = sum(1 for v in duplicated if len({json.dumps(r["metrics"], sort_keys=True) for r in v}) > 1)
    failures = [r for r in rows if not r["metrics"]["success"]]
    failing_configs = {k for k, v in configs.items() if not v[0]["metrics"]["success"]}
    print(f"  {group:<14} tasks={len(rows)} distinct={len(configs)} duplicated_configs={len(duplicated)} "
          f"duplicates_with_different_metrics={differing} failures={len(failures)} "
          f"distinct_failing_configs={len(failing_configs)}")


def oscillation_ticks(rows):
    previous, streak, count = None, 0, 0
    for row in rows:
        movement = row["applied_movement"]
        if not isinstance(movement, dict) or row["session_state"] != "executing":
            previous, streak = None, 0
            continue
        strafe, forward = movement.get("strafe"), movement.get("forward")
        if forward == 0 and strafe and previous and strafe == -previous:
            streak += 1
            count += streak >= 3
        else:
            streak = 0
        previous = strafe
    return count


print("\n2. fixed_route_stalled failures")
for r in runs:
    if r["reason"] != "fixed_route_stalled":
        continue
    rows = trace(r)
    executing = [row for row in rows if row["session_state"] == "executing"]
    tail = executing[-12:]
    net = math.hypot(tail[-1]["position"][0] - tail[0]["position"][0], tail[-1]["position"][2] - tail[0]["position"][2])
    travelled = sum(math.hypot(b["position"][0] - a["position"][0], b["position"][2] - a["position"][2])
                    for a, b in zip(tail, tail[1:]))
    p = r["parameters"]
    print(f"  {r['id']} {p['direction']:<5} speed={p['speed']} offset={p['offset']} oscillation_ticks={oscillation_ticks(rows)} "
          f"last12: path={travelled:.3f} net={net:.3f}  extractor zero_displacement_ticks={r['metrics']['zero_displacement_ticks']}")

print("\n3. Oscillation in all tasks: (layer, success) -> tasks with oscillation / oscillation ticks / tasks")
summary = collections.defaultdict(lambda: [0, 0, 0])
for r in runs:
    ticks = oscillation_ticks(trace(r))
    cell = summary[(r["group"], r["metrics"]["success"])]
    cell[2] += 1
    if ticks:
        cell[0] += 1
        cell[1] += ticks
for key in sorted(summary):
    print(f"  {key}: {summary[key][0]} / {summary[key][1]} / {summary[key][2]}")

print("\n4. wall_detour outcome by direction (scene is rotation-symmetric)")
for group in ("point-normal", "point-late"):
    counts = collections.Counter((r["parameters"]["direction"], r["metrics"]["success"]) for r in runs
                                 if r["group"] == group and r["parameters"]["case"] == "wall_detour")
    print(f"  {group}: " + ", ".join(f"{d}:{counts[(d, True)]}ok/{counts[(d, False)]}fail"
                                     for d in ("south", "east", "north", "west")))
