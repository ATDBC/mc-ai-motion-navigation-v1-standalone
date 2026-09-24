"""Read-only actual-pose quality summaries; this module is never an actor input."""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ''}:
    sys.path.insert(0, str(ROOT))

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.observation import Vec3V0
from mc2p.runtime.segmented_trace import _safe_path
from scripts.active_perception_metrics import (
    MAX_INTERVAL_NS, MAX_SAMPLES, MIN_COVERAGE, QualitySample, summarize_phase,
)
from scripts.active_perception_scenarios import ANGULAR_WINDOW_NS, MEASUREMENT_REVISION, quality_case_plan
from scripts.block_observation_v3_evidence import (
    NAVIGATION_CONTRACT, require_navigation_contract, require_navigation_snapshot_v3,
)

VARIANTS = ('m6_baseline','smooth_only','active_perception_v1')
SEEDS = (21001,21002,21003)


def angular_window_report(samples: tuple[QualitySample,...], start_ns: int) -> dict:
    """Fixed transient population, including zero derivatives; never outcome-cut."""
    result = summarize_phase(samples,start_ns=start_ns,end_ns=start_ns+ANGULAR_WINDOW_NS)
    result.update(start_ns=start_ns,end_ns=start_ns+ANGULAR_WINDOW_NS,duration_ns=ANGULAR_WINDOW_NS)
    return result


def quality_sample(raw: dict, action: dict | None, track_id: str | None,
                   task_id: str | None = None) -> QualitySample | None:
    observation = require_navigation_snapshot_v3(raw)
    if observation.privileged_fields_present:
        raise ValueError('quality observation contains privileged fields')
    own = observation.self_state.value
    if own is None:
        return None  # Missing intervals remain missing; never invent a pose.
    incoming = None
    if action is not None:
        if (action['schema_version'] != 'mc2p.action-snapshot.v1'
                or action['episode_id'] != observation.episode_id
                or action['request_sequence_id'] != observation.request_sequence_id
                or action['observation_sequence_id']+1 != observation.sequence_id):
            raise ValueError('quality action does not lead to the actual post observation')
        incoming = MovementV1(**action['movement']).forward
    perception = observation.perception.value
    visible = None if perception is None or track_id is None else False
    distance = None
    if perception is not None and track_id is not None:
        target = next((e for e in perception.visible_entities if e.track_id == track_id), None)
        visible = target is not None
        if target is not None:
            distance = math.hypot(target.relative_position.x, target.relative_position.z)
    return QualitySample(observation.episode_id,observation.sequence_id,observation.controller_clock_id,
        observation.client_sample.clock_id,observation.received_at_monotonic_ns,
        observation.client_sample.completed_at_monotonic_ns,own.position,own.yaw_degrees,own.pitch_degrees,
        incoming,visible,distance,task_id=task_id)


def sample_identity(sample: QualitySample) -> tuple[str,int,str,str]:
    return (sample.episode_id,sample.sequence_id,sample.controller_clock_id,sample.client_clock_id)


def continuous_samples(before: QualitySample, after: QualitySample) -> bool:
    return (before.episode_id==after.episode_id
        and before.controller_clock_id==after.controller_clock_id
        and before.client_clock_id==after.client_clock_id
        and after.sequence_id==before.sequence_id+1
        and 0<after.controller_ns-before.controller_ns<=MAX_INTERVAL_NS
        and 0<after.client_ns-before.client_ns<=MAX_INTERVAL_NS)


def _overlap_ns(before: QualitySample, after: QualitySample, begin_ns: int, end_ns: int) -> int:
    if not continuous_samples(before,after):
        return 0
    return max(0,min(end_ns,after.controller_ns)-max(begin_ns,before.controller_ns))


def _sample_coverage_ns(samples: tuple[QualitySample,...], begin_ns: int, end_ns: int) -> int:
    return sum(_overlap_ns(before,after,begin_ns,end_ns)
               for before,after in zip(samples,samples[1:]))


