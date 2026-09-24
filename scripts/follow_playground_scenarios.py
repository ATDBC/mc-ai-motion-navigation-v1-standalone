"""Predeclared, bounded test schedules. None of these schedules are follower inputs."""
from mc2p.contracts.action_v1 import MovementV1, LookV1
from mc2p.skills.follow_gaits import fixed_movement

NS = 1_000_000_000
CASES = ('fixed','tracking','auto','navigation','search','search-missing','long-session','interrupt','active-quality','active-world-change')


def case_plan(case: str, fault: str | None = None) -> dict:
    if case not in CASES: raise ValueError('undeclared playground case')
    if case == 'active-quality' and fault is None:
        from scripts.active_perception_scenarios import quality_case_plan
        return quality_case_plan()
    if case == 'active-world-change' and fault is None:
        from scripts.active_perception_world_change import world_change_plan
        return world_change_plan()
    if fault is not None:
        from scripts.follow_playground_faults import fault_contract
        if case!='interrupt': raise ValueError('faults belong only to interrupt cases')
        plan=case_plan(case)
        plan.update(fault=fault_contract(fault),duration_ns=20*NS,
            phases=[dict(id=0,kind='chase',mode='max',start_ns=2*NS,duration_ns=18*NS)],
            owner_commands=[dict(at_ns=2*NS,kind='follow_mode',mode='max'),dict(at_ns=2*NS,kind='follow_start')])
        return plan
    phases,commands = [],[]
    def phase(kind,mode,start,duration):
        phases.append(dict(id=len(phases),kind=kind,mode=mode,start_ns=int(start*NS),duration_ns=int(duration*NS)))
    def command(at,kind,**values):
        commands.append(dict(at_ns=int(at*NS),kind=kind,**values))
    if case=='fixed':
        for i,mode in enumerate(('slow','normal','fast','max')):
            start = 2+38*i
            phase('separate',mode,start,3)
            phase('chase',mode,start+3,10)
            phase('stop',mode,start+13,13)
            phase('rechase',mode,start+26,12)
            command(start,'follow_stop'); command(start,'follow_mode',mode=mode)
            command(start+3,'follow_start')
        duration = 154
    elif case in {'search','search-missing'}:
        command(2,'follow_mode',mode='normal'); command(2,'follow_start')
        if case=='search':
            phase('static','normal',2,2)
            phase('enter','normal',4,3)
            phase('corner','normal',7,3)
            phase('search_wait','normal',10,39)
        else:
            phase('escape','normal',2,77)
        duration=50 if case=='search' else 80
    elif case=='navigation':
        command(2,'follow_mode',mode='normal'); command(6,'follow_start')
        phase('near','normal',2,4); phase('static','normal',6,8)
        duration=15
    elif case=='auto':
        command(2,'follow_mode',mode='auto'); command(2,'follow_start')
        phase('static','auto',2,30); phase('moving','auto',32,60)
        duration = 93
    elif case=='long-session':
        command(2,'follow_mode',mode='normal'); command(2,'follow_start')
        modes = ('normal','slow','fast','max','auto','normal')
        for i,mode in enumerate(modes):
            start = 3+110*i
            command(start,'follow_mode',mode=mode)
            phase('long','normal' if i==0 else mode,start,110)
        command(664,'follow_stop'); command(665,'follow_start')
        phase('restart','normal',665,3)
        duration = 669
    elif case=='tracking':
        command(2,'follow_mode',mode='normal'); command(2,'follow_start')
        start = 2
        for kind,duration in (('short_hide',4),('short_return',4),('long_hide',15),('long_return',4),
                              ('object_leave',30),('object_return',30),('sideways',5),('turn',5),('pass',5),('jump',6)):
            phase(kind,'normal',start,duration); start += duration
        command(89.5,'follow_start')
        duration = start+1
    else:
        command(2,'follow_mode',mode='normal'); command(2,'follow_start')
        phase('chase','normal',2,7)
        command(7,'follow_stop',when='ground_moving',latest_ns=9*NS)
        phase('released','normal',9,3)
        command(12,'follow_mode',mode='max'); command(12,'follow_start')
        phase('chase','max',12,14)
        command(24,'follow_stop',when='airborne',latest_ns=26*NS)
        phase('released','max',26,3)
        command(29,'follow_start'); phase('chase','max',29,5)
        duration = 35
    command(duration-0.5,'follow_stop')
    result=dict(schema_version='mc2p.playground-case-plan.v1',revision=4 if case in {'tracking','interrupt','long-session'} else 2,case=case,interval_ns=100_000_000,
        duration_ns=int(duration*NS),phases=phases,owner_commands=sorted(commands,key=lambda c:c['at_ns']),
        leader_separation_distance_blocks=12 if case=='fixed' else None,
        actor_input='lawful_structured_only',leader_schedule='ordinary_client_controls_evaluator_only')
    if case=='interrupt': result['leader_release_distance_blocks']=4
    if case=='navigation': result['leader_near_distance_blocks']=1.1
    if case in {'search','search-missing'}:
        result['fixture_scenario']='playground_search' if case=='search' else 'playground'
        if case=='search-missing': result['revision']=3
    if case=='long-session':
        result['revision']=6
        result['leader_pause_during_look_hold']=True
        result['test_look_hold']=dict(start_ns=200*NS,away_ns=45*NS,return_ns=NS)
        result['leader_return_before_restart']=dict(start_ns=633*NS,distance_blocks=4)
    return result


