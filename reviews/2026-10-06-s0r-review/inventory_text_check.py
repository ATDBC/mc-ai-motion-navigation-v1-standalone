"""How much of the S0-R v2 inventory text is per-item review, and how much is shared template?  (e108a32)

    python -B inventory_text_check.py <repo root>

Reads evidence/motion_navigation/post-f1-structure-s0r/{deletion-inventory,path-matrix}.json.
Read-only; prints repeated text, the closure/owner_review stance of each candidate, and targets
with zero calls in all five behaviour sets.
"""
import collections
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
base = root / "evidence/motion_navigation/post-f1-structure-s0r"
inventory = json.loads((base / "deletion-inventory.json").read_text("utf-8"))
matrix = json.loads((base / "path-matrix.json").read_text("utf-8"))
candidates = inventory["candidates"]

print(f"candidates: {len(candidates)}  mutation_checks_executed={inventory['mutation_checks_executed']}")
for field in ("lifecycle", "risk", "detecting_check"):
    values = collections.Counter()
    for item in candidates:
        value = (item["fact_review"]["lifecycle"] if field == "lifecycle" else
                 item["risk"] if field == "risk" else tuple(item["detecting_check"]["checks"]))
        values[str(value)] += 1
    print(f"\n{field}: {len(values)} distinct values over {len(candidates)} candidates")
    for value, count in values.most_common():
        print(f"  {count:>2} x {value[:110]}")

print("\nowner_review stance per candidate (pending S2 unless noted):")
for item in candidates:
    print(f"  {item['id']:<45} {item['closure']:<36} {item['owner_review'][:90]}")

functions = inventory["f1_function_review"]["functions"]
print(f"\nF1 function review: {len(functions)} functions")
by_text = collections.Counter((f["status"], f["review"]) for f in functions)
for (status, review), count in by_text.most_common():
    print(f"  {count:>2} x {status:<40} {review[:100]}")

calls = matrix["target_calls"]
targets = sorted(set().union(*[set(v) for v in calls.values()]))
print("\ntargets with zero calls in all five behaviour sets:")
for target in targets:
    if ":" not in target and not any(calls[s].get(target, 0) for s in calls):
        print("  " + target)
