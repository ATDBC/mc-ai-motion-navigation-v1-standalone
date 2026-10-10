"""D094 compares the same deterministic handoff inputs without rewriting D093."""
from __future__ import annotations
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import statistics

MOTION_FIELDS=("movement_tick","position","velocity","on_ground","applied_movement","action_kind","input_window")

def rows(path):
    with gzip.open(path,"rt",encoding="utf-8") as stream:
        return {(r["family"],r["seed"]):r for r in (json.loads(line) for line in stream)}

def motion(trace):
    return [[r[field] for field in MOTION_FIELDS] for r in trace]

def compare(baseline,current,output):
    old,new=rows(baseline),rows(current)
    if old.keys()!=new.keys(): raise ValueError("paired inputs differ")
    failures=[];recovered=[];unchanged=0
    for key,before in old.items():
        after=new[key]
        if before["late_ticks"]!=after["late_ticks"]: failures.append([key,"late-schedule-changed"])
        if after["violations"] or after["damage"]: failures.append([key,"safety"])
        if before["outcome"]=="success" and after["outcome"]!="success":failures.append([key,"old-success-regression"])
        if before["outcome"]==after["outcome"]:
            if (before["reason"],before["ticks"],before["final_position"],motion(before["trace"]))!=(after["reason"],after["ticks"],after["final_position"],motion(after["trace"])):
                failures.append([key,"unchanged-case-differs"])
            else:unchanged+=1
        elif before["outcome"]=="failed" and after["outcome"]=="success":
            failure_frame=next((i for i,r in enumerate(before["trace"]) if r["session_state"]=="failed"),None)
            if failure_frame is None:failures.append([key,"old-failure-frame-missing"]);continue
            if motion(before["trace"][:failure_frame])!=motion(after["trace"][:failure_frame]):failures.append([key,"prefix-differs"])
            tail=after["trace"][failure_frame:]
            height=before["trace"][failure_frame]["position"][1]
            if any(not r["on_ground"] or r["applied_movement"]["jump"] or abs(r["position"][1]-height)>1e-8 for r in tail):failures.append([key,"recovery-not-grounded-walk"])
            recovered.append({"family":key[0],"seed":key[1],"extra_ticks":after["ticks"]-before["ticks"]})
    root=Path(__file__).resolve().parents[1]
    result={"baseline_sha256":hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "current_sha256":hashlib.sha256(current.read_bytes()).hexdigest(),"paired_cases":len(new),
        "unchanged_cases":unchanged,"recovered_cases":len(recovered),"failures":failures,
        "families":{family:{"completed":sum(r["outcome"]=="success" for r in new.values() if r["family"]==family),
            "reasons":dict(Counter(r["reason"] for r in new.values() if r["family"]==family)),
            "recovery_extra_ticks_median":statistics.median([r["extra_ticks"] for r in recovered if r["family"]==family]) if any(r["family"]==family for r in recovered) else None}
            for family in sorted({r["family"] for r in new.values()})},
        "source_fingerprints":{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in (
            "mc2p/motion_nav/action_route_executor.py","mc2p/motion_nav/navigation_session.py",
            "mc2p/motion_nav/navigation_handoff.py","mc2p/motion_nav/route_admission.py",
            "mc2p/motion_nav/route_validation.py","mc2p/motion_nav/support_surfaces.py")}}
    output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in {"source_fingerprints","families"}},indent=2))
    if failures:raise SystemExit(1)

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline",type=Path,required=True)
    parser.add_argument("--current",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();compare(args.baseline,args.current,args.output)
