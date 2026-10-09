"""Per-tick trace of the v9 column-top approach under 20% random late inputs.

    python <this file> SEED GOAL_X GOAL_Z [FIRST_TICK]   (from a checkout root)

Each row: tick, applied (forward, strafe, sneak), whether that movement tick
was late, position, horizontal speed b/s, yaw, action index, segment, the
next segment's entry reference and required yaw, and the session reason.
"""
import math, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import tests.sim.runner as runner
from tests.sim.backend import Scene, Perturbations
STONE="minecraft:stone"
s={(x,63,z):STONE for x in range(-4,5) for z in range(-1,11)}
s.update({(x,64,z):STONE for x in (3,4) for z in (9,10)})
seed=int(sys.argv[1]); gx,gz=float(sys.argv[2]),float(sys.argv[3])
late=runner.late_ticks(0.2, seed)
rows=[]
def step(c):
    from mc2p.contracts.behavior import BehaviorProfileV0
    ex=c.session._executor
    c.driver.tick(BehaviorProfileV0(), c.clock[0]+500_000_000)
    st=c.backend.state
    route=getattr(ex,'route',None)
    seg=None; win=None
    if route is not None and ex.action_index < len(route.actions):
        seg=type(route.actions[ex.action_index]).__name__[:6]
        if ex.action_index+1 < len(route.actions):
            w=getattr(route.actions[ex.action_index+1],'entry_window',None)
            if w is not None: win=(tuple(round(v,2) for v in w.reference_point), None if w.required_yaw_radians is None else round(math.degrees(w.required_yaw_radians),1))
    sp=math.hypot(st.velocity_blocks_per_tick[0],st.velocity_blocks_per_tick[2])*20
    si=c.backend.sampled_inputs[-1] if c.backend.sampled_inputs else None
    keys=None if si is None else (si.forward, si.strafe, si.sneak)
    rows.append((c.tick, keys, c.backend.movement_tick in late, tuple(round(v,3) for v in st.position), round(sp,2), round(math.degrees(st.yaw_radians),1), getattr(ex,'action_index',None), seg, win, c.session.report.reason))
    return ()
r=runner.run(runner.Scenario("c", Scene(s,((-7,8),(60,69),(-5,14))),(.5,64.,.5),(gx,65.,gz),0.,max_ticks=300,perturbations=Perturbations(late_ticks=late)),control_step=step)
print(r.outcome, r.reason, r.final_position)
for row in rows[int(sys.argv[4]) if len(sys.argv)>4 else 0:]:
    print(row)
