# Run from the repository root of a daa5cc4 checkout: PYTHONPATH=. python -B <script> <first_seed> <last_seed>
"""The repository's own event-sequence generator at a larger budget, plus a variant that samples event kinds
WITH replacement (so the same interruption can repeat, e.g. frequent goal revisions) and more scenarios.
Every failing sequence is shrunk with the repository's shrink_sequence."""
import sys
from collections import Counter
from dataclasses import replace
import random
from tests.sim import event_sequences as es

first, last = int(sys.argv[1]), int(sys.argv[2])
scenarios = ("direct_drop_2", "direct_drop_5_budget_2", "far_landing_L_walkway", "terrace_two_ledges")


def with_replacement(seed, scenario, count):
    rng = random.Random(seed * 7919 + count)
    kinds = [rng.choice(list(es.EventKind)) for _ in range(count)]
    ticks = [rng.randint(14, 34) if kind is es.EventKind.REMOVE_LANDING_SUPPORT else rng.randint(10, 110)
             for kind in kinds]
    events = tuple(sorted((es.GeneratedEvent(k, t) for k, t in zip(kinds, ticks)),
                          key=lambda item: (item.tick, item.kind.value)))
    return es.GeneratedSequence(seed, scenario, 400, events)


tally = Counter()
failures = []
for seed in range(first, last + 1):
    candidates = []
    for scenario in scenarios[:2]:
        candidates.append(("repo", es.generate_sequence(seed, event_count=4, scenario=scenario)))
    for scenario in scenarios:
        candidates.append(("repeat", with_replacement(seed, scenario, 6)))
    for label, sequence in candidates:
        outcome = es.run_sequence(sequence)
        bad = outcome.failed_invariant
        tally[(label, sequence.scenario, "FAIL" if bad else "ok")] += 1
        if bad:
            failures.append((label, sequence, outcome))
for key in sorted(tally):
    print("tally", key, tally[key], flush=True)
print(f"failing sequences: {len(failures)}", flush=True)
kinds = Counter()
for label, sequence, outcome in failures[:40]:
    shrunk = es.shrink_sequence(sequence, lambda candidate: es.run_sequence(candidate).failed_invariant)
    final = es.run_sequence(shrunk)
    summary = (final.exception if final.exception else
               f"{final.result.outcome}/{final.result.reason}/"
               f"{','.join(sorted({v[1] for v in final.result.violations}))}")
    kinds[summary.split(':')[0] if final.exception else summary] += 1
    print(f"  {label} seed={sequence.seed} {sequence.scenario}: shrunk to "
          f"{[(e.kind.value, e.tick) for e in shrunk.events]} -> {summary}", flush=True)
print("shrunk failure classes:", dict(kinds))
