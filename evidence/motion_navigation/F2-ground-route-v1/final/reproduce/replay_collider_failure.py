from pathlib import Path
import json,sys
root=next(p for p in Path(__file__).resolve().parents if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
sys.path.insert(0,str(root))
from scripts.formal_observation_v3_trace import restore_observation_v3_trace
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter

trace=root/'artifacts/fabric-deployment/20261007T072545022605Z-9843853c/client-0/trace.jsonl'
adapter=NavigationObservationAdapter()
with trace.open('r',encoding='utf-8') as stream:
 for line in stream:
  r=json.loads(line)
  kind,payload=r['record_type'],r.get('payload',{})
  if kind=='reset': obs=payload['result']['observation']
  elif kind=='step': obs=payload['backend_result']['observation']
  else: continue
  frame=adapter.ingest(restore_observation_v3_trace(obs))
  if obs['sequence_id']==643: break
facts=[]
for z in range(0,11):
 for y in (99,100,101,102):
  p=(0,y,z)
  f=frame.world.cell(p)
  facts.append({'position':p,'knowledge':f.knowledge.value,
   'material':None if f.block is None else f.block.material_key,
   'last_observation_sequence':None if f.stamp is None else f.stamp.sequence_id})
report={'restored_formal_observations':frame.body.sequence_id+1,
 'actor_position':frame.body.position,'actor_facts':facts,
 'prepared_corridor_expected_clear_center_cells':[(0,y,z) for z in range(4,10) for y in (100,101)]}
(root/'.tmp/f2-task5-corridor-knowledge-red.json').write_text(json.dumps(report,indent=2)+'\n','utf-8')
print('Replayed',report['restored_formal_observations'],'observations')
print('Center body cells',[(r['position'],r['knowledge'],r['material'],r['last_observation_sequence'])
 for r in facts if r['position'][1] in (100,101)])
