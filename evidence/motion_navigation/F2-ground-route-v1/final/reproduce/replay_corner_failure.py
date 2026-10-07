from pathlib import Path
from dataclasses import replace
import json,sys
root=next(p for p in Path(__file__).resolve().parents if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
sys.path.insert(0,str(root))
from scripts.formal_observation_v3_trace import restore_observation_v3_trace
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.physics_adapter import build_physics_state
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.safe_ground_control import verified_ground_route_candidate
from mc2p.motion_nav.world_model import WorldQueryCache
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from mc2p.contracts.action_v1 import MovementV1
trace=root/'artifacts/fabric-deployment/20261007T073616711170Z-b40c75f9/client-0/trace.jsonl'
adapter=NavigationObservationAdapter()
for line in trace.open('r',encoding='utf-8'):
 r=json.loads(line); kind,payload=r['record_type'],r.get('payload',{})
 if kind=='reset': obs=payload['result']['observation']
 elif kind=='step': obs=payload['backend_result']['observation']
 else: continue
 frame=adapter.ingest(restore_observation_v3_trace(obs))
 if obs['sequence_id']==115: break
profile=NavigationSessionProfiles.load(root/'config/motion-navigation').ground
state=build_physics_state(frame,JAVA_1_21_RULESET,explicit_assumptions=dict(jumping_cooldown_ticks=0,
 movement_speed_attribute=.1,step_height_blocks=.6,gravity_attribute=.08,jump_strength_attribute=.42)).state
state=replace(state,movement_tick_id=frame.body.movement_tick_id)
print('Frame',frame.body.sequence_id,'position',frame.body.position)
results=[]
for forward,strafe in ((1,0),(1,-1),(1,1),(0,-1)):
 candidate=verified_ground_route_candidate(frame,state,MovementV1(forward=forward,strafe=strafe),
  control_ticks=2,tail_ticks=30,minimum_support=.15,profile=profile,
  query_cache=WorldQueryCache(frame.world))
 result={'movement':(forward,strafe),'status':candidate.status.value,'reason':candidate.reason,
  'missing_cells':candidate.missing_cells,'steps':candidate.physics_steps}
 results.append(result); print(result)
(root/'.tmp/f2-task5-corner-replay-red.json').write_text(json.dumps(results,indent=2)+'\n','utf-8')
