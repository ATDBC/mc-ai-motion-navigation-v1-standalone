"""Under constant one-tick latency, tally the input-responsibility verdicts the supervisor sees while the
probe is stopping, and the pending movement that keeps it from CLEAR."""
import runpy, sys
from collections import Counter
from mc2p.motion_nav import execution_supervisor as es

tally = Counter()
original = es.input_responsibility_status


def wrapped(ledger, anchor=None, **kwargs):
    status = original(ledger, anchor, **kwargs)
    pending = ()
    if ledger is not None:
        pending = tuple(sorted({(r.status.value, r.action.movement.sneak, r.action.movement.forward)
                                for r in ledger.snapshot() if r.status.value not in {"applied", "rejected", "superseded"}}))
    tally[(status.value, pending)] += 1
    return status


es.input_responsibility_status = wrapped
sys.argv = ["trace_fixed_latency.py", "direct_drop_2", "200"]
runpy.run_path(__file__.replace("why_not_clear.py", "trace_fixed_latency.py"), run_name="__main__")
for key, count in tally.most_common(8):
    print("responsibility", key, count)
