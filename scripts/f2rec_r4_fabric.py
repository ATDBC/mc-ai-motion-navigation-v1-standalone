"""Freeze and run the recovery's 24+16+3 Fabric groups on the existing actor."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

from scripts.f2_ground_route_runtime import frozen_plan
from scripts.f2_ground_route_evidence import digest
from scripts.f2_ground_route_quality import REFERENCE_POINTS

ROOT = Path(__file__).resolve().parents[1]


def plan():
    supports = frozen_plan(f2rec=True)
    geometry = frozen_plan(f2r=True)
    ordinary = frozen_plan()
    actors = ['f2-'+family+'-0-normal' for family in REFERENCE_POINTS]
    references = ['f2-reference-'+family for family in REFERENCE_POINTS]
    representative = [row for row in ordinary if row['id'] in actors+references]
    assert len(supports)==24 and len(geometry)==16 and len(representative)==6
    return {'schema_version':'mc2p.f2rec-r4-fabric-plan.v1',
        'support_edge':supports, 'f2r_geometry':geometry,
        'ordinary_actor_reference':representative,
        'groups':43,'actual_trials':46,'actor_information':'profile_4_surface_depth',
        'conditions':'normal and first command actually applied one tick late',
        'reference_policy':'three pairs use exactly the frozen scene/start/original GoalState'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--plan-only',action='store_true')
    args=parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True)
    frozen=plan()
    (args.output/'plan.json').write_text(json.dumps({**frozen,'sha256':digest(frozen)},
        indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    if args.plan_only:
        return 0
    commands=[('--f2rec','support_edge'),('--f2r','f2r_geometry'),(None,'ordinary_actor_reference')]
    result=[]
    for flag,key in commands:
        command=[sys.executable,'-m','scripts.f2_ground_route_runtime',
            '--output-root',str(args.output/key),'--timeout-seconds','600','--ids',
            *[row['id'] for row in frozen[key]]]
        if flag:
            command.append(flag)
        print('FABRIC_GROUP_START',key,flush=True)
        with (args.output/(key+'.log')).open('w',encoding='utf-8') as log:
            code=subprocess.call(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        result.append({'group':key,'command':command,'return_code':code})
        (args.output/'execution.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print('FABRIC_GROUP_END',key,code,flush=True)
        if code:
            return code
    return 0


if __name__=='__main__':
    raise SystemExit(main())
