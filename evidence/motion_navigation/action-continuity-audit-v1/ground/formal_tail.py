"""One existing formal-path scenario; add read-only route capture."""
from pathlib import Path
import sys
import json
import math
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from tests.sim.runner import Scenario,run,lane
from tests.sim.scenarios import columns
from mc2p.contracts.behavior import BehaviorProfileV0

seen=[]
last_key=None
def control(context):
    global last_key
    context.driver.tick(BehaviorProfileV0(),context.clock[0]+500_000_000)
    executor=context.session._executor
    if executor is not None and executor.route is not None:
        key=(id(executor),executor.action_index)
        if key!=last_key:
            last_key=key
            controller=executor._controller
            config=getattr(controller,"config",None)
            seen.append(dict(tick=context.backend.movement_tick,
                action_index=executor.action_index,route_id=executor.route.route_id,
                actions=[dict(kind=type(action).__name__,
                    route_id=getattr(getattr(action,"fixed_route",None),"route_id",None),
                    has_traversal_plan=getattr(action,"traversal_plan",None) is not None)
                    for action in executor.route.actions],
                position=context.backend.state.position,
                speed=math.hypot(context.backend.state.velocity_blocks_per_tick[0],
                    context.backend.state.velocity_blocks_per_tick[2])*20,
                stopped_speed=None if config is None else config.stopped_speed_blocks_per_second,
                has_handoff_window=False if config is None else config.handoff_entry_window is not None,
                handoff_speed=None if config is None else config.handoff_speed_blocks_per_second))
    return ()

scenario=Scenario("existing-height-goal-tail-audit",
    lane(columns([68,68,67,66,65,64,64]),width=3),(.5,68.,.5),(.68,64.,6.82))
result=run(scenario,control_step=control)
output=dict(outcome=result.outcome,reason=result.reason,ticks=result.ticks,violations=result.violations,
    route_entries=seen,trace=result.trace)
Path(__file__).with_name("formal-tail.json").write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({key:value for key,value in output.items() if key!="trace"},ensure_ascii=False,indent=2))
