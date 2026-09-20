"""Read sealed evidence. Executed in an isolated process for each source version."""
import argparse
import hashlib
import json
import math
import re
from pathlib import Path
import sys

VERSION = 14


def waiting_record(diagnostic):
    """Project only recorded waiting facts; old traces remain explicitly absent."""
    value=diagnostic.get('navigation_waiting')
    if not isinstance(value,dict):return None
    return dict(revision=value.get('revision'),category=value.get('category'),
        reason=value.get('reason'),
        temporary_candidate=coordinate(value.get('temporary_candidate')),
        formal_checkpoint=coordinate(value.get('formal_checkpoint')))


def search_schedule_record(diagnostic):
    value=diagnostic.get('navigation_search_schedule')
    return value if isinstance(value,dict) and value.get('revision')==2 else None


def choice_timing_record(diagnostic,start):
    value=diagnostic.get('navigation_choice_timing')
    if not isinstance(value,dict):return None
    return dict(publication=value.get('publication'),**{
        name.replace('_ns','_t'):(None if value.get(name) is None else (value[name]-start)/1e9)
        for name in ('requested_ns','published_ns','eligible_ns','adopted_ns')})


def change_event(change, report, start):
    """Read commands as data only; submission is not proof of world mutation."""
    blocks=[];spawn=None
    for command in change['commands']:
        match=re.fullmatch(r'setblock (-?\d+) (-?\d+) (-?\d+) (minecraft:[a-z_]+)',command)
        if match:
            x,y,z,block=match.groups();blocks.append(dict(x=int(x),y=int(y),z=int(z),block=block))
        elif command.startswith('summon '):
            fields=command.split();spawn=dict(type=fields[1],position=[float(v) for v in fields[2:5]])
        else:raise ValueError('网页尚不支持该地形事件格式')
    verified=report if report and report.get('submitted_at_ns')==change['submitted_at_ns'] and report.get('case',change['case'])==change['case'] else {}
    confirmed=verified.get('first_legal_change_ns')
    return dict(t=(change['submitted_at_ns']-start)/1e9,
                confirmed_t=None if confirmed is None else (confirmed-start)/1e9,
                case=change['case'],blocks=blocks,spawn=spawn,
                entity_id=verified.get('new_entity_track_id'))


def visible_entities(raw):
    perception=raw.get('perception',{});own=raw.get('self_state',{})
    if perception.get('status')!='valid' or own.get('status')!='valid':return []
    position=coordinate(own['value']['position'])
    return [dict(id=e['track_id'],type=e['entity_type'],
                 position=[a+b for a,b in zip(position,coordinate(e['relative_position']))],
                 size=coordinate(e['bounding_box_size']))
            for e in perception['value'].get('visible_entities',[])]


