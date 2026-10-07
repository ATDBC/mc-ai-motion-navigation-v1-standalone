from pathlib import Path
import json,sys
root=next(p for p in Path(__file__).resolve().parents if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
sys.path.insert(0,str(root))
from scripts.formal_observation_v3_trace import restore_observation_v3_trace
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.navigation_session import NavigationSession
from tests.sim.runner import _goal
trace=root/'artifacts/fabric-deployment/20261007T075937109035Z-69e30527/client-0/trace.jsonl'
adapter=NavigationObservationAdapter()
for line in trace.open('r',encoding='utf-8'):
 r=json.loads(line); kind,payload=r['record_type'],r.get('payload',{})
 if kind=='reset': obs=payload['result']['observation']
 elif kind=='step': obs=payload['backend_result']['observation']
 else: continue
 frame=adapter.ingest(restore_observation_v3_trace(obs))
 if obs['sequence_id']==398: break
node,missing=NavigationSession._surface_for_goal(frame,_goal((.5,100.,10.9)))
result={'node':None if node is None else str(node),'missing':missing,'facts':[]}
for p in missing:
 f=frame.world.cell(p)
 result['facts'].append({'position':p,'knowledge':f.knowledge.value,
  'material':None if f.block is None else f.block.material_key,
  'last_observation_sequence':None if f.stamp is None else f.stamp.sequence_id})
(root/'.tmp/f2-task5-ledge-replay-red.json').write_text(json.dumps(result,indent=2)+'\n','utf-8')
print(json.dumps(result,indent=2))