def leader_normal_input_report(samples: tuple[QualitySample,...], proofs: dict,
                               begin_ns: int, end_ns: int) -> dict:
    duration=end_ns-begin_ns
    if duration<=0:
        raise ValueError('leader input interval must be positive')
    proven=0; violations=0
    for before,after in zip(samples,samples[1:]):
        overlap=_overlap_ns(before,after,begin_ns,end_ns)
        if overlap<=0:
            continue
        proof=proofs.get(sample_identity(after))
        if proof is True:
            proven+=overlap
        elif proof is False:
            violations+=1
    coverage=proven/duration
    return dict(passed=coverage>=MIN_COVERAGE and violations==0,
                valid_coverage_fraction=coverage,normal_input_violation_intervals=violations)


def completion_seconds(samples: tuple[QualitySample, ...], leaders: tuple[QualitySample, ...],
                       goal: tuple[float,float], start_ns: int, *,
                       clock_bindings: dict[str,str] | None = None) -> float | None:
    """Continuous actual, same-task proximity after the leader reached its goal."""
    index = 0
    bindings = clock_bindings or {}
    near_since = previous = identity = None
    for sample in samples:
        if sample.controller_ns < start_ns:
            continue
        while index+1 < len(leaders) and leaders[index+1].controller_ns <= sample.controller_ns:
            index += 1
        leader = leaders[index] if leaders else None
        current_identity = (sample.episode_id,sample.controller_clock_id,sample.client_clock_id,sample.task_id)
        valid = (leader is not None and bindings.get(leader.controller_clock_id,leader.controller_clock_id)
            == bindings.get(sample.controller_clock_id,sample.controller_clock_id)
            and 0 <= sample.controller_ns-leader.controller_ns <= 250_000_000
            and math.hypot(leader.position.x-goal[0],leader.position.z-goal[1]) <= .25
            and sample.target_visible is True and sample.target_distance is not None
            and sample.target_distance <= 3.5 and sample.task_id is not None
            and sample.incoming_forward is not None)
        contiguous = (previous is not None and identity == current_identity
            and sample.sequence_id == previous.sequence_id+1
            and 0 < sample.controller_ns-previous.controller_ns <= MAX_INTERVAL_NS
            and 0 < sample.client_ns-previous.client_ns <= MAX_INTERVAL_NS)
        if not valid:
            near_since = None
        elif near_since is None or not contiguous:
            near_since = sample.controller_ns
        elif sample.controller_ns-near_since >= 1_000_000_000:
            return (sample.controller_ns-start_ns)/1e9
        previous, identity = sample, current_identity
    return None


