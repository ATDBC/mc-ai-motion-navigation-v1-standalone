"""Streaming M3 request audit from formal pre-observations, not fixture truth."""
from collections import Counter
from dataclasses import asdict
import json
import math
from mc2p.backends.client_observation_payload import decode_client_observation_payload, snapshot_v2_from_payload
from mc2p.runtime.trace import trace_projection
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.local_navigation import plan_local_route
from mc2p.skills.navigation_evidence import project_navigation_evidence
from mc2p.skills.navigation_memory import NavigationMemory
from mc2p.skills.navigation_motion import NavigationBlockMap, check_navigation_motion
from mc2p.skills.perception_needs import PerceptionConfig
from scripts.target_search_evidence import SearchRequestAudit


def valid_navigation_combination(movement, look, variant):
    """Check only the versioned control combination; evidence is checked later."""
    if movement['forward']!=1 or movement['strafe']!=0:
        return False
    if variant=='active_perception_v1':
        return look['yaw_delta_degrees']==0
    return look['yaw_delta_degrees']==0 and look['pitch_delta_degrees']==0


def _perception_variant(context):
    """Reject direct callers that try to enable the new rule from diagnostics alone."""
    variant=context.get('perception_variant','m6_baseline')
    event=context.get('active_perception')
    if variant=='m6_baseline':
        if event is not None:
            raise ValueError('historical M6 navigation gained active perception metadata')
        return variant
    config=context.get('perception_config')
    if variant not in {'smooth_only','active_perception_v1'} or type(config) is not dict:
        raise ValueError('navigation perception variant/config missing')
    try:
        expected=asdict(PerceptionConfig(**config))
    except (TypeError,ValueError) as error:
        raise ValueError('navigation perception config invalid') from error
    fields={'schema_version','task_id','attempt_id','episode_id','source_generation',
            'intent_sequence','observation_sequence_id','variant','config','perception',
            'planning_elapsed_ns','auto_target_distance_blocks'}
    if (config!=expected or type(event) is not dict or set(event)!=fields
            or event['schema_version']!='mc2p.active-perception-step.v1'
            or event['variant']!=variant or event['config']!=config
            or type(event['perception']) is not dict
            or any(event[name]!=context.get(name) for name in (
                'task_id','attempt_id','episode_id','source_generation','intent_sequence',
                'observation_sequence_id'))):
        raise ValueError('navigation perception metadata is not bound to this request')
    return variant


def restore_snapshot(raw):
    """Restore a declared version exactly; old V2 is readable, never upgraded."""
    if type(raw) is not dict:
        raise ValueError('formal snapshot must be an object')
    try:
        if raw.get('schema_version')=='mc2p.observation.v3':
            return _restore_v3_snapshot(raw)
        if raw.get('schema_version')!='mc2p.observation.v2':
            raise ValueError('unsupported formal snapshot version')
        return _restore_v2_snapshot(raw)
    except (KeyError,TypeError,IndexError) as error:
        raise ValueError('malformed versioned formal snapshot') from error


def _same_trace_value(actual, expected):
    """JSON value equality without Python's bool/int aliasing.

    Both int and float are legal finite coordinate scalars in typed V3, so
    preserve their numeric equivalence without coercion or rounding.
    """
    if type(actual) is not type(expected):
        return type(actual) in (int,float) and type(expected) in (int,float) and actual==expected
    if type(actual) is dict:
        return actual.keys()==expected.keys() and all(
            _same_trace_value(actual[key],expected[key]) for key in actual)
    if type(actual) is list:
        return len(actual)==len(expected) and all(_same_trace_value(a,b) for a,b in zip(actual,expected))
    return actual==expected


