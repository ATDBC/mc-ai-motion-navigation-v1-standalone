"""Invariants of the recovered landing-goal cases in a 100-seed prototype run.

    python <this file> PROTO_RUNS_GZ BASELINE_RUNS_GZ

PROTO_RUNS_GZ: runs.jsonl.gz from scripts/action_entry_late_hardening.py
--seeds 100 with proto-h02-replan.diff applied.  BASELINE_RUNS_GZ: the same
script on 19231c6 for the two landing families.  For every case that failed
on 19231c6 and succeeds on the prototype it checks:
  * the trace before the frame where 19231c6 marks the task failed is
    identical (same movement, position, velocity, ground flag, action kind
    and input window), so the change only acts after the safe landing;
  * after that point no jump input is applied and the body never leaves
    the ground (no second takeoff);
  * the body stays on the landing surface height (y = 65).
"""
import gzip
import json
import sys


def load(path):
    return {(r["family"], r["seed"]): r for r in map(json.loads, gzip.open(path, "rt"))}


def motion(frame):
    return json.loads(json.dumps((frame["movement_tick"], frame["position"], frame["velocity"],
                                  frame["on_ground"], frame["applied_movement"],
                                  frame["action_kind"], frame["input_window"])))


proto, base = load(sys.argv[1]), load(sys.argv[2])
checked = prefix_ok = no_jump = grounded = level = 0
kinds, states = set(), set()
for key, b in base.items():
    p = proto.get(key)
    if p is None or b["outcome"] == "success" or p["outcome"] != "success":
        continue
    checked += 1
    n = next(i for i, f in enumerate(b["trace"]) if f["session_state"] == "failed")
    prefix_ok += [motion(f) for f in p["trace"][:n]] == [motion(f) for f in b["trace"][:n]]
    tail = p["trace"][n:]
    kinds.update(f["action_kind"] for f in tail)
    states.update(f["session_state"] for f in tail)
    no_jump += not any(f["applied_movement"] and f["applied_movement"].get("jump") for f in tail)
    grounded += all(f["on_ground"] for f in tail)
    level += all(abs(f["position"][1] - 65.0) < 1e-6 for f in tail)
print(f"recovered cases checked: {checked}")
print(f"identical prefix before the baseline's failure frame: {prefix_ok}/{checked}")
print(f"no jump input after that point: {no_jump}/{checked}")
print(f"always on ground after that point: {grounded}/{checked}")
print(f"always at y=65 after that point: {level}/{checked}")
print(f"action kinds after that point: {sorted(k for k in kinds if k)}")
print(f"session states after that point: {sorted(states)}")
