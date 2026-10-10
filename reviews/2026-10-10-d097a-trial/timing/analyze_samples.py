"""审查方补充分析：按模板拆分 T3（或 T2）的决定延迟，并检查单步耗时是否随运行时间漂移。

不改变任何门槛判定；门槛判定只看 perf_record 输出的 gate_view。
运行：python analyze_samples.py T3_performance_record_samples_ACD.json.gz
"""
import collections
import gzip
import json
import math
import sys

path = sys.argv[1]
opener = gzip.open if path.endswith(".gz") else open
with opener(path, "rt", encoding="utf-8") as handle:
    rows = json.load(handle)["rows"]          # columns: pass, config, cand, kind, steps, ms


def pct(values, q):
    values = sorted(values)
    return round(values[max(0, math.ceil(q * len(values)) - 1)], 3) if values else None


def group(cand):
    head = cand.split(":")[0]
    if head.isdigit():                        # set D: "<row>:<template>/<speed>/<n>/<offset>"
        return "D-" + cand.split(":")[1].split("/")[0]
    return "C" if "/" in cand else "A"


groups = collections.defaultdict(list)
for row in rows:
    if row[3] in ("tick", "commit"):
        groups[(row[1], row[3], group(row[2]))].append((row[4], row[5]))
print("config kind group: n, ms P50/P95, steps P50/P95, median us/step (nonzero-step decisions)")
for key in sorted(groups):
    values = groups[key]
    per_step = [ms / steps * 1000 for steps, ms in values if steps > 0]
    print(*key, len(values), pct([v[1] for v in values], .5), pct([v[1] for v in values], .95),
          pct([v[0] for v in values], .5), pct([v[0] for v in values], .95), pct(per_step, .5))
for config in ("strict", "strict_runahead"):
    ticks = [(row[4], row[5]) for row in rows if row[1] == config and row[3] == "tick" and row[4] > 0]
    n = len(ticks)
    print(config, "tick median us/step by tenth of the run:",
          [pct([ms / steps * 1000 for steps, ms in ticks[k * n // 10:(k + 1) * n // 10]], .5) for k in range(10)])
f1 = [row for row in rows if row[1] == "strict_runahead" and row[3] == "tick"]
print("F1 tick decisions with zero physics steps:", round(sum(r[4] == 0 for r in f1) / len(f1), 4),
      "| P95 over the non-zero ones:", pct([r[5] for r in f1 if r[4] > 0], .95), "ms")
slow = [row for row in rows if row[3] in ("tick", "commit") and row[5] > 100]
print("decisions over 100 ms:", len(slow), dict(collections.Counter((r[1], r[3]) for r in slow)),
      "max", max((r[5] for r in slow), default=None))
