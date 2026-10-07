from pathlib import Path
import json,sys
ROOT=next(p for p in Path(__file__).resolve().parents if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
sys.path.insert(0,str(ROOT))
from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1,LookV1,MovementV1,MovementTickWindowV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from tests.test_player_runtime_v1 import V3WorldBackend
from tests import test_player_runtime as legacy
rows=[]
for winning in (True,False):
 runtime=PlayerRuntimeV1(V3WorldBackend(),legacy._RecordingTrace(),clock_ns=lambda:10)
 try:
  reset=runtime.reset(ResetRequestV0('f2-binding-reset','episode-v3-world','test',1,1000))
  assert reset.succeeded
  def intent(name,priority=ActionPriorityV0.TASK,**kwargs):
   return ActionIntentV1(name,name,'episode-v3-world',runtime.observation.sequence_id,
    priority,1,900,**kwargs)
  runtime.submit_intent(intent('navigation',movement=MovementV1(forward=1),
   movement_conditioned_look_intent_id='combat-look',movement_tick_window=MovementTickWindowV1(2,3)))
  runtime.submit_intent(intent('combat-look',look=LookV1(15.,0.)))
  if not winning:
   runtime.submit_intent(intent('player-look',ActionPriorityV0.PLAYER,look=LookV1(-20.,0.)))
  result=runtime.step(legacy._task(),BehaviorProfileV0(),1000)
  assert result.report.failure is None
  decision=result.decision
  record=runtime.input_ledger.record(decision.action.request_sequence_id)
  if winning:
   assert decision.action.movement==MovementV1(forward=1)
   assert decision.movement_tick_window==MovementTickWindowV1(2,3)
   assert record.latest_allowed_first_tick==3
  else:
   assert decision.action.movement==MovementV1()
   assert decision.movement_tick_window is None
   assert ('navigation','conditioned_look_not_selected') in decision.suppressed_intents
   assert record.latest_allowed_first_tick==record.requested_first_tick
  rows.append({'conditioned_look_won':winning,'actual_action_movement':str(decision.action.movement),
   'adopted_window':None if decision.movement_tick_window is None else
    [decision.movement_tick_window.earliest_tick,decision.movement_tick_window.latest_tick],
   'requested_first_tick':record.requested_first_tick,
   'latest_allowed_first_tick':record.latest_allowed_first_tick,'status':record.status.value,
   'actual_application_ticks':record.applied_ticks})
 finally:runtime.close()
path=ROOT/'.tmp/f2-task5-startfix-look-binding.json'
path.write_text(json.dumps({'passed':True,'cases':rows},indent=2)+'\n','utf-8')
print(path.read_text())