def compare_reports(reports: tuple[dict, ...]) -> dict:
    expected = {(variant,seed) for variant in VARIANTS for seed in SEEDS}
    groups = {}
    errors = []
    for report in reports:
        key = (report.get('perception_variant'),report.get('seed'))
        if key not in expected or key in groups:
            errors.append('unexpected_or_duplicate_group')
        else:
            groups[key] = report
    missing = sorted(expected-set(groups))
    result = dict(schema_version='mc2p.active-perception-comparison.v2',passed=False,
                  missing_groups=missing,errors=errors,checks=[],runs=list(reports))
    if missing or errors:
        return result
    def check(name, passed):
        result['checks'].append(dict(name=name,passed=bool(passed)))

    def number(value):
        if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
            raise ValueError('quality metric is unavailable or invalid')
        return value

    try:
        reference = groups[('m6_baseline',21001)]
        require_navigation_contract(reference)
        check('current_measurement_protocol',reference['measurement_revision']==MEASUREMENT_REVISION
              and reference['scenario_plan']==quality_case_plan())
        sensor = reference['sensor_contract']
        check('current_sensor_profile', sensor.get('sensor_profile_revision') == 3
              and sensor.get('horizontal_fov_degrees') == 120.
              and sensor.get('vertical_fov_degrees') == 120.
              and sensor.get('ray_columns') == 159 and sensor.get('ray_rows') == 9)
        for key, report in groups.items():
            require_navigation_contract(report)
            check(str(key)+':current_report_version',report.get('schema_version')=='mc2p.active-perception-quality.v3')
            statuses={'passed'} if key[0]=='active_perception_v1' else {'passed','behavior_failed'}
            check(str(key)+':evaluable',report['status'] in statuses
                  and report['measurement_status']=='passed'
                  and len(report['phases'])==4 and {p['id'] for p in report['phases']}=={0,1,2,3}
                  and all(p['status'] in statuses and p['measurement_status']=='passed'
                          and number(p['valid_coverage_fraction'])>=.95 for p in report['phases']))
            if key[0]=='active_perception_v1':
                check(str(key)+':all_tasks_completed',report['task_status']=='passed' and all(
                    p['task_status']=='passed' and 0<number(p['completion_seconds'])<=35. for p in report['phases']))
            check(str(key)+':angular_windows_evaluable',all(
                p['angular_window']['status']=='passed'
                and number(p['angular_window']['valid_coverage_fraction'])>=.95
                and p['angular_window']['duration_ns']==ANGULAR_WINDOW_NS for p in report['phases']))
            for name in ('source_fingerprints','measurement_revision','scenario_plan','sensor_contract',
                         'observation_schema_version','knowledge_model','field_profile'):
                check(str(key)+':matched_'+name,report[name]==reference[name])
            check(str(key)+':frozen_variant_configuration',
                  report['perception_config']==groups[(key[0],21001)]['perception_config'])

        def phase(variant, seed, index):
            return next(p for p in groups[(variant,seed)]['phases'] if p['id']==index)

        def median(variant, index, metric):
            return statistics.median(number(phase(variant,seed,index)[metric]) for seed in SEEDS)

        base_low = median('m6_baseline',0,'low_head_fraction')
        active_low = median('active_perception_v1',0,'low_head_fraction')
        check('normal_straight_low_head_relative_reduction_20_percent',base_low>0 and active_low<=.8*base_low)
        base_accel = statistics.median(number(phase('m6_baseline',seed,3)['angular_window']
            ['angular_acceleration_abs_p95']) for seed in SEEDS)
        active_accel = statistics.median(number(phase('active_perception_v1',seed,3)['angular_window']
            ['angular_acceleration_abs_p95']) for seed in SEEDS)
        check('turn_acceleration_relative_reduction_20_percent',base_accel>0 and active_accel<=.8*base_accel)
        for seed in SEEDS:
            base = phase('m6_baseline',seed,0)
            active = phase('active_perception_v1',seed,0)
            check(str(seed)+':normal_completion_time',number(base['completion_seconds'])>0
                  and number(active['completion_seconds'])<=1.1*base['completion_seconds'])
            for index in range(4):
                base,active = phase('m6_baseline',seed,index),phase('active_perception_v1',seed,index)
                # All unobserved time could be unseen. Do not count it as visibility.
                check(f'{seed}:{index}:target_unseen',number(active['target_unseen_seconds'])
                    +number(active['target_unknown_seconds'])<=1.1*number(base['target_unseen_seconds'])+.5)
                check(f'{seed}:{index}:stop_starts',number(active['stop_starts'])<=number(base['stop_starts'])+1)
        unseen_better = any(statistics.median(
            number(phase('active_perception_v1',seed,index)['target_unseen_seconds'])
            +number(phase('active_perception_v1',seed,index)['target_unknown_seconds']) for seed in SEEDS)
            < median('smooth_only',index,'target_unseen_seconds') for index in range(4))
        check('joint_better_than_smoothing_alone',active_low<median('smooth_only',0,'low_head_fraction') or unseen_better)
        result['passed'] = all(item['passed'] for item in result['checks']) and not errors
    except (KeyError,ValueError,TypeError,StopIteration) as error:
        result['errors'].append(str(error))
    return result


def classify_phase(detail: dict, *, completed: float | None, route_passed: bool, normal_passed: bool) -> dict:
    """Task failure is evidence, not permission to waive measurement/legality gates."""
    if completed is not None and (type(completed) not in (int,float)
            or not math.isfinite(completed) or not 0<completed<=35.):
        raise ValueError('invalid quality completion time')
    measured=(detail['status']=='passed' and detail['angular_window']['status']=='passed'
        and detail['valid_coverage_fraction']>=MIN_COVERAGE
        and detail['angular_window']['valid_coverage_fraction']>=MIN_COVERAGE
        and route_passed is True and normal_passed is True)
    task='passed' if completed is not None else 'failed'
    status=('passed' if task=='passed' else 'behavior_failed') if measured else 'inconclusive'
    reason=detail['reason']
    if completed is None:
        reason+='; task did not meet continuous one-second completion'
    if not route_passed or not normal_passed:
        reason+='; leader route or ordinary-input evidence insufficient'
    return dict(detail,measurement_status='passed' if measured else 'inconclusive',
                task_status=task,status=status,completion_seconds=completed,reason=reason)


