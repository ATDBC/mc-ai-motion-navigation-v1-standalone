"""在最终源码上重跑的 600 项，与 D094 公开证据逐项比较。

    python <本文件> PUBLISHED_DIR RERUN_DIR

PUBLISHED_DIR：evidence/motion_navigation/post-input-loss-landing-v1/green/random100
RERUN_DIR：在 34d8ba1 上执行
    PYTHONPATH=. python scripts/action_entry_late_hardening.py --output RERUN_DIR --seeds 100 --workers 3
的输出目录。比较 index 的全部字段，以及 runs 中每项的逐 tick 运动
（movement_tick、position、velocity、on_ground、applied_movement）。
"""
import collections
import gzip
import json
import sys
from pathlib import Path

published, rerun = map(Path, sys.argv[1:3])


def index(path):
    return {(r["family"], r["seed"]): r for r in map(json.loads, open(path / "index.jsonl"))}


def motion(path):
    out = {}
    with gzip.open(path / "runs.jsonl.gz", "rt") as stream:
        for line in stream:
            row = json.loads(line)
            out[(row["family"], row["seed"])] = json.dumps(
                [(t["movement_tick"], t["position"], t["velocity"], t["on_ground"],
                  t["applied_movement"]) for t in row["trace"]])
    return out


a, b = index(published), index(rerun)
fields = sorted(set(next(iter(a.values()))) - {"id"})
diffs = collections.Counter()
for key in a:
    for field in fields:
        x, y = a[key][field], b[key][field]
        if field == "final_position":
            x, y = [round(v, 6) for v in x], [round(v, 6) for v in y]
        diffs[field] += x != y
print(f"cases: published {len(a)}, rerun {len(b)}")
# 公开文件以 CRLF 存储，按解析后的 JSON 比较。
print("inputs.json identical (parsed JSON):",
      json.loads((published / "inputs.json").read_text()) == json.loads((rerun / "inputs.json").read_text()))
print("index field differences:", {k: v for k, v in diffs.items() if v} or "none",
      f"(fields compared: {', '.join(fields)})")
ma, mb = motion(published), motion(rerun)
print(f"identical per-tick motion traces: {sum(ma[k] == mb.get(k) for k in ma)}/{len(ma)}")
for family in dict.fromkeys(k[0] for k in b):
    counts = collections.Counter(b[k]["outcome"] for k in b if k[0] == family)
    print(f"  {family:24s} {dict(counts)}")
print("rerun failures:", [(k, b[k]["reason"]) for k in b if b[k]["outcome"] != "success"])
