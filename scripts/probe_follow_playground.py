"""Bounded original-speed playground matrix entry; no visible client or training data claim."""
from pathlib import Path
import argparse
import json
import math
import os
import sys
from copy import copy

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None,''}: sys.path.insert(0,str(ROOT))
from scripts.follow_playground_scenarios import case_plan, CASES
from scripts.follow_playground_session import create_session, session_path, supervise_command, run_worker, write_json_atomic
from scripts.follow_playground_session import (
    PERCEPTION_VARIANTS, DEFAULT_PERCEPTION_VARIANT, validate_perception_configuration,
)
from scripts.follow_playground_evidence import evaluate_playground
from scripts.follow_playground_faults import FAULT_KINDS
from scripts.probe_craftground_timing_parallel import run_bounded_process
from mc2p.runtime.trace import trace_projection


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed',type=int,choices=(21001,21002,21003),default=21001)
    parser.add_argument('--case',choices=CASES,default='fixed')
    parser.add_argument('--perception-variant',choices=PERCEPTION_VARIANTS,default=DEFAULT_PERCEPTION_VARIANT)
    parser.add_argument('--timeout-seconds',type=int,default=300)
    parser.add_argument('--parent',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--session-id',help=argparse.SUPPRESS)
    parser.add_argument('--fault',choices=FAULT_KINDS,help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 180<=args.timeout_seconds<=900: parser.error('case timeout must be 180..900 seconds')
    if args.fault and args.case!='interrupt': parser.error('fault injection requires interrupt case')
    if args.case=='active-world-change' and args.perception_variant!='active_perception_v1':
        parser.error('active-world-change requires active_perception_v1')
    plan = case_plan(args.case,args.fault)
    if plan['duration_ns']/1e9+60>args.timeout_seconds: parser.error('timeout cannot fit declared case and cleanup')
    try:
        validate_perception_configuration(args.perception_variant)
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.worker: return run_worker(session_path(args.session_id))
    if args.parent:
        return supervise_command(session_path(args.session_id),[sys.executable,str(Path(__file__).absolute()),
            '--worker','--session-id',args.session_id,'--case',args.case,'--seed',str(args.seed),'--timeout-seconds',str(args.timeout_seconds),
            '--perception-variant',args.perception_variant,
            *(['--fault',args.fault] if args.fault else [])])
    status,directory=run_case(args)
    if status==0 and args.case=='interrupt' and not args.fault:
        results=[]
        for fault in FAULT_KINDS:
            child=copy(args); child.fault=fault
            code,child_dir=run_case(child)
            results.append(dict(kind=fault,directory=str(child_dir),passed=code==0))
            if code: break
        status=0 if len(results)==len(FAULT_KINDS) and all(r['passed'] for r in results) else 1
        write_json_atomic(directory/'interrupt-suite.json',dict(schema_version='mc2p.playground-interrupt-suite.v1',
            command_case=str(directory),fault_cases=results,passed=status==0))
    print('FOLLOW_PLAYGROUND_CASE_OK' if status==0 else 'FOLLOW_PLAYGROUND_CASE_FAILED',flush=True)
    return status


def run_case(args):
    plan=case_plan(args.case,args.fault)
    directory = create_session(args.seed,interactive=False,test_duration_seconds=math.ceil(plan['duration_ns']/1e9)+5,
                               perception_variant=args.perception_variant)
    write_json_atomic(directory/'case-plan.json',plan)
    print('FOLLOW_PLAYGROUND_RUN_DIR='+str(directory),flush=True)
    command = [sys.executable,str(Path(__file__).absolute()),'--parent','--session-id',directory.name,
               '--case',args.case,'--seed',str(args.seed),'--timeout-seconds',str(args.timeout_seconds),
               '--perception-variant',args.perception_variant,
               *(['--fault',args.fault] if args.fault else [])]
    supervision = run_bounded_process(command,cwd=ROOT,environment=dict(os.environ),log_path=directory/'parent-console.log',timeout_seconds=args.timeout_seconds)
    write_json_atomic(directory/'outer-supervisor.json',trace_projection(supervision))
    if (directory/'result.json').exists():
        result = json.loads((directory/'result.json').read_text('utf-8'))
        write_json_atomic(directory/'inner-result.json',result)
        if supervision.return_code!=0 or supervision.primary_failure or supervision.cleanup_failures or not supervision.process_stopped:
            result.update(state='failed',outer_failure=trace_projection(supervision))
        write_json_atomic(directory/'result.json',result)
    resources=None
    try:
        if args.case=='long-session':
            from scripts.follow_playground_resources import ResourceSampler
            resources=ResourceSampler(directory/'evaluator-rss','evaluator'); resources.start()
        evidence = evaluate_playground(directory,args.case)
    finally:
        if resources is not None: resources.close()
    if resources is not None:
        from scripts.follow_playground_resources import attach_evaluator_resources
        attach_evaluator_resources(evidence,directory/'evaluator-rss',resources.identity)
    if args.case=='active-quality':
        from scripts.active_perception_evidence import summarize_quality
        # Keep the original general gate result separate, including all failures.
        write_json_atomic(directory/'case-base-evidence.json',evidence)
        quality=summarize_quality(directory)
        write_json_atomic(directory/'quality-evidence.json',quality)
        evidence['metrics']['active_quality']=quality
        evidence['checks'].append(dict(name='actual_pose_quality_evaluable',passed=quality['measurement_status']=='passed'))
        evidence['checks'].append(dict(name='quality_tasks_completed',passed=quality['task_status']=='passed'))
        evidence['baseline_admissible']=evidence['passed'] and quality['baseline_admissible']
        evidence['passed']=evidence['passed'] and quality['status']=='passed'
    write_json_atomic(directory/'case-evidence.json',evidence)
    return (0 if evidence['passed'] else 1),directory


if __name__=='__main__': raise SystemExit(main())
