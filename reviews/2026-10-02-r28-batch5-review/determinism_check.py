"""Does the same code with the same inputs give the same robot behaviour on another machine?  (7348b64)

The terminal-approach screening stops at a 50 ms wall-clock deadline.  When it finishes it returns
FEASIBLE and a screened target; when it runs out of time the route falls back to the conventional
target.  This script pairs a re-run of unchanged 7348b64 with the archived v3 and, for every task
whose metrics differ, prints the screening outcome on both machines.

    python determinism_check.py <unpacked archived v3> <re-run directory>
"""
import gzip
import json
import pathlib
import sys

archive, rerun = (pathlib.Path(p) for p in sys.argv[1:3])


def screening(root, run):
    path = root / run["trace_file"]
    if not path.exists():
        path = root / run["trace_file"].removesuffix(".gz")
    data = json.load(gzip.open(path)) if path.suffix == ".gz" else json.loads(path.read_text("utf-8"))
    out, seen = [], set()
    for row in data["trace"]:
        value = row.get("terminal_screening")
        if not value:
            continue
        for group in (value if isinstance(value, list) else [value]):
            for item in (group if isinstance(group, list) else [group]):
                key = json.dumps(item, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    out.append(f"{item.get('status')}@{item.get('elapsed_ns', 0) / 1e6:.1f}ms")
    return out


old = {json.loads(l)["id"]: json.loads(l) for l in (archive / "runs.jsonl").read_text("utf-8").splitlines()}
new = [json.loads(l) for l in (rerun / "runs.jsonl").read_text("utf-8").splitlines()]
differing = [r for r in new if r["metrics"] != old[r["id"]]["metrics"]]
print(f"re-run tasks {len(new)}; identical metrics {len(new) - len(differing)}; differing {len(differing)}; "
      f"success archive {sum(old[r['id']]['metrics']['success'] for r in new)} re-run {sum(r['metrics']['success'] for r in new)}")
by_layer = {}
for r in differing:
    by_layer[r["group"]] = by_layer.get(r["group"], 0) + 1
print("differing by layer:", by_layer)
for r in differing:
    changed = [k for k in r["metrics"] if r["metrics"][k] != old[r["id"]]["metrics"].get(k)]
    print(f"  {r['id']:<22} {r['parameters']['case']:<24} archive {screening(archive, old[r['id']])} "
          f"re-run {screening(rerun, r)} changed {changed[:3]}")
