"""E9: decision cost of the rolling proof with both timing branches vs with the ON_TIME branch only
(= after the first application has fixed the timing).  Linux; ratios matter, not absolute ms."""
import math, statistics, time
from dataclasses import replace
from experiments.motion_navigation.trajectory_proto.contracts import TimingBranch
from experiments.motion_navigation.trajectory_proto.incremental_proof import prove_incrementally
from experiments.motion_navigation.trajectory_proto.reference_search import _boundary_evidence
from experiments.motion_navigation.trajectory_proto.measurement import _set_a, _set_c, _set_d
from experiments.motion_navigation.trajectory_proto.physics import CountedPhysics

def one_branch(request):
    return replace(request, entry_states=request.entry_states[:1], timing_branches=(TimingBranch.ON_TIME,),
                   allowed_effect_ticks=request.allowed_effect_ticks[:1], branch_preludes=request.branch_preludes[:1])

def pct(v, q):
    v = sorted(v); return v[max(0, math.ceil(q * len(v)) - 1)]
cases = list(_set_a(4)) + list(_set_c(6)) + list(_set_d(18))
for label, transform in (("two branches", lambda r: r), ("ON_TIME only", one_branch)):
    kinds = {"plan": [], "tick": [], "commit": []}
    steps = {"plan": [], "tick": [], "commit": []}
    commit_by_case = {}
    for _ in range(5):
        for set_id, key, request, world, inputs, *_ in cases:
            request = transform(request)
            ev = _boundary_evidence(request, inputs)
            proof = prove_incrementally(request, inputs, world, ev, mode="strict_runahead", runahead_steps=24)
            assert proof.status.value == "VERIFIED_CANDIDATE", (key, proof.status, proof.reason)
            for d in proof.decisions:
                if d.kind in kinds:
                    kinds[d.kind].append(d.wall_ns / 1e6); steps[d.kind].append(d.physics_steps)
                    if d.kind == "commit":
                        commit_by_case.setdefault(key, []).append((d.boundary, d.physics_steps, d.wall_ns / 1e6))
    print(f"== {label}: {len(cases)} cases x5")
    for k in kinds:
        if kinds[k]:
            print(f"   {k:6s} n={len(kinds[k]):5d} ms P50={pct(kinds[k], .5):6.2f} P95={pct(kinds[k], .95):6.2f} max={max(kinds[k]):6.2f} | steps P95={pct(steps[k], .95)} max={max(steps[k])}")
    gap = commit_by_case.get("jump_gap_1:A3")
    if gap:
        print("   jump_gap_1:A3 commit decision (boundary, steps, ms):", gap[0], "median ms", round(statistics.median(x[2] for x in gap), 2))
