# Run from the repository root of a daa5cc4 checkout: PYTHONPATH=. python -B <script> <scenario> <kind@tick> ...
"""Replay one shrunk event sequence through the repository's own event_sequences.run_sequence and print a trace."""
import sys
from tests.sim import event_sequences as es

scenario = sys.argv[1]
events = []
for item in sys.argv[2:]:
    kind, tick = item.split("@")
    events.append(es.GeneratedEvent(es.EventKind(kind), int(tick)))
events.sort(key=lambda e: (e.tick, e.kind.value))
outcome = es.run_sequence(es.GeneratedSequence(0, scenario, 400, tuple(events)))
if outcome.exception:
    print("EXCEPTION", outcome.exception)
    raise SystemExit
r = outcome.result
print(r.outcome, r.reason, r.ticks, r.final_position, "damage", r.damage, [(t, c, d) for t, c, d in r.violations])
last = None
for row in r.trace:
    key = (row["session_state"], row["session_reason"], row["controller_ids"], row["on_ground"], row["driver_state"],
           row["source_bound"])
    if key != last:
        print(row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"], row["session_reason"],
              row["controller_ids"], "ground" if row["on_ground"] else "AIR", "driver", row["driver_state"],
              "bound" if row["source_bound"] else "unbound",
              "v=%.2f" % (20 * (row["velocity"][0] ** 2 + row["velocity"][2] ** 2) ** .5))
        last = key
