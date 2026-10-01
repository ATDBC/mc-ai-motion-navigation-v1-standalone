"""Window-based stall measure re-computed from the archived R28-0 traces (no re-run needed).

v1 (tests/sim/product_metrics.py): a stall tick is one with movement demand and per-tick
horizontal displacement <= 0.005 block, counted in runs of >= 3 ticks.  Back-and-forth strafing
moves ~0.06 block every tick and is never counted.

v2 (proposed): a tick stalls when there is movement demand and the *net* horizontal displacement
over the last W ticks is below D blocks; runs of >= 3 such ticks are counted.  Intervals are
labelled by the active controller roles so that strict-action preparation (edge probe, verified
command submission) is reported apart from ordinary walking.

    python stall_metric_v2.py <unpacked archive> [W=10] [D=0.1]
"""
import collections
import gzip
import json
import math
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
WINDOW = int(sys.argv[2]) if len(sys.argv) > 2 else 10
DISTANCE = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
TERMINAL = {"success", "failed", "cancelled", "stopped", "interaction_required"}


def stalls(rows):
    history, current, intervals = [], None, []
    terminal = False
    for row in rows:
        terminal = terminal or row["driver_state"] in TERMINAL
        demand = row["source_bound"] and not row["goal_satisfied"] and not terminal
        history.append(row["position"])
        window = history[-(WINDOW + 1):]
        net = math.hypot(window[-1][0] - window[0][0], window[-1][2] - window[0][2])
        stalled = demand and len(window) == WINDOW + 1 and net < DISTANCE
        label = "strict_preparation" if (
            "landing_edge_probe" in row["controller_ids"]
            or row["driver_reason"] in {"submit_verified_command", "awaiting_verified_motion"}
        ) else "walking"
        if stalled:
            if current is None or current[2] != label:
                if current is not None and current[1] - current[0] + 1 >= 3:
                    intervals.append(current)
                current = [row["movement_tick"], row["movement_tick"], label]
            else:
                current[1] = row["movement_tick"]
        else:
            if current is not None and current[1] - current[0] + 1 >= 3:
                intervals.append(current)
            current = None
    if current is not None and current[1] - current[0] + 1 >= 3:
        intervals.append(current)
    return intervals


runs = [json.loads(l) for l in (root / "runs.jsonl").read_text("utf-8").splitlines()]
table = collections.defaultdict(lambda: collections.Counter())
caught = []
for r in runs:
    rows = json.load(gzip.open(root / r["trace_file"]))["trace"]
    found = stalls(rows)
    cell = table[(r["group"], r["metrics"]["success"])]
    cell["tasks"] += 1
    cell["v1_tasks_with_stall"] += bool(r["metrics"]["zero_displacement_intervals"])
    for label in ("walking", "strict_preparation"):
        ticks = sum(b - a + 1 for a, b, l in found if l == label)
        cell[f"v2_{label}_tasks"] += bool(ticks)
        cell[f"v2_{label}_ticks"] += ticks
    if r["reason"] == "fixed_route_stalled":
        caught.append((r["id"], sum(b - a + 1 for a, b, l in found if l == "walking")))

print(f"v2 parameters: window {WINDOW} ticks, net displacement < {DISTANCE} block, runs >= 3 ticks\n")
print(f"{'layer, success':<26}{'tasks':>6}{'v1 stall tasks':>15}{'v2 walking tasks/ticks':>24}{'v2 strict-prep tasks/ticks':>28}")
for key in sorted(table):
    c = table[key]
    print(f"{str(key):<26}{c['tasks']:>6}{c['v1_tasks_with_stall']:>15}"
          f"{c['v2_walking_tasks']:>12}/{c['v2_walking_ticks']:<11}{c['v2_strict_preparation_tasks']:>14}/{c['v2_strict_preparation_ticks']:<13}")
print("\nfixed_route_stalled failures and their v2 walking-stall ticks:")
for run_id, ticks in caught:
    print(f"  {run_id}: {ticks}")
