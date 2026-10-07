"""Small R28 product regressions on the formal Runtime navigation path."""
from pathlib import Path
import time
import hashlib
import os
import random
import math
from dataclasses import asdict

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver, RuntimeNavigationDriverState
from scripts.control_probe_core import append_jsonl
from scripts.r25_planning_information_runtime import _task
from tests.sim.runner import _goal
from tests.sim.product_cases import player_layout


def _rotate(x, z, direction):
    return ((x, z), (z, -x), (-x, -z), (-z, x))[direction]


def run_r28_product_runtime(runtime, backend, episode, directory, deadline_ns, fixture_writer,
                            *, trace_owns_diagnostics=False):
    profile, task = BehaviorProfileV0(), _task(deadline_ns)
    profiles = NavigationSessionProfiles.load(Path("config/motion-navigation"))
    trials, rows, diagnostic_rows = [], [], []
    def diagnostic():
        row = {"episode_id": episode, "observation_sequence_id": runtime.observation.sequence_id,
               "diagnostics": backend.last_diagnostics}
        diagnostic_rows.append(row)
        if not trace_owns_diagnostics:
            append_jsonl(directory / "diagnostics.jsonl", row)
    diagnostic()
    if os.environ.get('MC2P_R28_BATCH5') == '1':
        return run_batch5(runtime,backend,episode,directory,deadline_ns,fixture_writer,
                          profile,task,profiles,diagnostic,diagnostic_rows)
    cases = [("point", (.911, 100., 9.316)), ("point-margin", (.636, 100., 10.366)),
             ("wall", (2.5, 100., 10.5))]
    for kind, local_goal in cases:
        for direction in range(4):
            name = f"r28-{kind}-{direction}"
            # These commands create only a dedicated local test fixture. The
            # actor receives the normal profile-4 observations, never truth.
            fixture_writer(("fill -18 96 -18 18 105 18 minecraft:air replace",
                            "fill -16 99 -16 16 99 16 minecraft:stone replace"))
            if kind == "wall":
                # Cell rotation differs from point rotation on negative axes.
                blocks = [_rotate(x + .5, 5.5, direction) for x in (-1, 0, 1)]
                import math
                fixture_writer(tuple(f"setblock {math.floor(x)} {y} {math.floor(z)} minecraft:stone"
                                      for x, z in blocks for y in (100, 101, 102)))
            requested = tuple((x, y, z) for x in range(-4, 5) for y in range(99, 103) for z in range(-12, 13))
            # Explore the fixture from both sides before the measured task;
            # all facts still pass through the real surface-depth sensor.
            for x, z, yaw in ((-3.5, .5, -45), (3.5, 8.5, 135), (3.5, .5, 45), (-3.5, 8.5, -135)):
                px, pz = _rotate(x, z, direction)
                fixture_writer((f"tp MC2PProbe {px} 100 {pz} {yaw - 90 * direction} 45",))
                for offset in range(0, len(requested), 128):
                    result = runtime.step(task, profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000),
                        observation_request=ObservationRequestV3("navigation_v1", requested[offset:offset + 128]))
                    diagnostic()
                    if result.report.failure is not None:
                        raise RuntimeError(result.report.failure)
            sx, sz = _rotate(.5, .5, direction)
            fixture_writer((f"tp MC2PProbe {sx} 100 {sz} {-90 * direction} 35",))
            for _ in range(4):
                runtime.step(task, profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000))
                diagnostic()
            gx, gz = _rotate(local_goal[0], local_goal[2], direction)
            goal = _goal((gx, local_goal[1], gz))
            session = NavigationSession(name, profiles, observation_adapter=runtime.navigation_observation_adapter)
            driver = RuntimeNavigationDriver(runtime, session)
            started = time.perf_counter_ns()
            driver.start(name, 1, goal, started)
            first_tick = runtime.navigation_observation_adapter.latest_frame.body.movement_tick_id
            try:
                for index in range(300):
                    if driver.state in {RuntimeNavigationDriverState.SUCCESS, RuntimeNavigationDriverState.FAILED, RuntimeNavigationDriverState.CANCELLED}:
                        break
                    result = driver.tick(profile, min(deadline_ns, time.perf_counter_ns() + 500_000_000))
                    diagnostic()
                    frame = runtime.navigation_observation_adapter.latest_frame
                    movement = None if result.decision is None else result.decision.action.movement
                    row = {"trial": name, "frame": index, "movement_tick": frame.body.movement_tick_id,
                           "position": frame.body.position, "velocity": frame.body.velocity_blocks_per_second,
                           "on_ground": frame.body.is_on_ground, "state": driver.state.value,
                           "reason": driver.reason, "movement": None if movement is None else {
                               "forward": movement.forward, "strafe": movement.strafe, "jump": movement.jump}}
                    rows.append(row)
                    append_jsonl(directory / "r28-product-frames.jsonl", row)
                    if result.report.failure is not None:
                        raise RuntimeError(result.report.failure)
                frame = runtime.navigation_observation_adapter.latest_frame
                trials.append({"id": name, "outcome": driver.state.value, "reason": driver.reason,
                               "passed": driver.state == RuntimeNavigationDriverState.SUCCESS, "position": frame.body.position,
                               "movement_ticks": frame.body.movement_tick_id - first_tick,
                               "wall_elapsed_seconds": (time.perf_counter_ns() - started) / 1e9})
            finally:
                driver.release("r28_product_probe_end")
                session.close()
    checks = [{"name": row["id"], "passed": row["passed"]} for row in trials]
    return {"probe": "r28-product-formal-navigation", "trials": trials,
            "passed": all(row["passed"] for row in trials),
            "sources": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in (
                "mc2p/motion_nav/support_surfaces.py", "mc2p/motion_nav/route_admission.py",
                "mc2p/motion_nav/known_map_planner.py", "mc2p/motion_nav/fixed_route.py",
                "scripts/r28_product_fabric_runtime.py")}}, diagnostic_rows, checks


