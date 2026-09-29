# Run from the repository root of a daa5cc4 checkout: PYTHONPATH=. python -B <script> [period]
"""Diagnosis only (reads RetryLedger internals): active wait tokens during goal flapping.  Interrupted edge
probes never end their acquisition wait, so the fixed 8-token wait ledger fills and the task fails with
acquisition_wait_capacity_exhausted although both goals stay reachable."""
import sys
from mc2p.motion_nav import retry_ledger as rl

original_begin = rl.RetryLedger.begin_wait
original_end = rl.RetryLedger.end_wait
log = []


def begin(self, wait_id, policy, movement_tick, monotonic_ns):
    new = wait_id not in self._waits
    try:
        return original_begin(self, wait_id, policy, movement_tick, monotonic_ns)
    finally:
        if new:
            log.append((movement_tick, "begin", wait_id[:40], len(self._waits)))


def end(self, wait_id):
    had = wait_id in self._waits
    original_end(self, wait_id)
    if had:
        log.append((None, "end", wait_id[:40], len(self._waits)))


rl.RetryLedger.begin_wait = begin
rl.RetryLedger.end_wait = end
import runpy
period = sys.argv[1] if len(sys.argv) > 1 else "5"
sys.argv = ["goal_flapping.py", period]
import os
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "2026-09-29-review20-remediation-review-repro", "goal_flapping.py"),
               run_name="__main__")
for item in log:
    print("wait", item)
