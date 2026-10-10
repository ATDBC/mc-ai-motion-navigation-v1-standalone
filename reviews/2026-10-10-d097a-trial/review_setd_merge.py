"""审查方用：把 setd_shards/ 的三片 G8 集合 D 结果合并成 m2/equivalence_setd.json，供 `run_m2.py gates` 读取。

只做加总，不重算任何比较；逐片原始文件保留在 setd_shards/。
运行：python review_setd_merge.py
"""
import json
from pathlib import Path

HERE = Path(__file__).parent


def add(total, part):
    for key, value in part.items():
        if isinstance(value, dict):
            add(total.setdefault(key, {}), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        else:
            raise ValueError(f"cannot merge {key}={value!r}")


shards = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(HERE.glob("setd_shards/setd_shard_*_of_3.json"))]
assert [s["shard"] for s in shards] == [0, 1, 2] and all(s["shards"] == 3 for s in shards)
summary, mismatches = {}, []
for shard in shards:
    add(summary, shard["summary"])
    mismatches += shard["mismatches"]
payload = {"available": True, "path": "../m1/accepted_candidates.jsonl",
           "merged_by_reviewer_from": [f"setd_shards/setd_shard_{s['shard']}_of_3.json" for s in shards],
           "shard_seconds": [s["seconds"] for s in shards],
           "platform": [s["platform"] for s in shards], "summary": summary, "mismatches": mismatches}
(HERE / "m2" / "equivalence_setd.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
print(json.dumps(summary, indent=1))