def run_batch5(runtime,backend,episode,directory,deadline_ns,fixture_writer,
               profile,task,profiles,diagnostic,diagnostic_rows):
    families=('player_wall_head','player_wall_parallel','player_corner',
              'player_corridor_end','player_ledge_1','player_ledge_2','wall_detour')
    start,count=int(os.environ['MC2P_R28_CASE_START']),int(os.environ['MC2P_R28_CASE_COUNT'])
    late=float(os.environ['MC2P_R28_LATE_PROBABILITY'])
    trials=[]
    for number in range(start,start+count):
        family,direction=families[number//4],number%4
        if family=='wall_detour':
            from tests.sim.product_cases import _platform,STONE
            from tests.sim.backend import Scene
            blocks=_platform();blocks.update({(x,y,5):STONE for x in (-1,0,1) for y in (64,65,66)})
            scene,initial,target=Scene(blocks,((-5,5),(60,68),(-2,16))),(.5,64.,.5),(2.5,64.,10.5)
        else:
            scene,initial,target=player_layout(family)
        name=f'batch5-{number:02d}-'+('late' if late else 'normal')
        fixture_writer(('fill -20 96 -20 20 105 20 minecraft:air replace',))
        # Exact support geometry: a wide artificial floor would hide the ledge.
        commands=[]
        for (x,y,z),block in scene.solids.items():
            px,pz=_rotate(x+.5,z+.5,direction)
            commands.append(f'setblock {math.floor(px)} {y+36} {math.floor(pz)} {block}')
        fixture_writer(tuple(commands))
        requested=[]
        for x in range(-5,6):
            for z in range(-2,15):
                px,pz=_rotate(x+.5,z+.5,direction)
                for y in range(98,103):
                    requested.append((math.floor(px),y,math.floor(pz)))
        for x,z,yaw in ((-3.5,.5,-45),(3.5,8.5,135),(3.5,.5,45),(-3.5,8.5,-135)):
            px,pz=_rotate(x,z,direction)
            fixture_writer((f'tp MC2PProbe {px} 100 {pz} {yaw-90*direction} 45',))
            for offset in range(0,len(requested),128):
                result=runtime.step(task,profile,min(deadline_ns,time.perf_counter_ns()+500_000_000),
                    observation_request=ObservationRequestV3('navigation_v1',tuple(requested[offset:offset+128])))
                diagnostic()
                if result.report.failure is not None:raise RuntimeError(result.report.failure)
        sx,sz=_rotate(initial[0],initial[2],direction)
        fixture_writer((f'tp MC2PProbe {sx} 100 {sz} {-90*direction} 35',))
        for _ in range(4):
            runtime.step(task,profile,min(deadline_ns,time.perf_counter_ns()+500_000_000));diagnostic()
        gx,gz=_rotate(target[0],target[2],direction)
        session=NavigationSession(name,profiles,observation_adapter=runtime.navigation_observation_adapter)
        driver=RuntimeNavigationDriver(runtime,session)
        driver.start(name,1,_goal((gx,100.,gz)),time.perf_counter_ns())
        first_tick=runtime.navigation_observation_adapter.latest_frame.body.movement_tick_id
        rng=random.Random(f'batch5:{number}:21001')
        delayed=[]; timings=[]
        try:
            for index in range(300):
                if driver.state in {RuntimeNavigationDriverState.SUCCESS,RuntimeNavigationDriverState.FAILED,RuntimeNavigationDriverState.CANCELLED}:break
                deadline=min(deadline_ns,time.perf_counter_ns()+500_000_000)
                before=runtime.navigation_observation_adapter.latest_frame.body.movement_tick_id
                begun=time.perf_counter_ns()
                control=driver.prepare_proposals(deadline)
                prepared=time.perf_counter_ns()
                inject=rng.random()<late
                if inject:time.sleep(.055)
                result=runtime.control_frame(task,profile,deadline,proposals=control)
                driver.adopt_result(result);diagnostic()
                elapsed=time.perf_counter_ns()-begun
                frame=runtime.navigation_observation_adapter.latest_frame
                fd=driver.last_frame_diagnostics
                record=None if fd.action_request_sequence is None else runtime.input_ledger.record(fd.action_request_sequence)
                applied=None if record is None or not record.applied_ticks else min(record.applied_ticks)-before
                if inject:delayed.append({'control_sequence':fd.action_request_sequence,'actual_tick_offset':applied})
                timings.append(prepared-begun)
                append_jsonl(directory/'r28-product-frames.jsonl',dict(trial=name,frame=index,
                    movement_tick=frame.body.movement_tick_id,position=frame.body.position,
                    velocity=frame.body.velocity_blocks_per_second,on_ground=frame.body.is_on_ground,
                    state=driver.state.value,reason=driver.reason,prepare_ns=prepared-begun,
                    roundtrip_ns=elapsed,injection_requested=inject,actual_tick_offset=applied,
                    diagnostics=asdict(session.diagnostics),winning_activity=asdict(fd.movement_activity) if fd.movement_activity else None))
                if result.report.failure is not None:raise RuntimeError(result.report.failure)
            terminal_reason=driver.reason
            trials.append(dict(id=name,family=family,direction=direction,outcome=driver.state.value,reason=terminal_reason,
                task_success=driver.state==RuntimeNavigationDriverState.SUCCESS,bounded_result=driver.state in {RuntimeNavigationDriverState.SUCCESS,RuntimeNavigationDriverState.FAILED,RuntimeNavigationDriverState.CANCELLED},
                position=frame.body.position,movement_ticks=frame.body.movement_tick_id-first_tick,
                requested_late_probability=late,delayed_applications=delayed,
                verified_late=sum(r['actual_tick_offset']==2 for r in delayed),prepare_ns=timings))
        finally:
            if driver.source is not None:
                driver.release('batch5_end')
            session.close()
    checks=[{'name':r['id'],'passed':r['bounded_result']} for r in trials]
    return {'probe':'r28-batch5-formal-navigation','trials':trials,
            'passed':all(r['passed'] for r in checks),'case_start':start,'case_count':count,
            'product_success_is_not_bounded_success':True},diagnostic_rows,checks