def summarize_quality(directory: Path) -> dict:
    result = dict(schema_version='mc2p.active-perception-quality.v3',status='failed',
                  measurement_status='failed',task_status='unknown',baseline_admissible=False,
                  directory=str(directory),errors=[],checks=[],phases=[])
    try:
        directory = _safe_path(directory)
        manifest = json.loads((directory/'manifest.json').read_text('utf-8'))
        if manifest['schema_version'] != 'mc2p.follow-playground-session.v3':
            raise ValueError('quality requires an explicit versioned run manifest')
        require_navigation_contract(manifest)
        from scripts.follow_playground_session import CORE_SOURCES, _hash
        from scripts.follow_playground_evidence import _actor_steps
        from scripts.active_perception_diagnostics import QualityDiagnostics
        sources = manifest['core_sources']
        if (set(sources)!=set(CORE_SOURCES) or any(type(v) is not str or re.fullmatch('[0-9a-f]{64}',v) is None
                                                 for v in sources.values())):
            raise ValueError('quality source fingerprint set is incomplete')
        sources_before={name:_hash(ROOT/name) for name in CORE_SOURCES}
        if sources_before!=sources:
            raise ValueError('quality source fingerprints differ from current evaluator checkout')
        worker = json.loads((directory/'worker-result.json').read_text('utf-8'))
        parent = json.loads((directory/'result.json').read_text('utf-8'))
        base = json.loads((directory/'case-base-evidence.json').read_text('utf-8'))
        require_navigation_contract(base)
        required = {'parent_and_worker_complete','all_scheduled_owner_commands_applied',
                    'each_applied_start_has_actual_task','MC2PFollower:complete_zero_image_runtime_and_time',
                    'MC2PLeader:complete_zero_image_runtime_and_time'}
        if (worker['core_sources_after']!=sources or parent['state']!='closed' or parent['primary_failure']
                or base['schema_version']!='mc2p.playground-case-evidence.v1' or base['case']!='active-quality'
                or base.get('measurement_revision')!=MEASUREMENT_REVISION
                or base['passed'] is not True or not required<={c['name'] for c in base['checks'] if c['passed'] is True}
                or not all(c['passed'] is True for c in base['checks'])):
            raise ValueError('quality requires completed lawful base checks and stable source fingerprints')
        plan = json.loads((directory/'case-plan.json').read_text('utf-8'))
        if plan != quality_case_plan() or manifest['measurement_revision']!=MEASUREMENT_REVISION:
            raise ValueError('quality scene or measurement revision differs')
        start = json.loads((directory/'case-start.json').read_text('utf-8'))
        if start['case']!='active-quality' or type(start['started_at_ns']) is not int or start['started_at_ns']<=0:
            raise ValueError('invalid quality case start')
        started_ns = start['started_at_ns']
        diagnostics = {p['id']:QualityDiagnostics(started_ns+p['start_ns'],
            started_ns+p['start_ns']+p['duration_ns'],manifest['perception_variant']) for p in plan['phases']}
        origin = json.loads((directory/'quality-origin.json').read_text('utf-8'))
        if origin['schema_version']!='mc2p.quality-origin.v1':
            raise ValueError('invalid quality origin')
        origin_position = Vec3V0(*origin['position'])
        bindings = origin.get('clock_bindings',{})
        if (origin.get('clock_source')!='time.perf_counter_ns' or type(bindings) is not dict
                or len(bindings)!=2 or len(set(bindings.values()))!=1):
            raise ValueError('quality controller clock association invalid')
        roles, sensor_contract, origin_seen = {}, None, False
        normal_input_proofs = {}
        for role in ('MC2PLeader','MC2PFollower'):
            samples = []
            track_id = None
            for raw, action, context in _actor_steps(directory/'runtime'/role,
                    perception_variant=manifest['perception_variant'] if role=='MC2PFollower' else 'm6_baseline',
                    perception_config=manifest['perception_config'] if role=='MC2PFollower' else None):
                perception = raw['perception']['value']
                if perception is not None:
                    sensor = {key:perception[key] for key in (
                        'horizontal_fov_degrees','vertical_fov_degrees','ray_columns','ray_rows',
                        'max_block_distance','body_expansion_blocks','block_epsilon_blocks',
                        'entity_max_distance','entity_occlusion_epsilon_blocks')}
                    sensor['sensor_profile_revision'] = perception.get('sensor_profile_revision', 1)
                    if sensor != dict(sensor_profile_revision=3,
                            horizontal_fov_degrees=120.,vertical_fov_degrees=120.,ray_columns=159,ray_rows=9,
                            max_block_distance=16.,body_expansion_blocks=.05,block_epsilon_blocks=.001,
                            entity_max_distance=32.,entity_occlusion_epsilon_blocks=.05):
                        raise ValueError('current quality run requires sensor profile 3 (120x120, 1431 rays)')
                    if sensor_contract is None:
                        sensor_contract = sensor
                    if sensor != sensor_contract:
                        raise ValueError('quality sensor contract changed within run')
                    if role=='MC2PFollower' and track_id is None:
                        players=[e for e in perception['visible_entities'] if e['entity_type']=='minecraft:player']
                        if len(players)==1:
                            track_id=players[0]['track_id']
                task_id = None
                associated_action = action
                if role=='MC2PFollower':
                    associated_action = None
                    if context is not None:
                        actual_track = context['decision']['target']['track_id']
                        if track_id is not None and actual_track!=track_id:
                            raise ValueError('quality task rebound a different target')
                        track_id=actual_track
                        selected=context['selected']
                        if selected.get('movement')==selected.get('look')==context['accepted_intent_id']:
                            associated_action=action
                            task_id=context['task_id']
                sample = quality_sample(raw,associated_action,track_id,task_id)
                if role=='MC2PFollower':
                    for diagnostic in diagnostics.values():
                        diagnostic.observe(raw,action,context)
                if sample is None:
                    continue
                if len(samples)>=MAX_SAMPLES:
                    raise ValueError('quality actual-pose sample capacity exceeded')
                samples.append(sample)
                if role=='MC2PLeader':
                    if action is not None:
                        movement=action['movement']
                        normal_input_proofs[sample_identity(sample)] = (movement['forward'] in (0,1)
                            and not any(movement[k] for k in ('strafe','sprint','sneak','jump'))
                            and action['operation'] is None and not raw['self_state']['value']['is_flying'])
                    if (sample.episode_id==origin['episode_id'] and sample.sequence_id==origin['observation_sequence_id']
                            and sample.controller_clock_id==origin['controller_clock_id']):
                        if sample.position!=origin_position:
                            raise ValueError('quality origin is not the actual leader pose')
                        origin_seen=True
            roles[role]=tuple(samples)
        if not origin_seen or sensor_contract is None:
            raise ValueError('quality origin or actual sensor evidence absent')
        leader_samples,follower_samples=roles['MC2PLeader'],roles['MC2PFollower']
        if not leader_samples or not follower_samples:
            raise ValueError('quality needs actual samples from both bodies')
        clocks={s.controller_clock_id for seq in roles.values() for s in seq}
        if len(clocks)!=2 or set(bindings)!=clocks or len(set(bindings.values()))!=1:
            raise ValueError('quality cross-role controller clock source is unproven')
        for phase in plan['phases']:
            begin=started_ns+phase['start_ns']; end=begin+phase['duration_ns']
            detail=summarize_phase(follower_samples,start_ns=begin,end_ns=end)
            detail['angular_window']=angular_window_report(follower_samples,begin)
            goal=(origin_position.x+phase['waypoints'][-1][0],origin_position.z+phase['waypoints'][-1][1])
            inside=tuple(s for s in follower_samples if begin<=s.controller_ns<=end)
            completed=completion_seconds(inside,leader_samples,goal,begin,clock_bindings=bindings)
            route=leader_route_report(leader_samples,phase,origin_position,begin,end)
            normal=leader_normal_input_report(leader_samples,normal_input_proofs,begin,end)
            detail.update(id=phase['id'],kind=phase['kind'],mode=phase['mode'],completion_seconds=completed,
                          leader_route=route,leader_normal_inputs=normal['passed'],
                          leader_normal_input=normal,diagnostics=diagnostics[phase['id']].report())
            detail=classify_phase(detail,completed=completed,route_passed=route['passed'],normal_passed=normal['passed'])
            result['phases'].append(detail)
        sources_after={name:_hash(ROOT/name) for name in CORE_SOURCES}
        if sources_after!=sources_before or sources_after!=sources:
            raise ValueError('quality sources changed during evaluation')
        measured=all(p['measurement_status']=='passed' for p in result['phases'])
        completed=all(p['task_status']=='passed' for p in result['phases'])
        result.update(status=('passed' if completed else 'behavior_failed') if measured else 'inconclusive',
            measurement_status='passed' if measured else 'inconclusive',task_status='passed' if completed else 'failed',
            baseline_admissible=measured and manifest['perception_variant'] in {'m6_baseline','smooth_only'},
            seed=manifest['seed'],perception_variant=manifest['perception_variant'],perception_config=manifest['perception_config'],
            measurement_revision=MEASUREMENT_REVISION,scenario_plan=plan,sensor_contract=sensor_contract,
            source_fingerprints=sources,clock_bindings=bindings,**dict(NAVIGATION_CONTRACT),
            samples_by_role={name:len(samples) for name,samples in roles.items()})
    except (OSError,ValueError,KeyError,TypeError) as error:
        result['errors'].append(type(error).__name__+': '+str(error))
    return result


