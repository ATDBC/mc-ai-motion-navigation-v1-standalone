# Run from the repository root of a daa5cc4 checkout: PYTHONPATH=. python -B <script>
"""One omitted application receipt, one goal revision back to the start, and the two alone; plus the
input-responsibility verdict the supervisor sees in the stuck case (diagnosis wrapper, prints only)."""
from collections import Counter
from tests.sim import event_sequences as es
from mc2p.motion_nav import route_body_controller as rbc

verdicts = Counter()
original = rbc.input_responsibility_status


def wrapped(ledger, anchor=None, **kwargs):
    status = original(ledger, anchor, **kwargs)
    verdicts[status.value] += 1
    return status


rbc.input_responsibility_status = wrapped


def outcome(scenario, *events):
    seq = es.GeneratedSequence(0, scenario, 400, tuple(sorted(
        (es.GeneratedEvent(es.EventKind(k), t) for k, t in events), key=lambda e: (e.tick, e.kind.value))))
    result = es.run_sequence(seq)
    if result.exception:
        return "EXCEPTION " + result.exception
    r = result.result
    return f"{r.outcome}/{r.reason} {sorted({v[1] for v in r.violations})}"


for scenario in ("direct_drop_2", "terrace_two_ledges", "far_landing_L_walkway"):
    for label, events in (("omit@3 only", (("omit_receipt", 3),)),
                          ("goal_back@5 only", (("goal_back", 5),)),
                          ("omit@3 + goal_back@5", (("omit_receipt", 3), ("goal_back", 5))),
                          ("omit@6 + goal_back@9", (("omit_receipt", 6), ("goal_back", 9))),
                          ("omit@10 + goal_back@14", (("omit_receipt", 10), ("goal_back", 14)))):
        verdicts.clear()
        print(f"{scenario:<22} {label:<24} -> {outcome(scenario, *events)}  route-release responsibility verdicts={dict(verdicts)}",
              flush=True)
