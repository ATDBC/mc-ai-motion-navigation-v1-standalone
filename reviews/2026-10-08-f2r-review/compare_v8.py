import json, glob, sys
from collections import Counter
def rows(p): return {r["id"]: r for r in map(json.loads, open(p))}
pub = {}
for f in glob.glob("evidence/motion_navigation/F2R-piecewise-completion-v1/final/v8/layers/*.jsonl"):
    pub.update(rows(f))
for label, paths in [(a.split("=")[0], a.split("=")[1].split(",")) for a in sys.argv[1:]]:
    mine = {}
    for p in paths: mine.update(rows(p))
    keys = [k for k in mine if k in pub]
    same = {f: sum(mine[k].get(f) == pub[k].get(f) for k in keys) for f in
            ("input_sha256", "outcome", "reason", "final_position", "trajectory_sha256", "damage")}
    ok = sum(mine[k]["success"] for k in mine)
    reasons = Counter(mine[k]["reason"] for k in mine if not mine[k]["success"])
    diff = [k for k in keys if (mine[k]["outcome"], mine[k]["reason"]) != (pub[k]["outcome"], pub[k]["reason"])]
    print(f"{label}: {len(mine)} rows ({len(keys)} matched to published); success {ok}; failures {dict(reasons)}")
    print(f"   equal to published: {same}")
    if diff: print(f"   outcome/reason changed: {len(diff)} e.g. {[ (k, pub[k]['reason'], mine[k]['reason']) for k in diff[:5]]}")