def read(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def safe_child(root, name):
    candidate=(root/name).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError('封存路径越界')
    return candidate


def read_stream(directory):
    directory=Path(directory)
    complete=read(directory/'complete.json')
    manifest=directory/'manifest.jsonl'
    if sha(manifest)!=complete['manifest_sha256']:
        raise ValueError('分段清单哈希不一致')
    count=0;size=0;segments=0
    for line in manifest.read_text('utf-8').splitlines():
        entry=json.loads(line)
        if entry['index']!=segments or entry['first_record_ordinal']!=count:
            raise ValueError('分段顺序不连续')
        path=safe_child(directory,entry['filename'])
        if path.stat().st_size!=entry['byte_count'] or sha(path)!=entry['sha256']:
            raise ValueError('分段内容损坏：'+path.name)
        local=0
        with path.open('rb') as stream:
            while raw:=stream.readline(8*1024*1024+1):
                if len(raw)>8*1024*1024 or not raw.endswith(b'\n'):
                    raise ValueError('记录过长或未完整写入')
                yield json.loads(raw)
                local+=1
        if local!=entry['record_count'] or entry['last_record_ordinal']!=count+local-1:
            raise ValueError('分段记录数不一致')
        count+=local;size+=entry['byte_count'];segments+=1
    if (count,size,segments)!=(complete['record_count'],complete['byte_count'],complete['segment_count']):
        raise ValueError('封存计数不一致')


def memory_delta(before, after):
    return dict(remove=sorted(before.keys()-after.keys()),
                upsert=[after[k] for k in sorted(after) if before.get(k)!=after[k]])


def terrain_record(record, seconds):
    result=cell_record(record.block)+[round(seconds(record.last_seen.request_start_ns),6)]
    current=getattr(record,'current',None)
    if current is not None:
        batch=getattr(record,'history_batch_ns',None)
        result.append(dict(model='current_history_5s',current=current,
            history_batch_start=None if batch is None else seconds(batch)))
    return result


def coordinate(value):
    if value is None:return None
    return [value[k] for k in ('x','y','z')] if isinstance(value,dict) else list(value)


def cell_key(position):
    return ','.join(str(v) for v in position)


def cell_record(block):
    return [*block.position,block.block_id,str(block.collision.kind),block.fluid_id,list(block.sources)]


def protected_inferences(blocks, current, accepted):
    """Only mark an inference rejected by archived effective-memory merging."""
    return [cell_key(b['position']) for b in blocks
            if b.get('sources') == ['inferred_air']
            and cell_key(b['position']) not in accepted
            and cell_key(b['position']) in current
            and current[cell_key(b['position'])][3] not in
                ('minecraft:air', 'minecraft:cave_air', 'minecraft:void_air')]


def archived_memory(evidence,run):
    archive=evidence/'source-archive'
    manifest=read(archive/'manifest.json')
    if manifest['tree_sha256']!=run['source_archive']['tree_sha256']:
        raise ValueError('源码版本与运行清单不一致')
    files=archive/'files'
    fingerprints=manifest['source_fingerprints']
    for name,digest in fingerprints.items():
        if name.endswith('.py'):
            if sha(safe_child(files,name))!=digest:
                raise ValueError('封存 Python 源码损坏：'+name)
    sys.path.insert(0,str(files))
    from mc2p.skills.navigation_state import NavigationState
    from scripts.navigation_motion_evidence import restore_snapshot
    for name,module in tuple(sys.modules.items()):
        if name.startswith(('mc2p.','scripts.')) and getattr(module,'__file__',None):
            path=Path(module.__file__).resolve()
            if not path.is_relative_to(files.resolve()) or path.relative_to(files.resolve()).as_posix() not in fingerprints:
                raise ValueError('记忆重放混入未封存模块：'+name)
    options={'retain_terrain':True} if run.get('joint_configuration',{}).get('retained_terrain_revision')==1 else {}
    if run.get('joint_configuration',{}).get('radius_history_revision')==1:options={'radius_history':True}
    return NavigationState(run['task_binding']['scope_id'],**options),restore_snapshot


def extract_run(evidence):
    evidence=Path(evidence).resolve()
    run=read(evidence/'run-manifest.json');terminal=read(evidence/'terminal.json')
    start=run['controller_clock']['started_at_ns'];end=terminal['ended_at_ns']
    seconds=lambda ns:(ns-start)/1e9
    plan=run['case_plan'];layout=plan['layout'];bounds=layout['bounds']
    initial=read(evidence/'initial-state.json')['task_start'];initial_body=None
    def in_bounds(p):
        return bounds['min_x']<=p[0]<=bounds['max_x'] and bounds['min_z']<=p[2]<=bounds['max_z']
    imports=[];state=None;restore=None;warnings=[];memory_error=None
    try:
        imports=read(evidence/'memory-observations.json')
        if not imports:raise ValueError('没有记忆导入记录')
        state,restore=archived_memory(evidence,run)
    except (OSError,ValueError,KeyError,ImportError) as error:
        memory_error=str(error)
        warnings.append('地形记忆不可用：'+memory_error)
    frames=[];observations=[];samples=[];decisions=[];entities=[];old={};index=0;intent=None;last_raw=None
    last_import_time=-1;ground=layout['ground_y'];imports_checked=0
    old_visual={};navigation_volume=False;current_history_batches=False
    for row in read_stream(evidence/'runtime-trace/trace'):
        kind=row['record_type'];payload=row.get('payload',{})
        raw=(payload.get('result',{}) if kind=='reset' else payload.get('backend_result',{})).get('observation')
        if raw:
            last_raw=raw
            now=raw['received_at_monotonic_ns'];own=raw.get('self_state',{}).get('value')
            if now<=end:entities.append(dict(t=seconds(now),items=visible_entities(raw)))
            if own and raw['sequence_id']==initial['sequence_id'] and raw['episode_id']==initial['episode_id']:
                initial_body=own
            if own and start<=now<=end:
                samples.append(dict(t=seconds(now),position=coordinate(own['position']),
                    velocity=coordinate(own['velocity']),yaw=own['yaw_degrees'],pitch=own['pitch_degrees'],
                    sequence=raw['sequence_id']))
            perception=raw.get('perception',{})
            navigation_volume = navigation_volume or bool((perception.get('value') or {}).get('navigation_geometry'))
            blocks=(perception.get('value') or {}).get('blocks',[]) if perception.get('status')=='valid' else []
            observed=[[*b['position'],b['block_id'],b['collision']['kind'],b.get('fluid_id'),b.get('sources',[])]
                      for b in blocks if in_bounds(b['position'])]
            if now<=end:
                observations.append(dict(t=seconds(now),cells=observed,
                                         seen=[cell_key(b[:3]) for b in observed],sequence=raw['sequence_id']))
            typed=None
            if state is not None:
                try:
                    while index<len(imports) and (imports[index]['episode_id'],imports[index]['sequence'])==(raw['episode_id'],raw['sequence_id']):
                        entry=imports[index];at=entry['observed_at_controller_ns']
                        if entry['scope_id']!=state.scope_id or at<max(last_import_time,now):
                            raise ValueError('记忆导入时序或作用域不一致')
                        if typed is None:typed=restore(raw)
                        snapshot=state.observe(typed,at)
                        if not current_history_batches:
                            current_history_batches=any(getattr(r,'current',None) is not None
                                                        for r in snapshot.terrain)
                        if len(snapshot.terrain)!=entry['records'] or len(snapshot.terrain_index.owners)!=entry['owner_tiles']:
                            raise ValueError('记忆重放数量与原记录不一致')
                        if at<=end:
                            current={cell_key(r.block.position):terrain_record(r,seconds)
                                     for r in snapshot.terrain if in_bounds(r.block.position)}
                            seen=[cell_key(b.position) for b in snapshot.latest.blocks if in_bounds(b.position)] if snapshot.latest else []
                            delta=memory_delta(old,current)
                            visual_delta={}
                            if getattr(snapshot.latest,'navigation_blocks',None) is not None:
                                seen=[cell_key(b.position) for b in snapshot.latest.terrain_blocks if in_bounds(b.position)]
                                visual={cell_key(r.block.position):cell_record(r.block)+[seconds(r.last_seen.request_start_ns)]
                                        for r in snapshot.visual_terrain if in_bounds(r.block.position)}
                                vd=memory_delta(old_visual,visual)
                                visual_delta=dict(visual_upsert=vd['upsert'],visual_remove=vd['remove'],
                                                  visual_seen=[cell_key(b['position']) for b in blocks if in_bounds(b['position'])])
                                old_visual=visual
                            frames.append(dict(t=seconds(at),seen=seen,**delta,sequence=raw['sequence_id'],total=len(snapshot.terrain),
                                protected=protected_inferences(blocks,current,set(seen)),**visual_delta))
                            old=current
                        index+=1;imports_checked+=1;last_import_time=at
                except Exception as error:
                    memory_error=type(error).__name__+': '+str(error)
                    warnings.append('地形记忆重放失败：'+memory_error)
                    state=None;frames=[]
        if kind=='ordered_intent':
            intent=payload['envelope']['intent']
        if kind=='playground_task' and intent is not None:
            at=intent['submitted_at_monotonic_ns']
            if not start<=at<=end:continue
            d=payload.get('planning_diagnostic',{});stage=d.get('stage_goal',{})
            target=coordinate(stage.get('target'))
            path=[coordinate(p) for p in stage.get('path',[]) if p is not None]
            continuation=d.get('execution_continuity',{})
            if continuation.get('active') and continuation.get('checked_path'):
                path=[coordinate(p) for p in continuation['checked_path']]
            if not path and d.get('original_route'):path=[coordinate(p) for p in d['original_route']]
            decisions.append(dict(t=seconds(at),reason=payload.get('reason'),state=payload.get('state'),
                movement=payload.get('movement'),look=payload.get('look'),target=target,path=path,
                waypoint=coordinate(payload.get('selected_waypoint')),scanning=stage.get('scanning',False),
                planning_ms=None if d.get('elapsed_ns') is None else d['elapsed_ns']/1e6,
                rejection=d.get('submit_rejection'),route=d.get('route_id'),
                needs=d.get('needs',[]),memory_generation=(d.get('proposal') or {}).get('memory_generation'),
                lifecycle=d.get('execution_buffer',{}).get('stage_lifecycle'),exploration=d.get('exploration'),
                route_search_scheduling=d.get('execution_buffer',{}).get('route_search_scheduling'),
                search_schedule=search_schedule_record(d),
                waiting=waiting_record(d),
                hierarchical_navigation_revision=d.get('hierarchical_navigation_revision',0),
                navigation_layer_timing=d.get('navigation_layer_timing'),
                navigation_route_layers=d.get('navigation_route_layers'),
                navigation_local_trajectory=d.get('navigation_local_trajectory'),
                navigation_control_deadline=d.get('navigation_control_deadline'),
                navigation_impact=d.get('navigation_impact'),
                choice_timing=choice_timing_record(d,start),
                observation_attempts=d.get('observation_attempts'),
                navigation_observation=d.get('navigation_observation'),
                navigation_purpose=d.get('navigation_purpose'),
                observation_travel_revision=d.get('observation_travel_revision',0),
                exploration_integration=d.get('exploration_integration'),
                execution_continuity=d.get('execution_continuity'),
                observation_opportunity=d.get('observation_opportunity'),fine_gaze=d.get('fine_gaze'),
                route_handoff=d.get('motion_control',{}).get('handoff'),
                execution_origin=coordinate((d.get('proposal') or {}).get('origin'))))
    if state is not None and index!=len(imports):
        memory_error='记忆导入记录没有完整对应到原始观察';warnings.append(memory_error);frames=[]
    # Consume and validate the independent body trace, without substituting plans for samples.
    trajectory=[]
    for row in read_stream(evidence/'trajectory'):
        if row['record_type']=='sample':
            s=row['sample'];at=s['received_at_ns']
            if start<=at<=end:trajectory.append(s)
    if not samples:raise ValueError('任务时间范围内没有身体采样')
    if initial_body is None:raise ValueError('未找到任务起始位置对应的实际身体观察')
    initial_sample=dict(t=0,position=coordinate(initial['position']),velocity=coordinate(initial_body['velocity']),
                        yaw=initial_body['yaw_degrees'],pitch=initial_body['pitch_degrees'],sequence=initial['sequence_id'])
    samples.insert(0,initial_sample)
    samples.sort(key=lambda s:s['t']);decisions.sort(key=lambda d:d['t'])
    distance=sum(math.hypot(b['position'][0]-a['position'][0],b['position'][2]-a['position'][2]) for a,b in zip(samples,samples[1:]))
    summary_path=evidence.parent/'evidence-result.json'
    result=read(summary_path) if summary_path.exists() else {}
    changes=[]
    if (evidence/'terrain-change.json').exists():
        changes.append(change_event(read(evidence/'terrain-change.json'),result.get('terrain_change'),start))
    return dict(schema='mc2p.navigation-web-replay.v1',parser_version=VERSION,
        source=run.get('source_archive',{}).get('tree_sha256'),duration=seconds(end),
        case=plan['case'],seed=plan['seed'],layout=layout,start=coordinate(initial['position']),navigation_volume=navigation_volume,
        current_history_batches=current_history_batches,
        goal=coordinate(plan['actor_task']['position']),samples=samples,decisions=decisions,
        knowledge=frames,observations=observations if not frames else [],entities=entities,changes=changes,
        memory=dict(available=bool(frames),imports_checked=imports_checked,imports_expected=len(imports),error=memory_error,
                    retained_in_radius=(run.get('joint_configuration',{}).get('retained_terrain_revision')==1
                        or run.get('joint_configuration',{}).get('radius_history_revision')==1)),
        warnings=warnings,summary=dict(outcome=result.get('algorithm_outcome','未记录'),
            engineering=result.get('engineering_status','未记录'),path_length=distance,
            final_distance=math.hypot(samples[-1]['position'][0]-plan['actor_task']['position'][0],samples[-1]['position'][2]-plan['actor_task']['position'][2])),
        map_note='评测底图仅供人查看：斜线表示坑；事件提交后标出待确认区域，合法观察确认后更新地形。村民只在当前观察中显示，不推测离开视野后的运动。')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('evidence',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args()
    data=extract_run(args.evidence)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf-8')
    print(json.dumps(dict(samples=len(data['samples']),memory=data['memory'],seconds=data['duration']),ensure_ascii=False))


if __name__=='__main__':main()
