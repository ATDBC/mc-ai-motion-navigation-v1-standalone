from pathlib import Path
import json,sys
root=next(p for p in Path(__file__).resolve().parents if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
sys.path.insert(0,str(root))
from scripts.r28_baseline_alignment import raw_record
from scripts.navigation_structure_baseline import structure_signature

def rows(path): return {r['id']:r for r in map(json.loads,path.read_text('utf-8').splitlines())}
oldroot,newroot=root/'.tmp/action-hardening-p2-product',root/'.tmp/f2-task5-startfix-product'
old,new=rows(oldroot/'runs.jsonl'),rows(newroot/'runs.jsonl')
oldindex={r['id']:r['signature'] for r in json.loads((root/'evidence/motion_navigation/action-spec-hardening-v1/review-p2/product-index.json').read_text('utf-8'))['cases']}
newindex={r['id']:r['signature'] for r in json.loads((newroot/'index.json').read_text('utf-8'))['cases']}
strict=[k for k,r in new.items() if r['strict']]
air_kinds={'ControlledDropSegment','JumpGapSegment','JumpUpSegment'}
pairs=[]
for identifier in strict:
 a,b=raw_record(oldroot,old[identifier]),raw_record(newroot,new[identifier])
 slices=[]
 for raw in (a,b):
  ticks={r['movement_tick'] for r in raw['trace'] if r['action_kind'] in air_kinds}
  slices.append([r for r in raw['strict_trace'] if r['movement_tick'] in ticks])
 pair={'id':identifier,'before_frames':len(slices[0]),'after_frames':len(slices[1]),
  'before_sha256':structure_signature(slices[0]),'after_sha256':structure_signature(slices[1]),
  'motion_jobs_unchanged':old[identifier]['motion_jobs']==new[identifier]['motion_jobs'],
  'outcome_unchanged':old[identifier]['metrics']['outcome']==new[identifier]['metrics']['outcome'],
  'whole_behavior_unchanged':oldindex[identifier]==newindex[identifier]}
 pairs.append(pair)
changed=[p['id'] for p in pairs if p['before_sha256']!=p['after_sha256']]
payload={'cases':len(pairs),'changed_ids':changed,'nonempty_slices':sum(p['before_frames']>0 for p in pairs),
 'scope':'Existing normalized strict_trace rows at recorded JumpGap/JumpUp/ControlledDrop action kinds; final ordinary Walk is reported separately.',
 'normalizer_version':'r28-structure-trajectory-v1','pairs':pairs}
out=root/'.tmp/f2-task5-startfix-audit/strict-motion-comparison.json'
out.write_text(json.dumps(payload,indent=2)+'\n','utf-8')
print('Strict actions:',len(pairs),'nonempty',payload['nonempty_slices'],'changed',len(changed))
print('Changed examples',changed[:12])
assert all(p['motion_jobs_unchanged'] and p['outcome_unchanged'] for p in pairs)
assert all(p['whole_behavior_unchanged'] for p in pairs if not p['before_frames'])
assert not changed
