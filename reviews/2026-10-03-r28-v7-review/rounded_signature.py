"""Would rounding make the normalised trajectory hash portable across Windows and Linux?

    PYTHONPATH=. python -B rounded_signature.py <unpacked v6 archive (drop traces)> <Linux re-run of the same groups>

Run from a checkout of 3c15744 (it imports strict_trace).  For each task it hashes
strict_trace(trace) exactly as navigation_coordination_metrics.trace_signatures does, and again
after rounding every float to 9 decimals, and reports how many hashes agree.
"""
import gzip
import hashlib
import json
import pathlib
import sys

from tests.sim.product_metrics import strict_trace

archive, rerun = (pathlib.Path(p) for p in sys.argv[1:3])


def rounded(value, digits=9):
    if isinstance(value, float):
        return round(value, digits) + 0.0          # +0.0 folds -0.0 into 0.0
    if isinstance(value, list):
        return [rounded(v, digits) for v in value]
    if isinstance(value, dict):
        return {k: rounded(v, digits) for k, v in value.items()}
    return value


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


same_exact = same_rounded = total = 0
for line in (rerun / "runs.jsonl").read_text("utf-8").splitlines():
    record = json.loads(line)
    a = json.loads((archive / record["trace_file"].removesuffix(".gz")).read_text("utf-8"))["trace"]
    b = json.load(gzip.open(rerun / record["trace_file"], "rt", encoding="utf-8"))["trace"]
    sa, sb = strict_trace(a), strict_trace(b)
    total += 1
    same_exact += digest(sa) == digest(sb)
    same_rounded += digest(rounded(sa)) == digest(rounded(sb))
print(f"tasks {total}: exact strict_trace hash equal {same_exact}, after rounding floats to 1e-9 equal {same_rounded}")
