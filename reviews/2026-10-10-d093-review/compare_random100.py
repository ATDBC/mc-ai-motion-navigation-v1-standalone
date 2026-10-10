"""Per-seed comparison of a 100-seed run against the published D093 evidence.

    python <this file> PUBLISHED_INDEX PROTO_INDEX LINUX_BASELINE_INDEX

PUBLISHED_INDEX: evidence/motion_navigation/action-entry-late-hardening-v1/
round2-random100/index.jsonl at 19231c6.  PROTO_INDEX: index.jsonl written
by scripts/action_entry_late_hardening.py --seeds 100 on a checkout with
proto-h02-replan.diff applied.  LINUX_BASELINE_INDEX: the same script on
19231c6 for the two landing families only.
"""
import collections
import json
import statistics
import sys


def load(path):
    return {(r["family"], r["seed"]): r for r in map(json.loads, open(path))}


base, proto, linux = (load(p) for p in sys.argv[1:4])
same = sum(1 for k, v in linux.items()
           if (base[k]["outcome"], base[k]["reason"], base[k]["ticks"])
           == (v["outcome"], v["reason"], v["ticks"]))
print("Linux 19231c6 vs published evidence, landing families: identical "
      f"outcome/reason/ticks {same}/{len(linux)}")
print("old successes regressed:",
      [k for k in base if base[k]["outcome"] == "success" and proto[k]["outcome"] != "success"])
for family in dict.fromkeys(k[0] for k in base):
    pairs = collections.Counter((base[k]["outcome"], proto[k]["outcome"])
                                for k in base if k[0] == family)
    damage = sum(proto[k]["damage"] for k in proto if k[0] == family)
    violations = sum(len(proto[k]["violations"]) for k in proto if k[0] == family)
    print(f"{family:24s} (published -> proto) {dict(pairs)} damage={damage} violations={violations}")
print("proto failures:", [(k, proto[k]["reason"], base[k]["reason"])
                          for k in proto if proto[k]["outcome"] != "success"])
for family in ("column_outer_turn", "column_outer_aligned", "turn_jumpup", "turn_drop"):
    changed = sum(1 for k in base if k[0] == family and (
        base[k]["ticks"] != proto[k]["ticks"]
        or [round(v, 6) for v in base[k]["final_position"]]
        != [round(v, 6) for v in proto[k]["final_position"]]))
    print(f"{family:24s} cases whose ticks or final position changed: {changed}")
for family in ("column_landing_turn", "column_landing_aligned"):
    kept = [k for k in base if k[0] == family
            and base[k]["outcome"] == proto[k]["outcome"] == "success"]
    recovered = [proto[k]["ticks"] for k in proto if k[0] == family
                 and base[k]["outcome"] != "success" and proto[k]["outcome"] == "success"]
    print(f"{family:24s} kept successes with identical ticks "
          f"{sum(base[k]['ticks'] == proto[k]['ticks'] for k in kept)}/{len(kept)}; "
          f"recovered {len(recovered)} cases, ticks median {statistics.median(recovered)} "
          f"max {max(recovered)}; kept-success ticks median "
          f"{statistics.median(base[k]['ticks'] for k in kept)}")
