"""Prototype of a fourth structure baseline set: the 10 F1 follow simulation scenarios.  (1f0fefe)

    PYTHONPATH=. python -B follow_baseline_index.py --output <index.json>
    python -B follow_baseline_index.py --compare <a.json> <b.json>

Each scenario's formal result dict (distances, revision responses, planning submissions, state
history, applied perturbations, safety violations, terminal states, gates) is signed with the
project's own `structure_signature` (normaliser r28-structure-trajectory-v1).  Run it twice and
on two machines; the plan requires identical signatures before the set is frozen.
"""
import argparse
import json
import multiprocessing
import pathlib
import sys


def sign(name):
    from scripts.navigation_structure_baseline import NORMALIZER_VERSION, structure_signature
    from tests.sim.known_world_following import SCENARIO_BY_NAME, run_scenario
    result = run_scenario(SCENARIO_BY_NAME[name])
    return {"id": name, "passed": result["passed"], "signature": structure_signature(result),
            "normalizer_version": NORMALIZER_VERSION}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--compare", nargs=2, type=pathlib.Path)
    args = parser.parse_args()
    if args.compare:
        a, b = (json.loads(p.read_text("utf-8")) for p in args.compare)
        old = {row["id"]: row["signature"] for row in a["cases"]}
        new = {row["id"]: row["signature"] for row in b["cases"]}
        diff = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
        print(json.dumps({"cases": len(old), "differences": diff, "equivalent": not diff}))
        return int(bool(diff))
    from tests.sim.known_world_following import SCENARIOS
    with multiprocessing.Pool(4, maxtasksperchild=1) as pool:
        rows = pool.map(sign, [s.name for s in SCENARIOS], chunksize=1)
    payload = {"kind": "follow", "normalizer_version": rows[0]["normalizer_version"],
               "cases": [{"id": r["id"], "passed": r["passed"], "signature": r["signature"]} for r in rows]}
    args.output.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
    print(json.dumps({"cases": len(rows), "passed": sum(r["passed"] for r in rows)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
