"""Audit Task 5 evidence against frozen per-ID sources; never rewrite inputs."""
from pathlib import Path
from collections import Counter
import importlib.util
import gzip
import hashlib
import json
import sys

ROOT=next(p for p in Path(__file__).resolve().parents if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
sys.path.insert(0,str(ROOT))
F2=ROOT/'evidence/motion_navigation/F2-ground-route-v1'
DEST=ROOT/'.tmp/f2-task5-startfix-audit'
DEST.mkdir(exist_ok=True)

def read(p): return json.loads(Path(p).read_text('utf-8'))
def rows(p): return {r['id']:r for r in map(json.loads,Path(p).read_text('utf-8').splitlines())}
def write(name,value): (DEST/name).write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n','utf-8')

def groups():
    results={}
    for group in (sys.argv[2:] or ('faults','world_changes','follow','coordination','product')):
        archived=ROOT/f'evidence/motion_navigation/action-spec-hardening-v1/review-p2/{group}-index.json'
        current=ROOT/f'.tmp/f2-task5-startfix-{group}'
        a,b=read(archived),read(current/'index.json')
        assert a['normalizer_version']==b['normalizer_version']
        assert a.get('signature_schema_version')==b.get('signature_schema_version')
        old,new=({r['id']:r['signature'] for r in value['cases']} for value in (a,b))
        assert old.keys()==new.keys()
        changed=[k for k in old if old[k]!=new[k]]
        details=[]
        previous=ROOT/f'.tmp/action-hardening-p2-{group}'
        if group=='product':
            oldrows,newrows=rows(previous/'runs.jsonl'),rows(current/'runs.jsonl')
            assert oldrows.keys()==newrows.keys()==old.keys()
            assert all(oldrows[k]['parameters']==newrows[k]['parameters'] for k in oldrows)
            lost=[k for k in oldrows if oldrows[k]['metrics']['success'] and not newrows[k]['metrics']['success']]
            gained=[k for k in oldrows if not oldrows[k]['metrics']['success'] and newrows[k]['metrics']['success']]
            assert not lost, lost
            assert not any(r['exception'] or r['metrics']['safety_events'] for r in newrows.values())
            motion_changed=[k for k in oldrows if oldrows[k]['motion_jobs']!=newrows[k]['motion_jobs']]
            for k in changed:
                x,y=oldrows[k],newrows[k]
                details.append({'id':k,'before_success':x['metrics']['success'],
                    'after_success':y['metrics']['success'],
                    'motion_jobs_unchanged':x['motion_jobs']==y['motion_jobs'],
                    'changed_metrics':[f for f in x['metrics'] if x['metrics'][f]!=y['metrics'][f]],
                    'before_signature':old[k],'after_signature':new[k]})
            extra={'before_success':sum(r['metrics']['success'] for r in oldrows.values()),
                'after_success':sum(r['metrics']['success'] for r in newrows.values()),
                'gained_ids':gained,'lost_ids':lost,'motion_job_changed_ids':motion_changed,
                'changed_by_group':dict(Counter(newrows[k]['group'] for k in changed)),
                'strict_changed_ids':[k for k in changed if newrows[k]['strict']]}
        else:
            for k in changed:
                filename=k.replace('/','__')+'.json.gz'
                with gzip.open(previous/filename,'rt',encoding='utf-8') as stream: x=json.load(stream)
                with gzip.open(current/filename,'rt',encoding='utf-8') as stream: y=json.load(stream)
                assert x['input']==y['input']
                assert x['passed'] and y['passed']
                details.append({'id':k,'before_signature':old[k],'after_signature':new[k],
                    'changed_raw_fields':[f for f in x['raw'] if x['raw'][f]!=y['raw'].get(f)],
                    'before_passed':x['passed'],'after_passed':y['passed']})
            extra={}
        result={'cases':len(old),'identical':len(old)-len(changed),'changed_ids':changed,
            'normalizer_version':a['normalizer_version'],'signature_schema':a.get('signature_schema_version'),
            'baseline':str(archived.relative_to(ROOT)),
            'baseline_sha256':hashlib.sha256(archived.read_bytes()).hexdigest(),
            'results':read(current/'s0r-summary.json')['results'],'details':details,**extra}
        write(group+'-comparison.json',result)
        results[group]={k:v for k,v in result.items() if k not in {'details','gained_ids','lost_ids','motion_job_changed_ids','strict_changed_ids','changed_ids'}}
        print(group,len(old),'same',len(old)-len(changed),'changed',len(changed),flush=True)
    write('five-group-summary.json',results)

def f2():
    path=F2/'task4/audit_evidence.py'
    spec=importlib.util.spec_from_file_location('f2_previous_audit',path)
    helper=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    allrows=rows(ROOT/'.tmp/f2-task5-startfix-all/runs.jsonl')
    component=DEST/'component-runs.jsonl'
    component.write_text(''.join(json.dumps(r,sort_keys=True)+'\n' for r in allrows.values() if r['kind']=='component'),'utf-8')
    helper.components(F2/'task3/review-fix/runs.jsonl',component,DEST/'component-comparison.json')
    helper.compare(F2/'task4/before-player-runs.jsonl',ROOT/'.tmp/f2-task5-startfix-all/runs.jsonl',DEST/'player-comparison.json')
    helper.strict(ROOT/'.tmp/f2-task5-startfix-all/runs.jsonl',DEST/'player-comparison.json',DEST/'strict-goal-proof.json')
    before=rows(F2/'task4/player-reference-runs.jsonl')
    references=[k for k,r in allrows.items() if r['family'].startswith('reference_')]
    fields=('input_sha256','outcome','reason','trajectory_sha256','inputs_sha256')
    changes=[k for k in references if any(before[k][f]!=allrows[k][f] for f in fields)]
    assert not changes,changes
    write('reference-comparison.json',{'cases':len(references),'changed_ids':changes,
        'all_success':all(allrows[k]['success'] for k in references),
        'source':'task4/player-reference-runs.jsonl','fields':fields})

if __name__=='__main__':
    {'groups':groups,'f2':f2}[sys.argv[1]]()
