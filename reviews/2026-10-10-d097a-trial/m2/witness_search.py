"""Differential search for a test witness of the no_landing_proof fault on the two-hole world.

For approach gait g, first jump j1 and a second jump placed at / near the landing boundary of the
middle platform, compare the scanner with lazy_prove(faults={fault}); print every candidate whose
verdict or intervals differ.  Output is kept in logs/witness_search.log.
"""
from __future__ import annotations

import sys

import candidates as K
import common
import equiv
import incremental as I
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics
from experiments.motion_navigation.trajectory_proto.contracts import SearchBudget
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence

fault = sys.argv[1] if len(sys.argv) > 1 else "no_landing_proof"
fixture = K.double_gap_fixture("A5")
request, world = fixture.request, fixture.world
P = lambda text: K.parse_labels(text, request)
W, S, J, SJ, N = (P(t)[0] for t in ("W", "S", "J", "SJ", "N"))
counter = CountedPhysics(SearchBudget(10 ** 7, 10 ** 8, 40, 2))


def rollout(inputs):
    state, states = request.entry_states[0], []
    states.append(state)
    for command in inputs:
        state = counter.step(state, command, world).next_state
        states.append(state)
    return states


tried = hits = 0
for g in (W, S):
    for a in range(1, 14):
        for j1 in (J, SJ):
            probe = (g,) * a + (j1,) + (g,) * 20
            states = rollout(probe)
            landing = next((i for i in range(a + 2, len(states)) if states[i].on_ground), None)
            if landing is None:
                continue
            for delta in range(-1, 5):
                for j2 in (J, SJ):
                    for g2 in (W, S, N):
                        for c in (0, 2, 5):
                            cut = landing + delta
                            if cut <= a + 1:
                                continue
                            inputs = probe[:cut] + (j2,) + (g2,) * c + (N,) * 16
                            inputs = inputs[:40]
                            tried += 1
                            scan, lazy = equiv.run_pair(request, inputs, world, faults=frozenset({fault}))
                            row = equiv.compare(scan, lazy)
                            if row["scan_status"] == "VERIFIED_CANDIDATE" and (
                                    not row["equivalent"] or not row["status_match"]) or row["critical"]:
                                hits += 1
                                text = " ".join(common.label(c) for c in inputs)
                                print(f"HIT scan={row['scan_status']} lazy={row['lazy_status']} "
                                      f"iv_scan={row.get('intervals_scan')} iv_lazy={row.get('intervals_lazy')} :: {text}",
                                      flush=True)
print("tried", tried, "hits", hits)
