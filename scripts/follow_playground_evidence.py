"""Post-run streaming checks. Diagnostic/evaluator truth never crosses the actor boundary."""
from collections import Counter
from dataclasses import asdict
import json
import math
from pathlib import Path
import uuid
from itertools import zip_longest
from mc2p.runtime.segmented_trace import iter_segmented_jsonl, _safe_path
from mc2p.runtime.trace import trace_projection
from mc2p.skills.follow_gaits import fixed_movement
from mc2p.skills.perception_needs import OWNERS, PerceptionConfig
from scripts.follow_playground_scenarios import case_plan, phase_at, validate_case_plan
from scripts.follow_playground_faults import eligible_stop, StopEvidence, expected_fault_outcome, fault_outer_completed
from scripts.streaming_time_evidence import export_segmented_runtime_time_evidence


def validate_scheduled_sample(row, observation, plan, started_at_ns, previous_sequence):
    sampled = row['sampled_at_ns']
    received = observation['received_at_monotonic_ns']
    if (type(sampled) is not int or row['observed_at_ns']!=received
            or not 0<=sampled-received<=250_000_000
            or observation['sequence_id']<=previous_sequence
            or row['observation_sequence_id']!=observation['sequence_id']
            or row['slot']!=(sampled-started_at_ns)//plan['interval_ns']):
        raise ValueError('scheduled sample lacks fresh unique formal observation or actual slot time')
    phase = phase_at(plan,sampled-started_at_ns)
    if phase is None or row['phase_id']!=phase['id']:
        raise ValueError('scheduled sample phase does not match actual time')


def validate_owner_events(events, plan, started_at_ns, session_id):
    """Only a bounded declared command timeline is retained, not the session's heartbeats."""
    expected = plan['owner_commands']
    if len(expected)>64: raise ValueError('case command plan exceeds evaluator bound')
    pending,completed = {},[]
    sent_count,last_sequence = 0,0
    for event in events:
        if event['kind']=='sent':
            command = event['command']; sequence = command['sequence']
            if sent_count>=len(expected) or type(sequence) is not int or sequence<=last_sequence:
                raise ValueError('unexpected or duplicated owner command')
            declared = expected[sent_count]
            raw = dict(schema_version='mc2p.demo-command.v1',sequence=sequence,
                       **{k:v for k,v in declared.items() if k not in {'at_ns','when','latest_ns'}})
            sent = event['sent_at_ns']
            window=declared['latest_ns']-declared['at_ns']+250_000_000 if 'when' in declared else 500_000_000
            if command!=raw or type(sent) is not int or not 0<=sent-started_at_ns-declared['at_ns']<=window:
                raise ValueError('owner command does not match declared content or schedule')
            pending[sequence] = dict(command=command,sent_at_ns=sent,accepted=False)
            sent_count += 1; last_sequence = sequence
        elif event['kind']=='reply':
            reply = event['reply']; received = event['received_at_ns']
            item = pending.get(reply['sequence'])
            if (item is None or reply['schema_version']!='mc2p.demo-reply.v1' or reply['session_id']!=session_id
                    or type(received) is not int or not 0<=received-item['sent_at_ns']<=5_000_000_000):
                raise ValueError('unassociated owner reply or expired command')
            if reply['phase']=='accepted' and not item['accepted']:
                item['accepted']=True; item['accepted_at_ns']=received
            elif reply['phase']=='applied' and item['accepted'] and received>=item['accepted_at_ns']:
                status=reply['status']; command=item['command']
                if command['kind']=='follow_mode' and status['requested_mode']!=command['mode']:
                    raise ValueError('applied mode reply did not apply requested mode')
                if command['kind']=='follow_start' and (not status['task_id'] or not status['attempt_id']):
                    raise ValueError('applied start lacks actual task identity')
                if command['kind']=='follow_stop' and status.get('task_id') is not None:
                    raise ValueError('applied stop retained a task')
                completed.append(dict(item,applied_at_ns=received,status=status))
                del pending[reply['sequence']]
            else: raise ValueError('planned command not accepted then applied')
        else: raise ValueError('unexpected owner event kind')
    if pending or sent_count!=len(expected) or len(completed)!=len(expected):
        raise ValueError('incomplete planned command acknowledgments')
    return sorted(completed,key=lambda item:item['command']['sequence'])


def validate_owner_configuration(owner, observed_at_ns, requested_mode):
    # The newest command supersedes old settled state even while its own reply is in flight.
    sent=[c for c in owner if c['sent_at_ns']<=observed_at_ns]
    control=next((c for c in reversed(sent) if c['command']['kind'] in {'follow_start','follow_stop'}),None)
    if control is not None and control['applied_at_ns']<=observed_at_ns-500_000_000 and control['command']['kind']=='follow_stop':
        raise ValueError('Runtime task remained active after applied stop')
    config=next((c for c in reversed(sent) if c['command']['kind']=='follow_mode'),None)
    if config is not None and config['applied_at_ns']<=observed_at_ns-500_000_000 and requested_mode!=config['command']['mode']:
        raise ValueError('Runtime mode differs from settled owner configuration')


def validate_owner_distance(owner, observed_at_ns, target_distance, *, task_id=None, attempt_id=None):
    applied=[item for item in owner if item['applied_at_ns']<=observed_at_ns]
    current=max(applied,key=lambda item:item['applied_at_ns']) if applied else None
    if current is None and type(task_id) is str and type(attempt_id) is str:
        # The synchronous _start handler runs its first Driver step before
        # returning the applied reply. A later-delivered reply can therefore
        # prove THIS exact task's initial config, never an arbitrary future one.
        starts=[item for item in owner if item['command']['kind']=='follow_start'
                and item['sent_at_ns']<=observed_at_ns
                and item['status'].get('task_id')==task_id
                and item['status'].get('attempt_id')==attempt_id]
        if len(starts)==1:
            current=starts[0]
    if current is None or 'auto_distance_blocks' not in current['status']:
        raise ValueError('active perception distance lacks applied owner configuration')
    expected=current['status']['auto_distance_blocks']
    if (type(expected) not in (int,float) or not math.isfinite(expected)
            or float(expected)!=target_distance):
        raise ValueError('active perception distance differs from applied owner configuration')


_PERCEPTION_STATUS_KEYS = frozenset({'reason','no_progress','variant','config_revision',
    'candidate_id','candidate_kind','candidate_count','need_count','score','boundary_events','planning_ns','owner',
    'requested_yaw_degrees','requested_pitch_degrees','motion_guard_reason','task_gaze_debt'})


def _perception_status(value, variant, config):
    if type(value) is not dict or not set(value)<=_PERCEPTION_STATUS_KEYS:
        raise ValueError('active perception status exposed unsupported data')
    result=json.loads(json.dumps(value,ensure_ascii=False,allow_nan=False))
    for name in ('reason','candidate_id','candidate_kind'):
        item=result.get(name)
        if item is not None and (type(item) is not str or not 0<len(item)<=128):
            raise ValueError('active perception status identifier invalid')
    if 'owner' in result and result['owner'] not in OWNERS:
        raise ValueError('active perception status owner invalid')
    for name in ('requested_yaw_degrees','requested_pitch_degrees'):
        angle=result.get(name)
        if angle is not None and (type(angle) not in (int,float) or not math.isfinite(angle)):
            raise ValueError('active perception requested angle invalid: '+name)
    pitch=result.get('requested_pitch_degrees')
    if pitch is not None and not -90<=pitch<=90:
        raise ValueError('active perception requested pitch outside camera range')
    guard=result.get('motion_guard_reason')
    if guard is not None and (type(guard) is not str or not 0<len(guard)<=128):
        raise ValueError('active perception motion guard reason invalid')
    for name,limit in (('no_progress',2**31-1),('candidate_count',16),('need_count',8),
                       ('planning_ns',config['planning_budget_ns'])):
        item=result.get(name)
        if item is not None and (type(item) is not int or not 0<=item<=limit):
            raise ValueError('active perception status bound invalid: '+name)
    if ('variant' in result and result['variant']!=variant
            or 'config_revision' in result and result['config_revision']!=config['revision']):
        raise ValueError('active perception status variant/config mismatch')
    score=result.get('score')
    if score is not None and (type(score) not in (int,float) or not math.isfinite(score)):
        raise ValueError('active perception status score invalid')
    if (config['revision']>=4 and result.get('reason')=='selected_task_fragment'
            and 'task_gaze_debt' not in result):
        raise ValueError('selected task fragment requires task gaze debt')
    if 'task_gaze_debt' in result:
        debt=result['task_gaze_debt']
        if type(debt) not in (int,float) or debt not in (0.,1.):
            raise ValueError('active perception task gaze debt invalid')
    events=result.get('boundary_events')
    if events is not None and (type(events) is not list or len(events)>8
            or any(type(item) is not str or not 0<len(item)<=128 for item in events)):
        raise ValueError('active perception boundary events invalid')
    return result


def _perception_configuration(variant, config):
    if variant=='m6_baseline':
        if config is None: return {'revision':1}
        if config!={'revision':1}: raise ValueError('historical M6 perception config changed')
        return dict(config)
    if variant not in {'smooth_only','active_perception_v1'} or type(config) is not dict:
        raise ValueError('new perception audit requires an explicit implemented variant/config')
    try:
        expected=asdict(PerceptionConfig(**config))
    except (TypeError,ValueError) as error:
        raise ValueError('new perception audit config is invalid') from error
    if config!=expected:
        raise ValueError('new perception audit requires the complete frozen config')
    return expected


def _active_event(payload, task, envelope, source, variant, config):
    fields={'schema_version','task_id','attempt_id','episode_id','source_generation',
            'intent_sequence','observation_sequence_id','variant','config','perception',
            'planning_elapsed_ns','auto_target_distance_blocks'}
    if type(payload) is not dict or set(payload)!=fields:
        raise ValueError('active perception event schema fields changed')
    if payload['schema_version']!='mc2p.active-perception-step.v1':
        raise ValueError('active perception event schema changed')
    if task is None or envelope is None or source is None:
        raise ValueError('active perception event lacks accepted task/source')
    if (any(payload[name]!=task[name] for name in ('task_id','attempt_id','episode_id','source_generation',
                                                   'intent_sequence','observation_sequence_id'))
            or payload['intent_sequence']!=envelope['sequence']
            or payload['observation_sequence_id']!=envelope['intent']['observation_sequence_id']
            or payload['episode_id']!=source['episode_id']
            or payload['source_generation']!=source['generation']
            or payload['variant']!=variant or payload['config']!=config):
        raise ValueError('active perception event is not bound to its accepted task/config')
    elapsed=payload['planning_elapsed_ns']
    if (type(elapsed) is not list or len(elapsed)>8
            or any(type(item) is not int or not 0<=item<=250_000_000 for item in elapsed)
            or elapsed and (elapsed[0]!=0 or any(after<before for before,after in zip(elapsed,elapsed[1:])))
            or variant=='smooth_only' and elapsed):
        raise ValueError('active perception planning elapsed sequence invalid')
    distance=payload['auto_target_distance_blocks']
    if type(distance) not in (int,float) or not math.isfinite(distance) or not 1.5<=distance<=6:
        raise ValueError('active perception auto target distance invalid')
    status=_perception_status(payload['perception'],variant,config)
    if (variant=='active_perception_v1' and status.get('reason') in {
            'selected_task_fragment','planning_budget_exhausted'} and not elapsed):
        raise ValueError('active perception status does not match its planning elapsed sequence')
    return dict(payload,config=dict(config),perception=status,
                planning_elapsed_ns=list(elapsed),auto_target_distance_blocks=float(distance))


def _actor_steps(directory, tracking_audit=None, long_audit=None, navigation_audit=None, *,
                 perception_variant='m6_baseline', perception_config=None):
    """Bind task records to accepted ordered intents and actual following Runtime steps."""
    from scripts.block_observation_v3_evidence import require_navigation_snapshot_v3
    perception_config=_perception_configuration(perception_variant,perception_config)
    source = lease = task = envelope = active = None
    previous_intent = 0
    previous_observation=None
    for row in iter_segmented_jsonl(directory/'trace'):
        kind,payload = row['record_type'],row['payload']
        if kind=='ordered_source_registered' and payload['label']=='playground-follow':
            if source is not None: raise ValueError('overlapping playground sources')
            source=payload['source']; lease=task=envelope=active=None; previous_intent=0
        elif kind=='ordered_source_unregistered' and payload['source']==source:
            if task is not None or active is not None: raise ValueError('unconsumed perception task before source release')
            source=lease=task=envelope=active=None
        elif kind=='playground_control_release' and navigation_audit is not None:
            navigation_audit.release()
        elif kind=='task_lease_renewed':
            if (source is None or payload['episode_id']!=source['episode_id']
                    or payload['source_generation']!=source['generation']
                    or not 0<payload['owner_deadline_ns']-payload['renewed_at_ns']<=3_000_000_000
                    or payload['previous_owner_deadline_ns']!=(0 if lease is None else lease['owner_deadline_ns'])):
                raise ValueError('unassociated or unbounded task owner lease')
            lease=payload
        elif kind=='ordered_intent' and source is not None and payload['envelope']['source']==source:
            if task is not None or active is not None: raise ValueError('unconsumed perception task before next intent')
            envelope=payload['envelope']
            if envelope['sequence']!=previous_intent+1: raise ValueError('playground ordered intent gap/replay')
            previous_intent=envelope['sequence']
        elif kind=='playground_task':
            if (task is not None or active is not None
                    or source is None or lease is None or envelope is None
                    or any(payload[k]!=lease[k] for k in ('task_id','attempt_id','episode_id','source_generation','owner_deadline_ns'))
                    or payload['intent_sequence']!=envelope['sequence']
                    or payload['observation_sequence_id']!=envelope['intent']['observation_sequence_id']):
                raise ValueError('playground task lacks accepted intent/owner lease association')
            task=payload; active=None
        elif kind=='active_perception':
            if perception_variant=='m6_baseline':
                raise ValueError('historical M6 trace gained an active perception event')
            if active is not None:
                raise ValueError('duplicate active perception event')
            active=_active_event(payload,task,envelope,source,perception_variant,perception_config)
        elif kind in {'reset','step','close_release'}:
            observation=payload['result']['observation'] if kind=='reset' else payload['backend_result']['observation']
            require_navigation_snapshot_v3(observation)
            if kind=='reset' and long_audit is not None: long_audit.reset(observation['episode_id'])
            action=None if kind=='reset' else payload['decision']['action'] if kind=='step' else payload['action']
            context=None
            if kind=='step' and task is not None and payload['task']['task_type']=='playground_follow':
                if perception_variant!='m6_baseline' and active is None:
                    raise ValueError('new perception step lacks its versioned event')
                intent=envelope['intent']; runtime_task=payload['task']
                if (task['observation_sequence_id']+1!=observation['sequence_id']
                        or runtime_task['task_id']!=task['task_id']
                        or runtime_task['deadline_monotonic_ns']>lease['owner_deadline_ns']
                        or not 0<intent['expires_at_monotonic_ns']-intent['submitted_at_monotonic_ns']<=250_000_000
                        or intent['expires_at_monotonic_ns']>lease['owner_deadline_ns']):
                    raise ValueError('task dispatch does not match its bounded lease/observation')
                selected=dict(payload['decision']['selected_intents'])
                context=dict(task,selected=selected,accepted_intent_id=intent['intent_id'],
                    decision_time_ns=intent['submitted_at_monotonic_ns'],
                    control_deadline_ns=intent['expires_at_monotonic_ns'],
                    perception_variant=perception_variant,perception_config=dict(perception_config),
                    active_perception=active)
                if any(intent[group]!=task['decision'][group] for group in ('movement','look')):
                    raise ValueError('recorded controller decision differs from submitted intent')
                if navigation_audit is not None:
                    navigation_audit.observe(previous_observation,context)
                    navigation_audit.executed(previous_observation,context,observation,action,
                                              payload['backend_result']['receipt'])
                if tracking_audit is not None: tracking_audit.observe(context,context['decision_time_ns'])
                if long_audit is not None:
                    receipt=payload['backend_result']['receipt']
                    if receipt['status'] not in {'executed','confirmed_local','cancelled'}:
                        raise ValueError('long task lacks confirmed actual step receipt')
                    context['source_scope_id']=source['scope_id']
                    long_audit.observe(context,context['decision_time_ns'])
                task=envelope=active=None
            previous_observation=observation
            yield observation,action,context
    if task is not None or active is not None:
        raise ValueError('unconsumed perception task at trace end')


def _observations(directory: Path):
    for row in iter_segmented_jsonl(directory/'trace'):
        kind,payload = row['record_type'],row['payload']
        if kind=='reset': yield payload['result']['observation'],None
        elif kind in {'step','close_release'}:
            yield payload['backend_result']['observation'],payload['decision']['action'] if kind=='step' else payload['action']


def _motion_rows(directory: Path):
    events = (r for r in iter_segmented_jsonl(directory/'time-events') if r['event']=='observation')
    expected = 1
    try:
        for row in iter_segmented_jsonl(directory/'movement-events'):
            event = next(events)
            if (row['schema_version']!='mc2p.client-movement-event.v2' or type(row['event_sequence']) is not int
                    or row['event_sequence']!=expected or row['time_event_sequence']!=event['event_sequence']
                    or any(row[k]!=event[k] for k in ('session_id','world_id','client_ticks','generation_id'))):
                raise ValueError('movement/time association or continuity failed')
            expected += 1
            if row['available'] is True:
                if any(type(row[k]) is not bool for k in ('actual_sprinting','actual_sneaking','on_ground')):
                    raise ValueError('actual motion flag missing')
                if set(row['velocity'])!={'x','y','z'} or any(type(v) not in (int,float) or not math.isfinite(v) for v in row['velocity'].values()):
                    raise ValueError('actual motion velocity invalid')
            elif row['available'] is not False or any(row[k] is not None for k in ('actual_sprinting','actual_sneaking','on_ground','pose','velocity')):
                raise ValueError('actual motion missing semantics invalid')
            yield row
        if next(events,None) is not None: raise ValueError('missing movement sample')
    except StopIteration as error: raise ValueError('extra movement sample') from error


def record_fixed_start(stats: dict, offset_ns: int, distance: float | None) -> None:
    """Measure the declared phase boundary, not a later asynchronous task response.

    Missing or late first samples stay failures; never search later rows for a good distance.
    """
    if 'initial_distance' not in stats:
        stats['initial_distance']=distance if 0<=offset_ns<=250_000_000 else None


class TrackingRequestAudit:
    """Inspect every dispatched task request, including those preempted by test holds."""
    def __init__(self):
        self.identity=None; self.loss_started=None; self.loss_scans=0
        self.scans=self.waits=self.steps=0
        self.last_sequence=-1

    def observe(self, context, now_ns):
        identity=(context['task_id'],context['episode_id'])
        if identity!=self.identity:
            self.identity=identity; self.loss_started=None; self.loss_scans=0; self.last_sequence=-1
        sequence=context['observation_sequence_id']
        if sequence<=self.last_sequence: raise ValueError('tracking request replay')
        self.last_sequence=sequence; self.steps+=1
        decision=context['decision']; target=decision['target']
        if target['status']=='visible':
            self.loss_started=None; self.loss_scans=0
            return
        if self.loss_started is None: self.loss_started=now_ns
        if any(decision['movement'].values()) and not (context.get('verified_local_retreat') or context.get('verified_search')):
            raise ValueError('lost target requested blind movement')
        if target['last_seen_ns'] is not None and now_ns-target['last_seen_ns']>=2_000_000_000:
            if target['position'] is not None or target['size'] is not None:
                raise ValueError('expired target memory retained between scheduled samples')
        if target['status']=='waiting_target':
            self.waits+=1
            if any(decision['look'].values()): raise ValueError('waiting target still requested scanning')
        elif decision['state']=='searching' and decision['reason']=='target_not_visible':
            self.loss_scans+=1; self.scans+=1
            if self.loss_scans>16 or now_ns-self.loss_started>=10_000_000_000:
                raise ValueError('target search exceeded count/time budget')
        if (abs(decision['look']['yaw_delta_degrees'])>15 or abs(decision['look']['pitch_delta_degrees'])>10):
            raise ValueError('tracking look exceeded declared per-step limit')


class LeaderTurnEvidence:
    def __init__(self, started_at_ns: int, ended_at_ns: int):
        self.start,self.end=started_at_ns,ended_at_ns
        self.previous=None
        self.net_degrees=self.travel_degrees=0.
        self.samples=0

    def observe(self, now_ns: int, yaw: float):
        if self.start<=now_ns<self.end:
            if self.previous is None or not 0<now_ns-self.previous[0]<=250_000_000:
                raise ValueError('turn lacks fresh preceding actual observation')
            delta=(yaw-self.previous[1]+180)%360-180
            self.net_degrees+=delta; self.travel_degrees+=abs(delta); self.samples+=1
        self.previous=(now_ns,yaw)

    def passed(self) -> bool:
        return self.samples>0 and 80<=self.net_degrees<=100 and self.travel_degrees<=100


class TrackingEvidence:
    """Bounded evidence over actual decision memory and lawful visible references."""
    def __init__(self):
        self.counts=Counter()
        self.original=None

    def observe(self, kind, offset, target, now_ns, entities, yaw, forward):
        if self.original is None: self.original=target['track_id']
        status=target['status']; identity=target['track_id']
        visible_ids={e['track_id'] for e in entities if e['entity_type']=='minecraft:player'}
        if kind in {'short_hide','short_return','long_hide','long_return','object_leave','object_return'}:
            if identity!=self.original: raise ValueError('tracking task silently rebound old reference')
        if status!='visible' and target['last_seen_ns'] is not None and now_ns-target['last_seen_ns']>=2_000_000_000:
            if target['position'] is not None or target['size'] is not None:
                raise ValueError('expired target memory retained location or size')
            self.counts['expired_memory_cleared']+=1
        if kind=='short_hide' and status=='remembered': self.counts['short_remembered']+=1
        prior_absence=self.counts['short_remembered'] if kind=='short_return' else self.counts['long_absence']
        if kind in {'short_return','long_return'} and prior_absence and identity==self.original and status=='visible':
            self.counts[kind+':same_reference']+=1
        if kind=='long_hide' and status!='visible': self.counts['long_absence']+=1
        if kind=='long_hide' and status=='waiting_target': self.counts['finite_search']+=1
        if kind=='object_leave' and status!='visible': self.counts['object_absence']+=1
        if kind=='object_return' and visible_ids-{self.original} and status=='waiting_target' and forward==0:
            self.counts['new_reference_not_rebound']+=1
        if kind=='sideways' and identity!=self.original and identity in visible_ids and status=='visible':
            self.counts['explicit_new_reference']+=1

    def checks(self):
        c=self.counts
        return dict(short_legal_memory=c['short_remembered']>0,
            expired_memory_cleared=c['expired_memory_cleared']>0,
            long_real_absence=c['long_absence']>=100,
            same_reference_short_return=c['short_return:same_reference']>0,
            same_reference_long_return=c['long_return:same_reference']>0,
            finite_search_waiting=c['finite_search']>0,
            object_leave_real_absence=c['object_absence']>=50,
            object_rebuild_not_auto_bound=c['new_reference_not_rebound']>=3,
            explicit_retry_binds_new_reference=c['explicit_new_reference']>0)


def evaluate_fault_release(directory: Path, plan: dict, started_at_ns: int) -> dict:
    from scripts.follow_playground_faults import FinalReleaseEvidence, released_controls
    fault=plan['fault']
    proof=json.loads((directory/'fault-trigger.json').read_text('utf-8'))
    injected=json.loads((directory/'fault-injection.json').read_text('utf-8'))
    if (injected!=dict(proof,injected_at_ns=injected['injected_at_ns'])
            or proof['schema_version']!='mc2p.playground-fault-trigger.v1' or proof['session_id']!=directory.name
            or proof['kind']!=fault['kind']
            or not fault['earliest_ns']<=proof['sampled_at_ns']-started_at_ns<=fault['latest_ns']
            or not 0<=injected['injected_at_ns']-proof['sampled_at_ns']<=250_000_000):
        raise ValueError('invalid actual fault injection provenance')
    at=injected['injected_at_ns']; before=None; after=None; releases=0; actual_moving=False
    final_release=FinalReleaseEvidence(at,at+fault['release_deadline_ns'])
    actor=directory/'runtime/MC2PFollower'
    worker=json.loads((directory/'worker-result.json').read_text('utf-8'))
    budget_exit=worker.get('reason')=='owner_lease_insufficient'
    owner_deadline=0; budget_release_sequence=None; budget_exit_proved=False
    if budget_exit:
        for row in iter_segmented_jsonl(actor/'trace'):
            payload=row['payload']
            if row['record_type']=='task_lease_renewed': owner_deadline=max(owner_deadline,payload['owner_deadline_ns'])
            if row['record_type']=='playground_control_release' and payload['reason']=='owner_lease_insufficient':
                budget_release_sequence=payload['observation_sequence_id']
    for (obs,action),motion in zip_longest(_observations(actor),_motion_rows(actor)):
        if obs['sequence_id']==proof['observation_sequence_id']:
            if (action is None or action['movement']['forward']!=1
                    or not 0<=proof['sampled_at_ns']-obs['received_at_monotonic_ns']<=250_000_000):
                raise ValueError('fault was not triggered by actual follow dispatch')
            before=obs['self_state']['value']['position']
            actual_moving=motion['available'] and math.hypot(motion['velocity']['x'],motion['velocity']['z'])>.01
        if action is None or obs['request_started_at_monotonic_ns']<at: continue
        neutral=released_controls(action)
        final_release.observe(obs['request_started_at_monotonic_ns'],obs['received_at_monotonic_ns'],neutral)
        if obs['sequence_id']==budget_release_sequence:
            budget_exit_proved=neutral and owner_deadline>0 and owner_deadline-250_000_000<=obs['request_started_at_monotonic_ns']<=owner_deadline+1_000_000_000
        if obs['received_at_monotonic_ns']<=at+fault['release_deadline_ns'] and neutral:
            releases+=1
            if after is None: after=obs['self_state']['value']['position']
        if obs['request_started_at_monotonic_ns']>=at+3_250_000_000 and not neutral:
            raise ValueError('control remained active after owner/watchdog release deadline')
    checks=[dict(name='fault_injected_during_actual_motion',passed=bool(before is not None and actual_moving)),
            dict(name='fault_followed_by_confirmed_neutral_observation',passed=releases>0 and final_release.passed())]
    if budget_exit: checks.append(dict(name='early_release_matches_recorded_owner_budget',passed=budget_exit_proved))
    displacement=None
    if fault['kind']=='worker_stall':
        resumed=json.loads((directory/'fault-resumed.json').read_text('utf-8'))['resumed_at_ns']
        checks.append(dict(name='actual_worker_pause_observed',passed=4_000_000_000<=resumed-at<=4_500_000_000))
        if before is not None and after is not None:
            displacement=math.hypot(after['x']-before['x'],after['z']-before['z'])
        checks.append(dict(name='client_did_not_keep_moving_during_worker_stall',passed=displacement is not None and displacement<=3))
    return dict(checks=checks,metrics=dict(fault_kind=fault['kind'],neutral_observations=releases,
        stalled_horizontal_displacement_blocks=displacement))


def evaluate_playground(directory: Path, case: str) -> dict:
    from scripts.block_observation_v3_evidence import NAVIGATION_CONTRACT,require_navigation_contract
    from scripts.follow_playground_session import CORE_SOURCES,ROOT,_hash
    from scripts.active_perception_scenarios import MEASUREMENT_REVISION
    directory = _safe_path(directory)
    checks,errors = [],[]
    metrics = {}
    def check(name,value): checks.append(dict(name=name,passed=bool(value)))
    try:
        manifest=json.loads((directory/'manifest.json').read_text('utf-8'))
        require_navigation_contract(manifest)
        if (manifest['schema_version']!='mc2p.follow-playground-session.v3'
                or manifest['measurement_revision']!=MEASUREMENT_REVISION):
            raise ValueError('current case requires block-state measurement6 manifest')
        if manifest.get('core_sources')!={name:_hash(ROOT/name) for name in CORE_SOURCES}:
            raise ValueError('current case source fingerprints missing or changed')
        plan = json.loads((directory/'case-plan.json').read_text('utf-8'))
        validate_case_plan(plan,historical=True)
        if plan['case']!=case: raise ValueError('foreign case plan')
        fault=plan.get('fault')
        start=json.loads((directory/'case-start.json').read_text('utf-8'))
        if start['case']!=case or type(start['started_at_ns']) is not int or start['started_at_ns']<=0:
            raise ValueError('missing declared real case start')
        started_at_ns=start['started_at_ns']
        parent = json.loads((directory/'result.json').read_text('utf-8'))
        owner=validate_owner_events(iter_segmented_jsonl(directory/'owner-events'),plan,started_at_ns,parent['session_id'])
        check('all_scheduled_owner_commands_applied',True)
        supervisor = parent['supervisor']
        if fault:
            worker=json.loads((directory/'worker-result.json').read_text('utf-8'))
            check('declared_fault_observed_and_cleanup_complete',parent['state']=='failed'
                and expected_fault_outcome(fault['kind'],supervisor,worker)
                and parent['evidence']['status']=='passed')
            outer=json.loads((directory/'outer-supervisor.json').read_text('utf-8'))
            check('outer_fault_supervision_completed_without_timeout_or_cleanup_failure',fault_outer_completed(outer))
        else:
            check('parent_and_worker_complete',parent['state']=='closed' and not parent['primary_failure']
                and supervisor['return_code']==0 and not supervisor['primary_failure'] and supervisor['process_stopped']
                and not supervisor['cleanup_failures'] and all(parent[k]['status']=='passed' for k in ('behavior','evidence','cleanup')))
        triggers={}
        for index,(command,applied) in enumerate(zip(plan['owner_commands'],owner)):
            if 'when' not in command: continue
            proof=json.loads((directory/f'command-trigger-{index:02}.json').read_text('utf-8'))
            if (proof['schema_version']!='mc2p.playground-command-trigger.v1' or proof['session_id']!=directory.name
                    or proof['command_index']!=index or proof['when']!=command['when']
                    or not command['at_ns']<=proof['sampled_at_ns']-started_at_ns<=command['latest_ns']
                    or not 0<=applied['sent_at_ns']-proof['sampled_at_ns']<=250_000_000):
                raise ValueError('invalid or stale stop trigger')
            triggers[applied['command']['sequence']]=proof
        stops=StopEvidence(owner,{seq:p['when'] for seq,p in triggers.items()})
        verified_triggers=set()
        for role in ('MC2PFollower','MC2PLeader'):
            source = directory/'runtime'/role
            time = export_segmented_runtime_time_evidence(source,source/('case-validation-'+uuid.uuid4().hex))
            check(role+':complete_zero_image_runtime_and_time',time['status']=='passed')
            count=0
            for formal,motion_row in zip_longest(_observations(source),_motion_rows(source)):
                if formal is None or motion_row is None: raise ValueError('motion/formal coverage differs')
                obs,actual_action=formal; sample=obs['client_sample']
                if (motion_row['generation_id']!=obs['sequence_id'] or motion_row['clock_id']!=sample['clock_id']
                        or not sample['started_at_monotonic_ns']-250_000_000<=motion_row['sampled_at_monotonic_ns']<=sample['completed_at_monotonic_ns']+250_000_000):
                    raise ValueError('complete motion stream is not in its formal sample clock')
                if role=='MC2PFollower':
                    stops.observe(obs,actual_action,motion_row)
                    for sequence,proof in triggers.items():
                        if obs['sequence_id']==proof['observation_sequence_id']:
                            if (not 0<=proof['sampled_at_ns']-obs['received_at_monotonic_ns']<=250_000_000
                                    or actual_action is None or not eligible_stop(proof['when'],actual_action['movement'],motion_row)):
                                raise ValueError('stop trigger is not actual ground/airborne motion')
                            verified_triggers.add(sequence)
                count+=1
            check(role+':actual_movement_stream_complete',count>1)
        actor = directory/'runtime/MC2PFollower'
        from scripts.navigation_motion_evidence import NavigationRequestAudit
        search_window=None
        if case=='search':
            corner=next(p for p in plan['phases'] if p['kind']=='corner')
            search_window=(started_at_ns+corner['start_ns'],started_at_ns+49_000_000_000)
        elif case=='long-session':
            hold=plan['test_look_hold']
            search_window=(started_at_ns+hold['start_ns'],started_at_ns+hold['start_ns']+hold['away_ns'])
        navigation_audit=NavigationRequestAudit(required=True,search_required=True,search_window=search_window)
        tracking_audit=TrackingRequestAudit() if case in {'tracking','long-session','search','search-missing'} else None
        long_audit=None
        if case=='long-session':
            from scripts.follow_playground_long import LongTaskAudit
            long_audit=LongTaskAudit(owner)
        perception_variant=manifest['perception_variant']
        perception_config=manifest['perception_config']
        observations,motions = iter(_actor_steps(actor,tracking_audit,long_audit,navigation_audit,
            perception_variant=perception_variant,
            perception_config=perception_config)),iter(_motion_rows(actor))
        observation,action,context = next(observations)
        motion = next(motions)
        counts = Counter(); phase_counts = Counter(); visible_histogram = Counter()
        mode_motion = {m:Counter() for m in ('slow','normal','fast','max')}
        speed_windows = {m:[] for m in mode_motion}  # At most two endpoints per fixed window.
        previous_ground = {m:True for m in mode_motion}
        jump_pending = {m:False for m in mode_motion}
        last_slot = -1; previous_sequence = -1
        missing_since = None; max_missing_ns = 0
        last_gait = None; gait_switches = 0
        verified_starts = set()
        tracking=TrackingEvidence()
        fault_at=None
        if fault:
            injected=json.loads((directory/'fault-injection.json').read_text('utf-8'))
            fault_at=injected['sampled_at_ns']-started_at_ns
        prefault_counts=Counter()
        for row in iter_segmented_jsonl(directory/'case-samples'):
            slot = row['slot']
            if type(slot) is not int or slot<=last_slot or not 0<=slot<math.ceil(plan['duration_ns']/plan['interval_ns']):
                raise ValueError('duplicate/out-of-range scheduled slot')
            last_slot = slot
            phase = phase_at(plan,slot*plan['interval_ns'])
            if phase is None: continue
            if row['phase_id']!=phase['id']: raise ValueError('sample assigned to wrong phase')
            while observation['sequence_id']<row['observation_sequence_id']:
                observation,action,context = next(observations)
                motion = next(motions)
            if (observation['sequence_id']!=row['observation_sequence_id'] or observation['episode_id']!=row['episode_id']
                    or motion['generation_id']!=observation['sequence_id']): raise ValueError('case sample not bound to formal observation')
            validate_scheduled_sample(row,observation,plan,started_at_ns,previous_sequence)
            previous_sequence=observation['sequence_id']
            if context is not None:
                if any(row[k]!=context[k] for k in ('task_id','attempt_id','episode_id','source_generation','intent_sequence','requested_mode')):
                    raise ValueError('case task identity/mode is not actual Runtime task')
                starts=[c for c in owner if c['command']['kind']=='follow_start' and c['sent_at_ns']<=row['sampled_at_ns']
                    and c['status']['task_id']==context['task_id'] and c['status']['attempt_id']==context['attempt_id']]
                if not starts: raise ValueError('Runtime task lacks applied owner start')
                verified_starts.update(c['command']['sequence'] for c in starts)
                validate_owner_configuration(owner,row['observed_at_ns'],context['requested_mode'])
                if context['active_perception'] is not None:
                    validate_owner_distance(owner,context['decision_time_ns'],
                        context['active_perception']['auto_target_distance_blocks'],
                        task_id=context['task_id'],attempt_id=context['attempt_id'])
                if row['selected_gait']!=context['decision']['selected_gait']:
                    raise ValueError('case selected gait is not actual controller decision')
            elif row['task_id'] is not None and row['reason']!='controls_released':
                raise ValueError('active case sample lacks Runtime task evidence')
            offset=slot*plan['interval_ns']-phase['start_ns']
            if offset>=500_000_000 and row['requested_mode']!=phase['mode']:
                raise ValueError('case phase mode was not applied')
            if case=='auto' and offset>=500_000_000 and context is None:
                raise ValueError('auto phase is not running an authorized task')
            own = observation['self_state']['value']
            if own is None or own['game_mode']!='survival' or own['is_dead'] or own['is_burning']:
                raise ValueError('actor unsafe or non-survival')
            if row['position']!=[own['position'][k] for k in ('x','y','z')]: raise ValueError('sample position not from actor observation')
            if action is None or row['movement']!=action['movement']: raise ValueError('sample movement not from actual Runtime dispatch')
            sample = observation['client_sample']
            if motion['clock_id']!=sample['clock_id']: raise ValueError('actual movement clock identity mismatch')
            if not sample['started_at_monotonic_ns']-250_000_000<=motion['sampled_at_monotonic_ns']<=sample['completed_at_monotonic_ns']+250_000_000:
                raise ValueError('actual movement sample clock mismatch')
            entities = observation['perception']['value']['visible_entities']
            target = next((e for e in entities if e['track_id']==row['target_track_id']),None)
            visible = target is not None
            distance = None if target is None else math.hypot(target['relative_position']['x'],target['relative_position']['z'])
            if visible!=row['target_visible'] or (distance is None)!=(row['distance'] is None) or (
                    distance is not None and abs(distance-row['distance'])>1e-6): raise ValueError('target metric is not lawful visible perception')
            phase_counts[phase['id']] += 1; counts['samples'] += 1
            if fault_at is not None and slot*plan['interval_ns']<fault_at: prefault_counts[phase['id']]+=1
            if visible:
                visible_histogram[min(640,int(distance/.05))] += 1
                if missing_since is not None: max_missing_ns=max(max_missing_ns,slot*plan['interval_ns']-missing_since)
                missing_since = None
            elif missing_since is None: missing_since = slot*plan['interval_ns']
            if row['selected_gait']!=last_gait:
                if last_gait is not None: gait_switches += 1
                last_gait = row['selected_gait']
            movement = row['movement']
            if row['state']=='idle' and (movement['forward'] or movement['strafe'] or movement['jump'] or movement['sprint']):
                raise ValueError('new movement after follow stop')
            if case=='search' and 46_000_000_000<=row['sampled_at_ns']-started_at_ns<49_000_000_000:
                from scripts.target_search_evidence import verified_final_hold
                counts['search_final_hold']+=verified_final_hold(context,row['target_track_id'],distance,movement)
            if case=='fixed':
                mode = phase['mode']; stats = mode_motion[mode]
                if phase['kind']=='chase':
                    record_fixed_start(stats,row['sampled_at_ns']-started_at_ns-phase['start_ns'],distance)
                if movement['forward']==1 and row['requested_mode']==mode:
                    if context is not None and context.get('verified_local_retreat'):
                        stats['local_retreat']+=1
                    else:
                        if movement!=trace_projection(fixed_movement(mode)): raise ValueError('fixed gait silently changed')
                        stats['approach'] += 1
                if motion['available']:
                    stats['sprint'] += motion['actual_sprinting']
                    stats['sneak'] += motion['actual_sneaking'] and motion['pose']=='crouching'
                    if previous_ground[mode] and not motion['on_ground']: jump_pending[mode]=True
                    if not previous_ground[mode] and motion['on_ground'] and jump_pending[mode]:
                        stats['landings'] += 1; jump_pending[mode]=False
                    previous_ground[mode] = motion['on_ground']
                offset = slot*plan['interval_ns']-phase['start_ns']
                if phase['kind']=='chase' and 1_000_000_000<=offset<=6_000_000_000:
                    endpoints = speed_windows[mode]
                    point = (row['observed_at_ns'],row['position'])
                    if not endpoints: endpoints.append(point)
                    if len(endpoints)==1: endpoints.append(point)
                    else: endpoints[1] = point
                    stats['speed_samples'] += 1
                if phase['kind']=='stop' and offset>=phase['duration_ns']-3_000_000_000:
                    stats['stopped_in_band'] += visible and distance<=3.5 and not movement['forward'] and not movement['jump']
                if phase['kind']=='rechase': stats['restarted'] += movement['forward']==1
            elif case=='auto':
                if phase['kind']=='static' and slot*plan['interval_ns']>=phase['start_ns']+10_000_000_000:
                    counts['static_in_band'] += visible and abs(distance-3)<=1
                if phase['kind']=='moving': counts['moving_in_band'] += visible and distance<=6
            elif case=='long-session' and phase['kind']=='long':
                if context is None and row['reason']!='controls_released': raise ValueError('long phase has no live task')
                if row['retained_source_slots']>64: raise ValueError('unbounded source slots')
            elif case in {'tracking','interrupt'}:
                counts[phase['kind']+':visible'] += visible
                counts[phase['kind']+':missing'] += not visible
                counts[phase['kind']+':neutral'] += not movement['forward'] and not movement['strafe'] and not movement['jump']
                counts[phase['kind']+':waiting'] += row['state']=='waiting_target'
                if case=='tracking' and context is not None:
                    tracking.observe(phase['kind'],offset,context['decision']['target'],context['decision_time_ns'],
                        entities,observation['yaw_degrees']['value'],movement['forward'])
                    if phase['kind']=='pass' and distance is not None and distance<2:
                        counts['pass:near']+=1
        # Exhaust all evidence even if scheduled samples ended earlier than Runtime cleanup.
        for _ in observations: pass
        for _ in motions: pass
        metrics['navigation']=navigation_audit.metrics()
        if case in {'search','search-missing'}:
            search=navigation_audit.search
            check('target_actually_lost',search.counts['losses']>0)
            if case=='search':
                check('same_loss_corner_movement_then_reacquisition',search.loss.completed>0)
                check('search_final_three_second_hold',counts['search_final_hold']>=27)
                from scripts.navigation_scenarios import SearchLeaderEvidence
                leader=SearchLeaderEvidence(plan,started_at_ns)
                for obs,_ in _observations(directory/'runtime/MC2PLeader'): leader.observe(obs)
                for name,passed in leader.checks().items(): check(name,passed)
                metrics['search_leader']=leader.metrics()
            else:
                check('finite_search_reports_waiting',search.counts['waiting_requests']>=10)
        if case=='navigation':
            check('neutral_look_before_local_retreat',navigation_audit.counts['executed_neutral_local_looks']>=6)
            check('fresh_checked_local_movement',navigation_audit.counts['executed_local_movements']>=1)
            check('actual_local_displacement',navigation_audit.max_retreat_displacement>=.2)
        if case=='active-world-change':
            if perception_variant!='active_perception_v1':raise ValueError('world change requires current active variant')
            from scripts.active_perception_world_change_evidence import evaluate_world_change
            changed=evaluate_world_change(directory)
            checks.extend(changed['checks'])
            metrics['world_change']=changed['metrics']
        check('each_applied_start_has_actual_task',verified_starts=={
            c['command']['sequence'] for c in owner if c['command']['kind']=='follow_start'})
        if missing_since is not None: max_missing_ns=max(max_missing_ns,plan['duration_ns']-missing_since)
        for phase in plan['phases']:
            duration=phase['duration_ns'] if fault_at is None else max(0,min(phase['duration_ns'],fault_at-phase['start_ns']))
            expected = math.ceil(duration/plan['interval_ns'])
            numerator=phase_counts[phase['id']] if fault_at is None else prefault_counts[phase['id']]
            check('phase_'+str(phase['id'])+':scheduled_coverage',expected>0 and numerator/expected>=.9)
        if case=='fixed':
            speeds = {}
            for mode,stats in mode_motion.items():
                points = speed_windows[mode]
                seconds = 0 if len(points)!=2 else (points[1][0]-points[0][0])/1e9
                speeds[mode] = 0 if seconds<=0 else math.hypot(points[1][1][0]-points[0][1][0],points[1][1][2]-points[0][1][2])/seconds
                check(mode+':five_second_measurement',seconds>=4.9 and stats['speed_samples']>=45 and speeds[mode]>.1)
                check(mode+':common_initial_distance',stats.get('initial_distance') is not None
                    and abs(stats['initial_distance']-plan['leader_separation_distance_blocks'])<=2)
                check(mode+':approach_stop_rechase',stats['approach']>10 and stats['stopped_in_band']>=27 and stats['restarted']>5)
            check('actual_crouch_sprint_and_three_landings',mode_motion['slow']['sneak']>10 and mode_motion['fast']['sprint']>10 and mode_motion['max']['landings']>=3)
            check('slow_lt_normal_lt_fast',speeds['slow']<speeds['normal']<speeds['fast'])
            metrics.update(speed_blocks_per_second=speeds,actual_motion={k:dict(v) for k,v in mode_motion.items()})
        elif case=='auto':
            moving=next(p for p in plan['phases'] if p['kind']=='moving')
            previous=None; travel=0; moving_samples=0
            for obs,_ in _observations(directory/'runtime/MC2PLeader'):
                elapsed=obs['received_at_monotonic_ns']-started_at_ns
                if moving['start_ns']<=elapsed<moving['start_ns']+moving['duration_ns']:
                    own=obs['self_state']['value']; position=own['position']
                    if previous is not None: travel+=math.hypot(position['x']-previous['x'],position['z']-previous['z'])
                    previous=position; moving_samples+=1
            check('moving_phase_has_actual_leader_motion',travel>=10 and moving_samples>=540)
            metrics['leader_moving_travel_blocks']=travel
            check('static_90_percent_all_scheduled_samples',counts['static_in_band']/200>=.9)
            check('moving_90_percent_all_scheduled_samples',counts['moving_in_band']/600>=.9)
        elif case=='long-session':
            for name,passed in long_audit.checks().items(): check(name,passed)
            if plan['revision']>=5:
                check('fresh_continuous_target_absence_at_least_35_seconds',navigation_audit.search.loss.max_fresh_unseen_ns>=35_000_000_000)
                check('same_loss_search_budget_waiting_verified',navigation_audit.search.loss.max_budget_fresh_unseen_ns>=35_000_000_000)
                check('search_survived_actual_other_selected_controls',navigation_audit.search.counts['other_selected_search_requests']>0)
            metrics['long_task']=long_audit.metrics()
            complete = json.loads((actor/'trace/complete.json').read_text('utf-8'))
            check('at_least_three_trace_segments',complete['segment_count']>=3)
            from scripts.follow_playground_resources import summarize_resources, long_resource_checks, resource_identity_matches
            worker_rss=summarize_resources(directory/'worker-rss')
            validator_rss=summarize_resources(directory/'validator-rss')
            beat=json.loads((directory/'heartbeat.json').read_text('utf-8'))
            validator_supervisor=json.loads((directory/'offline-evidence-supervisor.json').read_text('utf-8'))
            check('rss_bound_to_supervised_worker',resource_identity_matches(worker_rss,'worker',
                [dict(pid=beat['worker_pid'],create_time=beat['worker_create_time'])])
                and resource_identity_matches(worker_rss,'worker',supervisor['registered_processes']))
            check('rss_bound_to_supervised_validator',resource_identity_matches(validator_rss,'validator',validator_supervisor['registered_processes']))
            for name,passed in long_resource_checks(worker_rss,validator_rss).items(): check(name,passed)
            metrics['resources']=dict(worker=worker_rss,validator=validator_rss)
        elif case=='tracking':
            check('all_task_requests_checked_for_bounded_search',tracking_audit.steps>0 and tracking_audit.scans>0 and tracking_audit.waits>0)
            for name,passed in tracking.checks().items(): check(name,passed)
            for kind in ('sideways','turn','pass','jump'): check(kind+':legal_tracking',counts[kind+':visible']>=20)
            check('passes_near_follower',counts['pass:near']>0)
            turn_phase=next(p for p in plan['phases'] if p['kind']=='turn')
            turn=LeaderTurnEvidence(started_at_ns+turn_phase['start_ns'],started_at_ns+turn_phase['start_ns']+turn_phase['duration_ns'])
            previous_ground=True; landings=0
            for (obs,_),motion_row in zip_longest(_observations(directory/'runtime/MC2PLeader'),_motion_rows(directory/'runtime/MC2PLeader')):
                phase=phase_at(plan,obs['received_at_monotonic_ns']-started_at_ns)
                turn.observe(obs['received_at_monotonic_ns'],obs['yaw_degrees']['value'])
                if phase is not None and phase['kind']=='jump':
                    if not previous_ground and motion_row['on_ground']: landings+=1
                    previous_ground=motion_row['on_ground']
                    if obs['self_state']['value']['is_flying']: raise ValueError('ordinary jump became creative flight')
            check('actual_leader_ninety_degree_turn',turn.passed())
            check('actual_leader_ordinary_jump_landing',landings>=1)
            metrics.update(tracking=dict(tracking.counts),leader_turn_degrees=turn.net_degrees,leader_jump_landings=landings)
        elif case=='interrupt':
            if fault:
                detail=evaluate_fault_release(directory,plan,started_at_ns)
                checks.extend(detail['checks']); metrics.update(detail['metrics'])
            else:
                check('stop_observed_neutral',counts['released:neutral']>=54)
                check('both_stop_triggers_from_actual_movement',len(triggers)==2 and verified_triggers==set(triggers))
                check('neutral_release_observed_on_ground_and_in_air',stops.releases_complete())
        total = sum(visible_histogram.values())
        def percentile(q):
            cumulative = 0
            for value,count in sorted(visible_histogram.items()):
                cumulative += count
                if cumulative>=total*q: return (value+1)*.05
            return None
        metrics.update(samples=counts['samples'],visible_distance_p50=percentile(.5),visible_distance_p95=percentile(.95),
                       distance_histogram_bin_blocks=.05,max_contiguous_missing_ns=max_missing_ns,gait_switches=gait_switches)
    except (OSError,ValueError,KeyError,TypeError,StopIteration) as error:
        errors.append(type(error).__name__+': '+str(error)[:2048])
    return dict(schema_version='mc2p.playground-case-evidence.v1',case=case,passed=bool(checks) and all(c['passed'] for c in checks) and not errors,
                checks=checks,errors=errors,metrics=metrics,measurement_revision=MEASUREMENT_REVISION,
                **dict(NAVIGATION_CONTRACT))
