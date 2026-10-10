"""Log every same-support local-direct admission during one D093 case.

    PYTHONPATH=. python <this file> FAMILY SEED     (from a checkout root)

FAMILY and SEED follow scripts/action_entry_late_hardening.py.  Prints the
body position, admission status/reason and the local-direct evidence, then
the case outcome.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import mc2p.motion_nav.route_admission as ra
orig = ra.RouteAdmitter.admit_local_direct
def wrapped(self, request, frame, **kw):
    r = orig(self, request, frame, **kw)
    ev = r.local_direct_evidence
    print("admit_local_direct", tuple(round(v, 3) for v in frame.body.position),
          r.status, r.reason, None if ev is None else {k: getattr(ev, k) for k in getattr(ev, '__slots__', ())} if hasattr(ev,'__slots__') else ev)
    return r
ra.RouteAdmitter.admit_local_direct = wrapped
sys.path.insert(0, str(Path.cwd() / "scripts"))
import action_entry_late_hardening as h
row = h.run_case(sys.argv[1], int(sys.argv[2]))
print(row["outcome"], row["reason"], row["final_position"])