def validate_case_plan(plan: dict, *, historical=False) -> None:
    expected=case_plan(plan['case'],plan.get('fault',{}).get('kind'))
    if plan==expected: return
    if historical and plan['case']=='long-session' and plan.get('revision') in {4,5}:
        expected['revision']=plan['revision']; expected.pop('leader_pause_during_look_hold')
        if plan['revision']==4: expected['test_look_hold']['away_ns']=3*NS
        if plan==expected: return
    if historical and plan['case']=='search-missing' and plan.get('revision')==2:
        expected.update(revision=2,duration_ns=50*NS)
        expected['phases'][0]['duration_ns']=47*NS
        expected['owner_commands'][-1]['at_ns']=int(49.5*NS)
        if plan==expected: return
    raise ValueError('case plan drift')


def phase_at(plan: dict, elapsed_ns: int) -> dict | None:
    return next((p for p in plan['phases'] if p['start_ns']<=elapsed_ns<p['start_ns']+p['duration_ns']),None)


def pause_long_leader(plan, elapsed_ns):
    if not plan.get('leader_pause_during_look_hold',False): return False
    hold=plan['test_look_hold']
    return hold['start_ns']<=elapsed_ns<hold['start_ns']+hold['away_ns']+hold['return_ns']


def separation_controls(current_distance: float, desired_distance: float) -> MovementV1:
    delta=desired_distance-current_distance
    return MovementV1() if abs(delta)<=.2 else MovementV1(forward=1 if delta>0 else -1,sprint=delta>0)


def waypoint_controls(position, yaw: float, goal: tuple[float,float]) -> tuple[MovementV1,LookV1]:
    import math
    dx,dz=goal[0]-position.x,goal[1]-position.z
    if math.hypot(dx,dz)<=.2: return MovementV1(),LookV1()
    desired=math.degrees(math.atan2(-dx,dz))
    delta=(desired-yaw+180)%360-180
    return MovementV1(forward=1 if abs(delta)<=15 else 0,sprint=abs(delta)<=15),LookV1(max(-15,min(15,delta)),0)


def tracking_goal(kind: str, elapsed_ns: int, position, home: tuple[float,float]):
    x,z=home
    if kind in {'short_hide','short_return'}: return x+3,z+6
    if kind=='long_hide': return x,z+6
    if kind=='long_return':
        return (x+6,z+6) if elapsed_ns<2_000_000_000 else (x+6,z-4)
    if kind=='object_leave': return x+6,z-136
    if kind=='object_return': return x+6,z-4
    raise ValueError('not a tracking waypoint phase')


