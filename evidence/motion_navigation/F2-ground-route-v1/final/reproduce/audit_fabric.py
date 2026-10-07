"""Audit complete frozen F2 coverage from explicitly selected native batches."""
from pathlib import Path
import argparse,hashlib,json,sys
import math

ROOT=next(p for p in Path(__file__).resolve().parents
    if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
sys.path.insert(0,str(ROOT))
from scripts.f2_ground_route_runtime import frozen_plan,digest
from scripts.f2_ground_route_quality import REFERENCE_POINTS,motion_quality

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--manifest",type=Path,required=True)
parser.add_argument("--output",type=Path,default=ROOT/".tmp/f2-task5-fabric-audit.json")
args=parser.parse_args()
read=lambda p:json.loads(p.read_text("utf-8"))
rows=lambda p:[json.loads(l) for l in p.read_text("utf-8").splitlines()]
manifest=read(args.manifest)
expected={r["id"]:r for r in frozen_plan()}
trials={};sources=None;batches=[];quality_frames=0;reference_continuity=[];released_sources=0
for relative in manifest["native_batches"]:
    directory=ROOT/relative
    native=read(directory/"result.json")
    assert native["status"]=="passed",(relative,native.get("primary_failure"))
    assert all(c["passed"] for c in native["checks"]),relative
    assert not native["cleanup_failures"] and not native["diagnostic_failures"],relative
    assert native["ports_free"],relative
    summary=read(directory/"client-0/f2-summary.json")
    plan=read(directory/"client-0/f2-plan.json")
    assert summary["passed"] and summary["sources_unchanged"],relative
    assert summary["plan_sha256"]==plan["sha256"]==digest(plan["cases"])
    current={k:summary[k] for k in ("production","harness")}
    assert summary["source_before"]==current
    if sources is None:
        sources=current
    assert sources==current,"Batch source bytes changed"
    for group in sources.values():
        for name,sha in group["files"].items():
            assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==sha,name
    actual=rows(directory/"client-0/f2-trials.jsonl")
    assert len(actual)==len(plan["cases"])==summary["cases"]
    assert {r["id"] for r in actual}=={r["id"] for r in plan["cases"]}
    frames=rows(directory/"client-0/f2-frames.jsonl")
    reference_path=directory/"client-0/f2-reference-frames.jsonl"
    if reference_path.exists():
        frames+=rows(reference_path)
    registered={};unregistered={}
    with (directory/"client-0/trace.jsonl").open(encoding="utf-8") as stream:
        for index,line in enumerate(stream):
            event=json.loads(line)
            if event["record_type"]=="ordered_source_registered":
                registered[event["payload"]["source"]["source_id"]]=index
            elif event["record_type"]=="ordered_source_unregistered":
                unregistered[event["payload"]["source"]["source_id"]]=index
    assert registered.keys()==unregistered.keys(),(relative,"Source responsibility leaked")
    assert all(registered[sid]<unregistered[sid] for sid in registered)
    released_sources+=len(unregistered)
    for r in actual:
        identifier=r["id"]
        assert identifier not in trials,identifier
        assert identifier in expected,identifier
        assert all(r[k]==v for k,v in expected[identifier].items()),identifier
        assert r["passed"] and not r["violations"],identifier
        assert not any(r[k] for k in ("drop_frames","damage_points","danger_contact_frames")),identifier
        body=r["final_body"]
        assert body["is_on_ground"] and body["pose"]=="standing" and not body["is_sneaking"]
        if not r.get("reference"):
            assert r["state"] in r.get("expected_terminals",[r["expected"]]),identifier
        if r.get("expected")=="success" and not r.get("reference") and not r.get("injection"):
            assert r["task_success"] and r["formal_goal_status"]=="satisfied",identifier
        if not r.get("reference"):
            assert r["max_full_candidates"]<=3,identifier
        selected=[f for f in frames if f["trial"]==identifier]
        if r.get("reference"):
            held=[f for f in selected if f["movement"]["forward"]==1]
            actual_ticks=[t for f in held for t in f["actual_application_ticks"]]
            assert actual_ticks==list(range(actual_ticks[0],actual_ticks[-1]+1)),(
                identifier,"Reference was not continuously applied")
            assert all(f["movement"]["strafe"]==0 for f in held)
            assert all(min(f["actual_application_ticks"])<=f["latest_allowed_first_tick"]
                for f in selected),identifier
            assert r["formal_goal_status"]=="satisfied"
            assert math.hypot(body["velocity_blocks_per_second"][0],
                body["velocity_blocks_per_second"][2])<=1.e-9
            reference_continuity.append({"id":identifier,"held_input_ticks":len(actual_ticks),
                "first_tick":actual_ticks[0],"last_tick":actual_ticks[-1],
                "gaps":0,"final_exact_rest_in_original_goal":True})
        recomputed=motion_quality(selected,r["start_position"])
        assert all(recomputed[k]==v for k,v in r["motion_quality"].items()
            if k!="driver_terminal_tick"),(identifier,"Motion quality mismatch")
        assert not recomputed["stagnant_windows"],identifier
        quality_frames+=len(selected)
        trials[identifier]=r
    batches.append({"raw_directory":relative,"cases":len(actual),"native_checks":native["checks"],
        "batch_plan_sha256":plan["sha256"],"native_passed":True})
assert set(trials)==set(expected),("Incomplete coverage",sorted(set(expected)-set(trials)))
pairs=[]
for family in REFERENCE_POINTS:
    actor=trials["f2-"+family+"-0-normal"]
    reference=trials["f2-reference-"+family]
    assert all(actor[k]==reference[k] for k in
        ("scene_sha256","start_position","goal_bounds","original_goal")),family
    actor_ticks=actor["motion_quality"]["completion_ticks"]
    reference_ticks=reference["motion_quality"]["completion_ticks"]
    assert actor_ticks is not None and reference_ticks and actor_ticks/reference_ticks<=1.3,family
    pairs.append({"family":family,"same_scene_start_and_original_goal":True,
        "actor_timing":actor["motion_quality"],"reference_timing":reference["motion_quality"],
        "actor_reference_tick_ratio":actor_ticks/reference_ticks,"passed":True})
positive=[r for r in trials.values() if r["expected"]=="success"
    and not r.get("injection") and not r.get("reference") and r["condition"]!="late_two"]
assert len(positive)==80 and all(r["task_success"] for r in positive)
assert sum(r["input_deadline_miss_count"] for r in positive)==0
late=[r for r in positive if r["condition"]=="late_first"]
assert len(late)==40
assert all(r["late_input"]["actual_offset"]==2 and r["late_input"]["status"]=="applied"
    and r["late_input"]["valid_for_ticks"]==1 for r in late)
payload={"passed":True,"cases":len(trials),"positive_success":len(positive),
    "positive_late_first_actual_applied":len(late),"positive_input_deadline_misses":0,
    "frozen_plan_sha256":digest(frozen_plan()),"production":sources["production"],
    "harness":sources["harness"],"batches":batches,"quality_pairs":pairs,
    "audited_motion_quality_frames":quality_frames,"stagnant_10_tick_windows":0,
    "reference_continuity":reference_continuity,
    "ordered_sources_registered_and_released":released_sources,
    "safety":{"drop_frames":0,"damage_points":0,"danger_contact_frames":0,"terminal_sneak":0},
    "failed_history_included_as_passed":False,
    "trials":[trials[r["id"]] for r in frozen_plan()]}
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(payload,indent=2)+"\n","utf-8")
print("Frozen Fabric audit",len(trials),"positive",len(positive),"late1",len(late),"pairs",len(pairs))