def _restore_v3_snapshot(raw):
    from mc2p.backends.client_observation_payload_v3 import (
        decode_client_observation_payload_v3, snapshot_v3_from_payload,
    )
    payload={k:raw[k] for k in ('client_sample','self_state','inventory','gui','perception',
                                'field_profile','targeting','tracked_entity')}
    payload=json.loads(json.dumps(payload,allow_nan=False))
    payload.update(schema_version='mc2p.client_observation.v3',generation_id=raw['sequence_id'],
                   sample_world_tick=raw['world_time_ticks']['value'])
    if payload['gui']['value'] is not None:
        payload['gui']['value']['properties']=[dict(property_id=index,value=value)
            for index,value in payload['gui']['value']['properties']]
    if payload['perception']['value'] is not None:
        for block in payload['perception']['value']['blocks']:
            block['collision']['boxes']=[[box[k] for k in ('min_x','min_y','min_z','max_x','max_y','max_z')]
                for box in block['collision']['boxes']]
        for entity in payload['perception']['value']['visible_entities']:
            entity['equipment']=[dict(slot=slot,item=item) for slot,item in entity['equipment']]
    obs=snapshot_v3_from_payload(decode_client_observation_payload_v3(json.dumps(payload,allow_nan=False).encode()),
        **{k:raw[k] for k in ('episode_id','request_sequence_id','request_started_at_monotonic_ns',
            'received_at_monotonic_ns','controller_clock_id','source_backend')},
        privileged_fields_present=tuple(raw['privileged_fields_present']))
    if not _same_trace_value(trace_projection(obs),raw):
        raise ValueError('formal V3 snapshot changed during navigation replay')
    return obs


def _restore_v2_snapshot(raw):
    """Historical version only; not eligible for the current NavigationMemory."""
    payload={k:raw[k] for k in ('client_sample','self_state','inventory','gui','perception')}
    payload=json.loads(json.dumps(payload))
    payload.update(schema_version='mc2p.client_observation.v2',generation_id=raw['sequence_id'],
                   sample_world_tick=raw['world_time_ticks']['value'])
    if payload['gui']['value'] is not None:
        payload['gui']['value']['properties']=[dict(property_id=index,value=value)
            for index,value in payload['gui']['value']['properties']]
    if payload['perception']['value'] is not None:
        for ray in payload['perception']['value']['block_rays']:
            ray['state_properties']=[dict(name=name,value=value) for name,value in ray['state_properties']]
        for entity in payload['perception']['value']['visible_entities']:
            entity['equipment']=[dict(slot=slot,item=item) for slot,item in entity['equipment']]
    snapshot=snapshot_v2_from_payload(decode_client_observation_payload(json.dumps(payload).encode()),
        **{k:raw[k] for k in ('episode_id','request_sequence_id','request_started_at_monotonic_ns',
            'received_at_monotonic_ns','controller_clock_id','source_backend')},
        privileged_fields_present=tuple(raw['privileged_fields_present']))
    projected = trace_projection(snapshot)
    if (raw['perception']['value'] is not None
            and 'sensor_profile_revision' not in raw['perception']['value']):
        # Strict decoding has already required the original 90x60 profile.
        # Compare its historical representation without mutating the raw input.
        projected['perception']['value'].pop('sensor_profile_revision')
    if projected!=raw: raise ValueError('formal snapshot changed during navigation replay')
    return snapshot