def tracking_hold(kind: str, elapsed_ns: int, duration_ns: int) -> tuple[bool,bool]:
    movement=kind in {'short_hide','short_return','long_hide','long_return','object_leave','object_return'}
    if kind=='object_return' and elapsed_ns>=duration_ns-NS: movement=False
    look=(kind in {'object_leave','object_return'} or kind=='short_hide' and elapsed_ns>=3*NS
          or kind=='short_return' and elapsed_ns<500_000_000)
    return movement,movement and look


def leader_controls(kind: str, mode: str, elapsed_ns: int) -> tuple[MovementV1,LookV1]:
    if kind=='near': return MovementV1(forward=-1),LookV1()
    if kind in {'stop','static','released','restart'}: return MovementV1(),LookV1()
    if kind=='separate': return MovementV1(forward=1,sprint=True),LookV1()
    if kind in {'chase','rechase'}: return fixed_movement('fast' if mode=='max' else mode),LookV1()
    if kind=='moving': return MovementV1(forward=1 if elapsed_ns//50_000_000%5<2 else 0),LookV1()
    if kind=='long':
        phase = elapsed_ns//NS%55
        return (MovementV1(forward=1 if phase<25 else 0,sneak=mode=='slow' and phase<25),LookV1())
    if kind=='sideways': return MovementV1(strafe=1),LookV1()
    if kind=='turn': return MovementV1(forward=1),LookV1(15 if elapsed_ns<300_000_000 else 0,0)
    if kind=='pass': return MovementV1(forward=-1),LookV1()
    if kind=='jump': return MovementV1(jump=elapsed_ns//50_000_000%30==0),LookV1()
    # Hiding/rebuilding paths require own leader position, supplied only by the evaluator harness.
    if kind in {'short_hide','short_return','long_hide','long_return','object_leave','object_return',
                'enter','corner','search_wait','escape'}:
        return MovementV1(),LookV1()
    raise ValueError('undeclared leader phase')


class PlaygroundCaseRunner:
    """Test-only owner schedule/leader controls/evaluation sampling; follower sees none of it."""
    def __init__(self, directory, session):
        import json
        from mc2p.runtime.segmented_trace import SegmentedJsonlWriter
        self.directory,self.session,self.host = directory,session,session.host
        if self.host.interactive: raise ValueError('automated cases must be headless')
        self.plan = json.loads((directory/'case-plan.json').read_text('utf-8'))
        validate_case_plan(self.plan)
        self.samples = SegmentedJsonlWriter(directory/'case-samples')
        self._start = self._phase = None
        self._elapsed = 0
        self._last_slot = -1
        self._last_observation = -1
        self._track_id = None
        self._leader_source = self.host.leader.register_ordered_source('playground-scripted-leader')
        self._leader_sequence = 0
        self._heading = self.host.leader.observation.yaw_degrees.value
        self._separation_anchor = None
        self._tracking_home = None
        self._phase_identity = None
        self._phase_heading = None
        self._pass_goal = None
        self._hold_source = None
        self._hold_sequence = 0
        self._emitted_triggers=set()
        self._fault_emitted=False
        self._long_hold_yaw=None
        self._long_return_goal=None
        self._search_home=None
        self._quality_controller=None
        if self.plan['case']=='active-world-change':
            import math
            from scripts.active_perception_world_change import WorldChangeLeader,WorldChangeHistory
            from scripts.active_perception_scenarios import quality_clock_bindings
            from scripts.follow_playground_session import write_json_atomic
            observation=self.host.follower.observation
            own=observation.self_state.value.position
            support=(math.floor(own.x),math.floor(own.y)-1,math.floor(own.z)+3)
            self._world_change=WorldChangeLeader(support)
            self._world_history=WorldChangeHistory(support,quality_clock_bindings(
                (self.host.follower,self.host.leader),'world-change-controller-'+directory.name),
                initial_yaw=observation.yaw_degrees.value)
            self._world_yaw=observation.yaw_degrees.value
            self._hold_source=self.host.follower.register_ordered_source('playground-test-hold')
            write_json_atomic(directory/'world-change-history.json',self._world_history.report())
        if self.plan['case']=='active-quality':
            from scripts.active_perception_scenarios import QualityLeaderController, quality_clock_bindings
            from scripts.follow_playground_session import write_json_atomic
            observation=self.host.leader.observation
            position=observation.self_state.value.position
            self._quality_controller=QualityLeaderController(position)
            write_json_atomic(directory/'quality-origin.json',dict(
                schema_version='mc2p.quality-origin.v1',episode_id=observation.episode_id,
                observation_sequence_id=observation.sequence_id,controller_clock_id=observation.controller_clock_id,
                position=[position.x,position.y,position.z],clock_source='time.perf_counter_ns',
                clock_bindings=quality_clock_bindings((self.host.follower,self.host.leader),
                                                      'quality-controller-'+directory.name)))
        if self.plan['case'] in {'search','search-missing'}:
            own=self.host.follower.observation.self_state.value.position
            self._search_home=(own.x,own.z)
        if self.plan['case']=='long-session':
            self._hold_source=self.host.follower.register_ordered_source('playground-test-hold')
        if self.plan['case']=='tracking':
            own=self.host.follower.observation.self_state.value.position
            self._tracking_home=(own.x,own.z)
            self._tracking_yaw=self.host.follower.observation.yaw_degrees.value
            self._hold_source=self.host.follower.register_ordered_source('playground-test-hold')
        self.host.leader_controller = self._leader_step

    def before_tick(self, now_ns: int):
        import json
        path = self.directory/'case-start.json'
        if self._start is None:
            if not path.exists(): return
            start = json.loads(path.read_text('utf-8'))
            if start['case']!=self.plan['case'] or not 0<start['started_at_ns']<=now_ns: raise ValueError('invalid case start')
            self._start = start['started_at_ns']
        self._elapsed = now_ns-self._start
        self._phase = phase_at(self.plan,self._elapsed)
        if self.plan['case']=='long-session': self._hold_long_actor(now_ns)
        if self.plan['case']=='active-world-change': self._hold_world_actor(now_ns)
        if self._tracking_home is not None:
            if self._phase is not None and self._phase['id']!=self._phase_identity:
                import math
                self._phase_identity=self._phase['id']
                self._phase_heading=self.host.leader.observation.yaw_degrees.value
                actor=self.host.follower.observation.self_state.value.position
                leader=self.host.leader.observation.self_state.value.position
                dx,dz=actor.x-leader.x,actor.z-leader.z; length=max(.01,math.hypot(dx,dz))
                self._pass_goal=(actor.x+6*dx/length,actor.z+6*dz/length)
            self._hold_actor(now_ns)
        if self._phase is not None and (self._phase['kind'] in {'separate','near'}
                or self.plan['case']=='interrupt' and self._phase['kind']=='released'):
            # Test fixture control only; never supplied to the follower. The actor is commanded idle here.
            self._separation_anchor=self.host.follower.observation.self_state.value.position
        if self._elapsed>=self.plan['duration_ns']: self.session.stop_requested=True

    def _hold_world_actor(self,now_ns):
        runtime=self.host.follower
        runtime.cancel_source(self._hold_source.source_id)
        hold=self.plan['test_look_hold']
        away=hold['away_start_ns']<=self._elapsed<hold['return_start_ns']
        desired=self._world_yaw+(hold['yaw_offset_degrees'] if away else 0)
        obs=runtime.observation
        yaw_delta=(desired-obs.yaw_degrees.value+180)%360-180
        pitch_delta=32-obs.pitch_degrees.value
        self._submit_actor_hold(now_ns,LookV1(max(-15,min(15,yaw_delta)),max(-10,min(10,pitch_delta))))

    def _long_return_controls(self, leader_position, leader_yaw):
        import math
        declared=self.plan['leader_return_before_restart']
        if self._elapsed<declared['start_ns']: return None
        if self._long_return_goal is None:
            observation=self.host.follower.observation
            position=observation.self_state.value.position
            yaw=math.radians(observation.yaw_degrees.value)
            distance=declared['distance_blocks']
            self._long_return_goal=(position.x-math.sin(yaw)*distance,position.z+math.cos(yaw)*distance)
        # Evaluator-only fixed return waypoint. No actor observation or reference is changed.
        return waypoint_controls(leader_position,leader_yaw,self._long_return_goal)

    def _hold_long_actor(self, now_ns):
        hold=self.plan['test_look_hold']; offset=self._elapsed-hold['start_ns']
        runtime=self.host.follower
        runtime.cancel_source(self._hold_source.source_id)
        if not 0<=offset<hold['away_ns']+hold['return_ns']: return
        yaw=runtime.observation.yaw_degrees.value
        if self._long_hold_yaw is None: self._long_hold_yaw=yaw
        desired=self._long_hold_yaw+(90 if offset<hold['away_ns'] else 0)
        delta=(desired-yaw+180)%360-180
        self._submit_actor_hold(now_ns,LookV1(max(-15,min(15,delta)),0))

    def _submit_actor_hold(self, now_ns, held_look):
        from mc2p.contracts.action import ActionPriorityV0
        from mc2p.contracts.action_v1 import ActionIntentV1
        from mc2p.contracts.intent_source import OrderedIntentV1, ordered_intent_id
        runtime=self.host.follower
        self._hold_sequence+=1
        observation=runtime.observation
        intent=ActionIntentV1(ordered_intent_id(self._hold_source,self._hold_sequence),self._hold_source.source_id,
            observation.episode_id,observation.sequence_id,ActionPriorityV0.PLAYER,now_ns,now_ns+250_000_000,
            movement=MovementV1(),look=held_look,valid_for_ticks=1)
        runtime.submit_ordered_intent(OrderedIntentV1(self._hold_source,self._hold_sequence,intent))

    def _hold_actor(self, now_ns):
        runtime=self.host.follower
        runtime.cancel_source(self._hold_source.source_id)
        phase=self._phase
        if phase is None: return
        movement,look=tracking_hold(phase['kind'],self._elapsed-phase['start_ns'],phase['duration_ns'])
        if not movement: return
        held_look=LookV1() if look else None
        if look and phase['kind'] in {'short_hide','short_return'}:
            desired=self._tracking_yaw+(90 if phase['kind']=='short_hide' else 0)
            delta=(desired-runtime.observation.yaw_degrees.value+180)%360-180
            held_look=LookV1(max(-15,min(15,delta)),0)
        self._submit_actor_hold(now_ns,held_look)

    def _quality_controls(self, runtime):
        if self._phase is None:
            return MovementV1(),LookV1()
        own=runtime.observation.self_state.value
        return self._quality_controller.controls(self._phase,own.position,own.yaw_degrees,self._elapsed)

    def _leader_step(self, runtime, deadline_ns):
        if self.plan['case']=='active-world-change':
            return self._world_change.tick(runtime,self._leader_source,deadline_ns,self._elapsed)
        import time
        from dataclasses import replace
        from mc2p.contracts.action import ActionPriorityV0
        from mc2p.contracts.action_v1 import ActionIntentV1
        from mc2p.contracts.intent_source import OrderedIntentV1, ordered_intent_id
        from scripts.follow_scenarios import follow_task, _neutral
        from mc2p.contracts.behavior import BehaviorProfileV0
        phase = self._phase
        if self._quality_controller is not None:
            controls=self._quality_controls(runtime)
        else:
            controls = (MovementV1(),LookV1()) if phase is None else leader_controls(phase['kind'],phase['mode'],self._elapsed-phase['start_ns'])
        movement,look = controls
        if phase is not None and self._search_home is not None and phase['kind']!='static':
            from scripts.navigation_scenarios import search_goal
            own=runtime.observation.self_state.value
            movement,look=waypoint_controls(own.position,own.yaw_degrees,
                search_goal(phase['kind'],self._elapsed-phase['start_ns'],self._search_home))
        if phase is not None and self._tracking_home is not None:
            kind=phase['kind']; elapsed=self._elapsed-phase['start_ns']
            own=runtime.observation.self_state.value
            if kind in {'short_hide','short_return','long_hide','long_return','object_leave','object_return'}:
                movement,look=waypoint_controls(own.position,runtime.observation.yaw_degrees.value,
                    tracking_goal(kind,elapsed,own.position,self._tracking_home))
            elif kind=='turn':
                delta=(self._phase_heading+90-runtime.observation.yaw_degrees.value+180)%360-180
                look=LookV1(max(-15,min(15,delta)),0)
                movement=MovementV1(forward=1 if abs(delta)<=15 else 0)
            elif kind=='pass':
                movement,look=waypoint_controls(own.position,runtime.observation.yaw_degrees.value,self._pass_goal)
        if phase is not None and (phase['kind'] in {'separate','near'}
                or self.plan['case']=='interrupt' and phase['kind']=='released'):
            import math
            position=runtime.observation.self_state.value.position; anchor=self._separation_anchor
            dx,dz=-math.sin(math.radians(self._heading)),math.cos(math.radians(self._heading))
            separation=(position.x-anchor.x)*dx+(position.z-anchor.z)*dz
            key={'separate':'leader_separation_distance_blocks','near':'leader_near_distance_blocks',
                 'released':'leader_release_distance_blocks'}[phase['kind']]
            distance=self.plan[key]
            movement=separation_controls(separation,distance)
        if phase is not None and phase['kind']=='long':
            elapsed = self._elapsed-phase['start_ns']
            period = elapsed//(55*NS)
            rest = elapsed%(55*NS)>=25*NS
            desired = self._heading+180*(period+(1 if rest else 0))
            delta = (desired-runtime.observation.yaw_degrees.value+180)%360-180
            look = LookV1(max(-15,min(15,delta)),0)
            moving=not rest and abs(delta)<=15
            movement = MovementV1(forward=1 if moving else 0,sneak=moving and phase['mode']=='slow')
            returning=self._long_return_controls(runtime.observation.self_state.value.position,
                runtime.observation.yaw_degrees.value)
            if returning is not None: movement,look=returning
        if pause_long_leader(self.plan,self._elapsed): movement,look=MovementV1(),LookV1()
        now = time.perf_counter_ns(); observation = runtime.observation
        runtime.cancel_source(self._leader_source.source_id)
        self._leader_sequence += 1
        identity = ordered_intent_id(self._leader_source,self._leader_sequence)
        intent = ActionIntentV1(identity,self._leader_source.source_id,observation.episode_id,observation.sequence_id,
            ActionPriorityV0.PLAYER,now,min(deadline_ns,now+250_000_000),movement=movement,look=look,valid_for_ticks=1,movement_requires_look=True)
        runtime.submit_ordered_intent(OrderedIntentV1(self._leader_source,self._leader_sequence,intent))
        return _neutral(runtime,replace(follow_task(deadline_ns),task_id='scripted-leader',task_type='test_leader'),BehaviorProfileV0(),deadline_ns)

    def after_tick(self, now_ns: int):
        import math
        from mc2p.runtime.trace import trace_projection
        from mc2p.skills.follow_tracking import project_playground_view
        if self._start is None: return
        if self.plan['case']=='active-world-change':
            from scripts.follow_playground_session import write_json_atomic
            count=len(self._world_history.markers)
            self._world_history.sample(self.session.driver,self.host.follower.observation,now_ns,now_ns-self._start)
            if len(self._world_history.markers)!=count:
                write_json_atomic(self.directory/'world-change-history.json',self._world_history.report())
        self._interrupt_triggers(now_ns)
        elapsed = now_ns-self._start
        slot = elapsed//self.plan['interval_ns']
        if slot<=self._last_slot or elapsed>=self.plan['duration_ns']: return
        self._last_slot = slot
        phase = phase_at(self.plan,slot*self.plan['interval_ns'])
        if phase is None: return
        runtime = self.host.follower; observation = runtime.observation
        if observation.sequence_id<=self._last_observation:
            return  # Keep this slot missing in the fixed denominator; never reuse an old observation.
        self._last_observation=observation.sequence_id
        view = project_playground_view(observation,now_ns,observation.controller_clock_id)
        driver = self.session.driver
        if driver is not None and driver.follower.last_decision is not None:
            self._track_id = driver.follower.last_decision.target.track_id
        if self._track_id is None:
            players=[e for e in view.base.entities if e.entity_type=='minecraft:player']
            if len(players)==1: self._track_id=players[0].track_id
        target = next((e for e in view.base.entities if e.track_id==self._track_id),None)
        result = self.host.last_actor_result
        if result is None or result.observation.sequence_id!=observation.sequence_id: raise ValueError('case needs matching actual dispatch')
        position = view.base.own.position
        decision = None if driver is None else driver.follower.last_decision
        self.samples.write(dict(slot=slot,phase_id=phase['id'],episode_id=observation.episode_id,
            observation_sequence_id=observation.sequence_id,observed_at_ns=observation.received_at_monotonic_ns,
            sampled_at_ns=now_ns,position=[position.x,position.y,position.z],movement=trace_projection(result.decision.action.movement),
            target_track_id=self._track_id,target_visible=target is not None,
            distance=None if target is None else math.hypot(target.position.x-position.x,target.position.z-position.z),
            requested_mode=self.session.mode,selected_gait=None if decision is None else decision.selected_gait,
            state=self.session.state,reason=self.session.reason,task_id=None if driver is None else driver.follower.task_id,
            attempt_id=None if driver is None else driver.attempt_id,source_generation=None if driver is None else driver.source.generation,
            intent_sequence=0 if driver is None else driver.sequence,retained_source_slots=runtime.ordered_source_stats['retained_slots']))

    def _interrupt_triggers(self, now_ns):
        if self.plan['case']!='interrupt': return
        import time
        from mc2p.runtime.trace import trace_projection
        from scripts.follow_playground_session import write_json_atomic
        from scripts.follow_playground_faults import eligible_stop
        result=self.host.last_actor_result
        if result is None: return
        observation=self.host.follower.observation
        if result.observation.sequence_id!=observation.sequence_id: raise ValueError('trigger dispatch mismatch')
        movement=trace_projection(result.decision.action.movement)
        motion=self.host.motion_status(now_ns).get('actual_motion','unknown')
        elapsed=now_ns-self._start
        for index,command in enumerate(self.plan['owner_commands']):
            if 'when' not in command or index in self._emitted_triggers: continue
            if elapsed>command['latest_ns']: raise TimeoutError('declared stop condition did not occur')
            if elapsed>=command['at_ns'] and eligible_stop(command['when'],movement,motion):
                write_json_atomic(self.directory/f'command-trigger-{index:02}.json',dict(
                    schema_version='mc2p.playground-command-trigger.v1',session_id=self.directory.name,
                    command_index=index,when=command['when'],sampled_at_ns=now_ns,
                    observation_sequence_id=observation.sequence_id))
                self._emitted_triggers.add(index)
        fault=self.plan.get('fault')
        if fault is None or self._fault_emitted: return
        if elapsed>fault['latest_ns']: raise TimeoutError('declared moving fault condition did not occur')
        if elapsed>=fault['earliest_ns'] and movement['forward']==1:
            proof=dict(schema_version='mc2p.playground-fault-trigger.v1',session_id=self.directory.name,
                kind=fault['kind'],sampled_at_ns=now_ns,observation_sequence_id=observation.sequence_id)
            write_json_atomic(self.directory/'fault-trigger.json',proof)
            self._fault_emitted=True
            if fault['kind']=='worker_stall':
                write_json_atomic(self.directory/'fault-injection.json',dict(proof,injected_at_ns=time.perf_counter_ns()))
                time.sleep(fault['stall_duration_ns']/NS)
                write_json_atomic(self.directory/'fault-resumed.json',dict(resumed_at_ns=time.perf_counter_ns()))

    def close(self):
        try:
            if self._hold_source is not None and self.host.follower.state.value=='ready':
                self.host.follower.cancel_source(self._hold_source.source_id)
                self.host.follower.unregister_ordered_source(self._hold_source)
            if self.host.leader.state.value=='ready':
                self.host.leader.cancel_source(self._leader_source.source_id)
                self.host.leader.unregister_ordered_source(self._leader_source)
        finally:
            self.host.leader_controller = None
            self.samples.close()
            if self.plan['case']=='active-world-change':
                self._world_change.gaze.clear('case_closed')