def leader_route_report(samples, phase, origin, begin_ns, end_ns):
    points=[(origin.x+x,origin.z+z) for x,z in phase['waypoints']]
    previous=(origin.x,origin.z) if phase['id']==0 else (
        origin.x+quality_case_plan()['phases'][phase['id']-1]['waypoints'][-1][0],
        origin.z+quality_case_plan()['phases'][phase['id']-1]['waypoints'][-1][1])
    segments=list(zip([previous]+points,points))
    inside=[s for s in samples if begin_ns<=s.controller_ns<=end_ns]
    def segment_distance(position, a, b):
        dx,dz=b[0]-a[0],b[1]-a[1]
        fraction=max(0.,min(1.,((position.x-a[0])*dx+(position.z-a[1])*dz)/(dx*dx+dz*dz)))
        return math.hypot(position.x-a[0]-fraction*dx,position.z-a[1]-fraction*dz)
    duration=end_ns-begin_ns
    if duration<=0:
        raise ValueError('leader route interval must be positive')
    coverage=_sample_coverage_ns(tuple(samples),begin_ns,end_ns)/duration
    visited=0; max_deviation=0.; pause_ns=0; pause_run_ns=0; prior=None
    for sample in inside:
        max_deviation=max(max_deviation,min(segment_distance(sample.position,a,b) for a,b in segments))
        if visited<len(points) and math.hypot(sample.position.x-points[visited][0],sample.position.z-points[visited][1])<=.25:
            visited+=1
        if phase['kind']=='quality_stop_go' and prior is not None:
            dt=sample.controller_ns-prior.controller_ns
            if (continuous_samples(prior,sample)
                    and all(math.hypot(s.position.x-points[0][0],s.position.z-points[0][1])<=.25 for s in (prior,sample))):
                pause_run_ns+=dt
                pause_ns=max(pause_ns,pause_run_ns)
            else:
                pause_run_ns=0
        prior=sample
    return dict(passed=bool(inside) and coverage>=MIN_COVERAGE
                and visited==len(points) and max_deviation<=.35
                and (phase['kind']!='quality_stop_go' or pause_ns>=2_000_000_000),
                waypoints_observed=visited,max_corridor_deviation_blocks=max_deviation,
                first_waypoint_near_seconds=pause_ns/1e9,sample_count=len(inside),
                valid_coverage_fraction=coverage)


def compare_quality(directories: tuple[Path, ...]) -> dict:
    if type(directories) is not tuple or len(directories)>9:
        raise ValueError('quality comparison accepts at most nine explicit runs')
    return compare_reports(tuple(summarize_quality(path) for path in directories))


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs',type=Path,nargs='+',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    output=_safe_path(args.output)
    if output.exists():
        raise FileExistsError('quality output already exists: '+str(output))
    result=compare_quality(tuple(args.runs))
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,allow_nan=False,indent=2)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
