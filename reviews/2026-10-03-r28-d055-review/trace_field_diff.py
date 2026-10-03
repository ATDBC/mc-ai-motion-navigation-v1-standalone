"""Which trace fields differ between the archived v6 run (Windows) and a Linux re-run, and by how much?

    python trace_field_diff.py <unpacked v6 baseline> <re-run directory>

Random identifiers (uuid4 inside intent and work ids) are masked first.  For numeric fields the
largest absolute difference is reported (yaw modulo a full turn), so ULP-level libm differences can be told apart from
behaviour changes.
"""
import collections
import gzip
import math
import json
import pathlib
import re
import sys

archive, rerun = (pathlib.Path(p) for p in sys.argv[1:3])
HEX = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def masked(value):
    if isinstance(value, str):
        return HEX.sub("<id>", value)
    if isinstance(value, list):
        return [masked(v) for v in value]
    if isinstance(value, dict):
        return {k: masked(v) for k, v in value.items()}
    return value


def numeric_gap(a, b):
    """Largest absolute difference if a and b have the same shape and differ only in numbers."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b)
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        gaps = [numeric_gap(x, y) for x, y in zip(a, b)]
        return None if None in gaps else max(gaps, default=0.0)
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        gaps = [numeric_gap(a[k], b[k]) for k in a]
        return None if None in gaps else max(gaps, default=0.0)
    return 0.0 if a == b else None


runs = [json.loads(l) for l in (archive / "runs.jsonl").read_text("utf-8").splitlines()]
fields = collections.Counter()
largest = collections.defaultdict(float)
structural = collections.Counter()
identical_after_mask = 0
rows_total = 0
for record in runs:
    a = json.loads((archive / record["trace_file"].removesuffix(".gz")).read_text("utf-8"))
    b = json.load(gzip.open(rerun / record["trace_file"], "rt", encoding="utf-8"))
    ta, tb = masked(a["trace"]), masked(b["trace"])
    if ta == tb and masked(a["strict_trace"]) == masked(b["strict_trace"]):
        identical_after_mask += 1
        continue
    if len(ta) != len(tb):
        structural["trace_length"] += 1
    for x, y in zip(ta, tb):
        rows_total += 1
        for key in set(x) | set(y):
            if x.get(key) == y.get(key):
                continue
            fields[key] += 1
            gap = numeric_gap(x.get(key), y.get(key))
            if key == "yaw_radians" and gap is not None:
                # The same heading can be stored as -pi or +pi: compare modulo a full turn.
                gap = abs(math.remainder(x[key] - y[key], 2 * math.pi))
            if gap is None:
                structural[key] += 1
            else:
                largest[key] = max(largest[key], gap)
print(f"tasks: {len(runs)}; identical after masking random ids: {identical_after_mask}")
print(f"tasks whose trace length differs: {structural['trace_length']}")
print("\nfield                          rows differing   non-numeric differences   largest numeric gap")
for key, count in fields.most_common():
    print(f"{key:<31}{count:>14}{structural[key]:>26}{largest[key]:>22.3e}")