class NavigationRequestAudit:
    def __init__(self, *, required=False, search_required=False, search_window=None):
        self.required=required
        self.search=SearchRequestAudit(required=search_required,window=search_window)
        self.identity=self.local=None
        self.memory=None
        self.floor=None
        self.counts=Counter()
        self.max_retreat_displacement=0.

    def observe(self, pre, context):
        decision=context['decision']; now=context['decision_time_ns']
        if decision.get('navigation_mode')!='m2_local_v1':
            if self.required: raise ValueError('formal task missing M3 navigation mode')
            return
        if (pre is None or pre['sequence_id']!=context['observation_sequence_id']
                or pre['episode_id']!=context['episode_id']):
            raise ValueError('navigation request lacks its formal pre-observation')
        variant=_perception_variant(context)
        self.counts['requests']+=1
        identity=(context['task_id'],context['attempt_id'],context['episode_id'])
        if identity!=self.identity:
            self.identity=identity; self.local=None
            self.memory=NavigationMemory(context['attempt_id']); self.floor=None
        from scripts.block_observation_v3_evidence import require_navigation_snapshot_v3
        obs=require_navigation_snapshot_v3(pre)
        snapshot=self.memory.observe(obs,now_ns=now,controller_clock_id=obs.controller_clock_id,
                                     scope_id=context['attempt_id'])
        view=project_playground_view(obs,now,obs.controller_clock_id)
        if view.base.own is not None and view.base.own.on_ground:
            self.floor=round(view.base.own.position.y)-1
        context['verified_retreat_terminal']=False
        if (self.local is not None and decision.get('local_retreat') is None
                and not any(decision['movement'].values()) and not any(decision['look'].values())
                and view.base.available and view.base.own is not None
                and 0<=now-view.base.request_start_ns<=500_000_000):
            own=view.base.own.position
            age=now-self.local['started_at_ns']
            displacement=math.hypot(own.x-self.local['origin']['x'],own.z-self.local['origin']['z'])
            expired=age>=2_000_000_000 or displacement>1.5
            reached=math.hypot(own.x-self.local['goal']['x'],own.z-self.local['goal']['z'])<=.2
            context['verified_retreat_terminal']=(age>=0 and (
                decision['reason']=='local_retreat_expired' and expired
                or decision['reason']=='local_waypoint_reached' and not expired and reached))
        self.search.observe(snapshot,view,context,self.floor)
        movement,look=decision['movement'],decision['look']
        moving=any(movement.values())
        local=decision.get('local_retreat')
        context['verified_local_retreat']=False
        if not moving and local is None:
            self.local=None
            if any(look.values()): self.counts['neutral_look_requests']+=1
            return
        own=pre['self_state']['value']
        if own is None or not 0<=now-pre['request_started_at_monotonic_ns']<=500_000_000:
            raise ValueError('navigation request lacks fresh self evidence')
        if moving and not valid_navigation_combination(movement,look,variant):
            raise ValueError('navigation movement turned or used an invalid versioned combination')
        target=decision['target']
        if moving:
            guard=check_navigation_motion(snapshot,view,now,self.floor,view.base.own.yaw,
                jump=movement['jump'] or not view.base.own.on_ground,
                allowed_player_contact=target['track_id'] if decision['requested_mode']!='auto' and local is None else None)
            if guard: raise ValueError('navigation request lacks fresh motion checks: '+guard)
        if local is None:
            self.local=None
            if moving and target['status']!='visible' and not context.get('verified_search'):
                raise ValueError('ordinary chase lost target but moved')
            self.counts['approach_requests']+=int(moving)
            return
        if self.local is None:
            if (local['observation_sequence_id']!=pre['sequence_id'] or local['started_at_ns']!=now
                    or local['origin']!=own['position'] or target['status']!='visible'):
                raise ValueError('local retreat not created from current legal target')
            visible=pre['perception']['value']['visible_entities']
            entity=next((e for e in visible if e['track_id']==target['track_id']),None)
            if entity is None: raise ValueError('local retreat source target not legally visible')
            relative=entity['relative_position']; length=math.hypot(relative['x'],relative['z'])
            # Gait/distance configuration belongs to the follower and owner
            # contract, not a second hard-coded two-block policy in the audit.
            if length<.01: raise ValueError('local retreat source target overlaps own position')
            expected=dict(x=own['position']['x']-relative['x']/length,y=own['position']['y'],
                          z=own['position']['z']-relative['z']/length)
            if any(abs(local['goal'][k]-expected[k])>1e-6 for k in expected):
                raise ValueError('local retreat goal not one block away from legal target')
            # Copy scalar data; a caller cannot mutate the frozen audit anchor.
            self.local=dict(local,origin=dict(local['origin']),goal=dict(local['goal']))
            self.counts['local_intents']+=1
        if local!=self.local: raise ValueError('local retreat changed its frozen evidence/goal')
        distance=math.hypot(own['position']['x']-local['origin']['x'],own['position']['z']-local['origin']['z'])
        if not 0<=now-local['started_at_ns']<2_000_000_000 or distance>1.5:
            raise ValueError('local retreat exceeded time or displacement bound')
        if moving:
            goal=Vec3V0(**local['goal'])
            route=plan_local_route(NavigationBlockMap(snapshot),view.base,goal,now,
                                   support_freshness_ns=60_000_000_000)
            waypoint=Vec3V0(route.cells[1][0]+.5,self.floor+1,route.cells[1][1]+.5) if len(route.cells)>1 else goal
            dx,dz=waypoint.x-own['position']['x'],waypoint.z-own['position']['z']
            desired=math.degrees(math.atan2(-dx,dz))
            if abs((desired-pre['yaw_degrees']['value']+180)%360-180)>8:
                raise ValueError('local retreat moved without facing checked local goal')
            if decision['reason']!='local_retreat_segment' or movement!=dict(forward=1,strafe=0,jump=False,sneak=False,sprint=False):
                raise ValueError('local retreat altered its declared normal local gait')
            self.counts['local_movement_requests']+=1
        elif any(look.values()): self.counts['neutral_local_look_requests']+=1
        context['verified_local_retreat']=True

    def metrics(self):
        return dict(self.counts,max_retreat_displacement_blocks=self.max_retreat_displacement,search=self.search.metrics())

    def release(self):
        self.search.release()

    def executed(self, pre, context, post, action, receipt):
        """Positive counts require actual arbitration, receipt and post-sample."""
        context['verified_navigation_execution']=False
        decision=context['decision']
        if decision.get('navigation_mode')!='m2_local_v1': return
        identity=context['accepted_intent_id']
        from scripts.block_observation_v3_evidence import require_navigation_snapshot_v3
        post_snapshot=require_navigation_snapshot_v3(post)
        post_view=project_playground_view(post_snapshot,post['received_at_monotonic_ns'],post['controller_clock_id'])
        post_evidence=project_navigation_evidence(post_snapshot,
            now_ns=post['received_at_monotonic_ns'],controller_clock_id=post['controller_clock_id'])
        if any(context['selected'].get(group)!=identity for group in ('movement','look')):
            self.search.feedback(False,post_evidence,post_view,context,pre)
            return
        if (receipt['status'] not in {'executed','confirmed_local'}
                or receipt['episode_id']!=post['episode_id'] or receipt['generation_id']!=post['sequence_id']
                or receipt['request_sequence_id']!=action['request_sequence_id']
                or post['sequence_id']!=pre['sequence_id']+1
                or post['client_sample']['clock_id']!=pre['client_sample']['clock_id']
                or post['client_sample']['started_at_monotonic_ns']<pre['client_sample']['completed_at_monotonic_ns']
                or not 0<=post['received_at_monotonic_ns']-context['decision_time_ns']<=500_000_000):
            self.search.feedback(False,post_evidence,post_view,context,pre)
            return
        if any(action[g]!=decision[g] for g in ('movement','look')):
            raise ValueError('selected navigation action differs from request')
        context['verified_navigation_execution']=True
        self.search.feedback(True,post_evidence,post_view,context,pre)
        local=decision.get('local_retreat')
        if local is None: return
        moving=any(action['movement'].values())
        if not moving and any(action['look'].values()):
            yaw=(pre['yaw_degrees']['value']+action['look']['yaw_delta_degrees']+180)%360-180
            pitch=max(-90,min(90,pre['pitch_degrees']['value']+action['look']['pitch_delta_degrees']))
            if (abs((post['yaw_degrees']['value']-yaw+180)%360-180)<=1
                    and abs(post['pitch_degrees']['value']-pitch)<=1):
                self.counts['executed_neutral_local_looks']+=1
        if moving and context.get('verified_local_retreat'):
            self.counts['executed_local_movements']+=1
            position=post['self_state']['value']['position']
            distance=math.hypot(position['x']-local['origin']['x'],position['z']-local['origin']['z'])
            self.max_retreat_displacement=max(self.max_retreat_displacement,distance)
