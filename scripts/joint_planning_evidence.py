"""Validate bounded J4 JSON diagnostics without treating forecasts as observations.

This checks a single planning record, not execution or historical selection.
The raw-stream evaluator must bind its proposal to the actual intent and later
observations. D/E hysteresis cannot be replayed without the previous selection.
"""
from __future__ import annotations

import math

from mc2p.skills.normal_navigation_types import NormalNavigationConfig


def _keys(value, required, label, optional=()):
    if type(value) is not dict or not set(required) <= set(value) or set(value)-set(required)-set(optional):
        raise ValueError(label+' fields differ')


def _int(value, label, minimum=0):
    if type(value) is not int or value < minimum or value > 2**63-1:
        raise ValueError(label+' must be a bounded integer')
    return value


def _number(value, label, minimum=None):
    if (type(value) not in (int, float) or (type(value) is int and abs(value) > 2**63-1)
            or not math.isfinite(value) or (minimum is not None and value < minimum)):
        raise ValueError(label+' must be finite and in range')
    return value


def _text(value, label, maximum=160):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValueError(label+' must be bounded text')


def _list(value, maximum, label):
    if type(value) is not list or len(value) > maximum:
        raise ValueError(label+' exceeds bounds')


def _cell(value, dimensions):
    if type(value) is not list or len(value) != dimensions or any(type(x) is not int or abs(x) > 2**63-1 for x in value):
        raise ValueError('planning cell is invalid')


def _vector(value):
    _keys(value, {'x', 'y', 'z'}, 'proposal vector')
    for component in value.values():
        _number(component, 'proposal vector')


def _sample(value):
    _keys(value, {'clock_id', 'started_at_monotonic_ns', 'completed_at_monotonic_ns'}, 'client sample')
    _text(value['clock_id'], 'client sample clock')
    if _int(value['started_at_monotonic_ns'], 'sample start') > _int(value['completed_at_monotonic_ns'], 'sample end'):
        raise ValueError('client sample interval reversed')


def _proposal(value):
    _keys(value, {'scope_id', 'memory_generation', 'based_on', 'movement', 'look',
                  'origin', 'velocity', 'yaw', 'pitch', 'endpoint'}, 'proposal')
    _text(value['scope_id'], 'proposal scope')
    _int(value['memory_generation'], 'memory generation', 1)
    stamp = value['based_on']
    _keys(stamp, {'episode_id', 'sequence_id', 'request_sequence_id', 'controller_clock_id',
                  'client_sample', 'request_start_ns', 'received_at_ns', 'source_backend'}, 'proposal evidence')
    for field in ('episode_id', 'controller_clock_id', 'source_backend'):
        _text(stamp[field], 'evidence '+field)
    _int(stamp['sequence_id'], 'evidence sequence')
    if stamp['request_sequence_id'] is not None:
        _int(stamp['request_sequence_id'], 'request sequence')
    if _int(stamp['request_start_ns'], 'request start') > _int(stamp['received_at_ns'], 'receipt'):
        raise ValueError('proposal evidence receipt precedes request')
    _sample(stamp['client_sample'])
    movement = value['movement']; look = value['look']
    _keys(movement, {'forward', 'strafe', 'jump', 'sneak', 'sprint'}, 'proposal movement')
    if any(type(movement[k]) is not int or movement[k] not in (-1, 0, 1) for k in ('forward', 'strafe')) or any(movement[k] is not False for k in ('jump', 'sneak', 'sprint')):
        raise ValueError('proposal movement is not normal discrete control')
    _keys(look, {'yaw_delta_degrees', 'pitch_delta_degrees'}, 'proposal look')
    if math.hypot(*(_number(look[k], 'look delta') for k in ('yaw_delta_degrees', 'pitch_delta_degrees'))) > 3.+1e-9:
        raise ValueError('proposal look exceeds single-tick rate')
    for key in ('origin', 'velocity', 'endpoint'):
        _vector(value[key])
    _number(value['yaw'], 'proposal yaw'); _number(value['pitch'], 'proposal pitch')


def _feedback(value):
    if value == {}:
        return
    _keys(value, {'executed', 'received_at_ns', 'lateral_error_blocks', 'acquired_ns',
                  'information_lead_ns', 'need_id', 'required_by_ns', 'candidate_id', 'route_id'},
          'feedback', {'client_sample', 'failure_reason', 'force_route_completed_ns'})
    if type(value['executed']) is not bool:
        raise ValueError('feedback execution flag is invalid')
    received = _int(value['received_at_ns'], 'feedback receipt')
    for key in ('need_id', 'candidate_id', 'route_id'):
        if value[key] is not None:
            _text(value[key], 'feedback '+key)
    if value['lateral_error_blocks'] is not None:
        _number(value['lateral_error_blocks'], 'feedback lateral error')
    if value['required_by_ns'] is not None:
        _int(value['required_by_ns'], 'feedback required time')
    if value['acquired_ns'] is None:
        if value['information_lead_ns'] is not None or 'client_sample' in value:
            raise ValueError('feedback without acquisition claims information')
    else:
        acquired = _int(value['acquired_ns'], 'actual acquisition')
        if (value['need_id'] is None or value['required_by_ns'] is None or acquired > received
                or type(value['information_lead_ns']) is not int
                or value['information_lead_ns'] != value['required_by_ns']-acquired):
            raise ValueError('feedback actual acquisition/lead binding differs')
        _sample(value.get('client_sample'))
    if 'failure_reason' in value:
        _text(value['failure_reason'], 'feedback failure', 80)
    if 'force_route_completed_ns' in value:
        if (not value['executed'] or value['route_id'] is None
                or _int(value['force_route_completed_ns'], 'feedback force completion') > received):
            raise ValueError('feedback force completion lacks executed receipt')


def validate_execution_diagnostic(value,now_ns):
    """Check revision-1 execution arithmetic without importing actor state."""
    fields={'execution_recovery_revision','phase','revision','started_ns','problem_started_ns',
        'last_progress_ns','attempts','feedback_high_water','attempt_high_water','reason','problem_key',
        'deadline_reason','problem_elapsed_ns','no_progress_elapsed_ns','task_elapsed_ns',
        'last_position','last_executed','task_timeout_ns','problem_timeout_ns',
        'no_progress_timeout_ns','max_attempts'}
    _keys(value,fields,'execution diagnostic');_int(now_ns,'execution current time')
    if type(value['execution_recovery_revision']) is not int or value['execution_recovery_revision']!=1:
        raise ValueError('execution revision differs')
    if value['phase'] not in {'tracking','adjusting','observing','retreating','replanning','waiting','blocked','suspended'}:
        raise ValueError('execution phase differs')
    for field,expected in [('task_timeout_ns',90_000_000_000),('problem_timeout_ns',12_000_000_000),
            ('no_progress_timeout_ns',3_000_000_000),('max_attempts',12)]:
        if _int(value[field],field)!=expected:raise ValueError('execution configured bound differs')
    for field in ('revision','attempts'):_int(value[field],'execution '+field)
    for field in ('feedback_high_water','attempt_high_water'):_int(value[field],field,-1)
    for field in ('started_ns','problem_started_ns','last_progress_ns'):
        if value[field] is not None and _int(value[field],field)>now_ns:
            raise ValueError('execution timestamp exceeds current time')
    for field in ('reason','problem_key'):
        if value[field] is not None:_text(value[field],field,256)
    if value['last_executed'] is not None and type(value['last_executed']) is not bool:
        raise ValueError('execution actual execution flag differs')
    if value['last_position'] is not None:
        if type(value['last_position']) is not list or len(value['last_position'])!=3:
            raise ValueError('execution position differs')
        for coordinate in value['last_position']:_number(coordinate,'execution position')
    start,problem,progress=(value[k] for k in ('started_ns','problem_started_ns','last_progress_ns'))
    if ((start is None)!=(progress is None) or (start is None and problem is not None)
            or (start is not None and (progress<start or (problem is not None and problem<start)))):
        raise ValueError('execution timestamps lack their task binding')
    expected_elapsed={'task_elapsed_ns':None if start is None else now_ns-start,
        'problem_elapsed_ns':None if problem is None else now_ns-problem,
        'no_progress_elapsed_ns':None if problem is None else now_ns-max(problem,progress)}
    for field,expected in expected_elapsed.items():
        if expected is not None:_int(value[field],field)
        if value[field]!=expected:raise ValueError('execution elapsed arithmetic differs')
    deadline=None
    if start is not None and now_ns-start>=value['task_timeout_ns']:deadline='task_deadline'
    elif problem is not None:
        if now_ns-problem>=value['problem_timeout_ns']:deadline='problem_deadline'
        elif value['attempts']>=value['max_attempts']:deadline='attempts_exhausted'
        elif now_ns-max(problem,progress)>=value['no_progress_timeout_ns']:deadline='no_progress'
    if value['deadline_reason']!=deadline:raise ValueError('execution deadline differs')
    if ((value['feedback_high_water']==-1)!=(value['last_executed'] is None)
            or (value['attempts']==0)!=(value['attempt_high_water']==-1)):
        raise ValueError('execution counters lack actual feedback')


def _possible_selection(selected, candidates, value, group):
    """Check necessary selection rules, without inventing a previous selection."""
    eligible = list(candidates.values())
    if value['force_route_id'] is not None and not value['force_route_completed']:
        eligible = [row for row in eligible if row['route_id'] == value['force_route_id']]
    if not any(row['proposal']['movement']['forward'] or row['proposal']['movement']['strafe'] for row in eligible):
        sensing = [row for row in eligible if row['need_id'] is not None]
        if sensing:
            eligible = sensing
    if selected not in eligible:
        raise ValueError('selected candidate failed common selection filters')
    if group == 'D':
        best = min(eligible, key=lambda c: (c['route_score'], c['route_id']))
        if (selected['route_id'] != best['route_id']
                and selected['route_score']-best['route_score'] >= .10+1e-12):
            raise ValueError('D route selection exceeds independent hysteresis')
        def gaze_key(row):
            return (row['need_priority'], row['required_by_ns'] if row['required_by_ns'] is not None else 2**63,
                    row['need_id'] or '~', row['gaze_debt'], row['candidate_id'])
        best_gaze = min((row for row in eligible if row['route_id'] == selected['route_id']), key=gaze_key)
        if selected != best_gaze:
            raise ValueError('D gaze priority/deadline selection differs')
    else:
        best = min(eligible, key=lambda c: (c['score'], c['candidate_id']))
        if selected != best:
            earlier = (best['need_priority'] == selected['need_priority']
                       and best['required_by_ns'] is not None
                       and (selected['required_by_ns'] is None or best['required_by_ns'] < selected['required_by_ns']))
            if (selected['score']-best['score'] >= .10+1e-12
                    or selected['need_priority'] > best['need_priority'] or earlier):
                raise ValueError('E selection cannot be explained by allowed hysteresis')


def validate_planning_diagnostic(value, run) -> None:
    """Reject malformed, unbounded or internally inconsistent raw J4 records.

The function deliberately makes no optimality or actual-acquisition claim:
previous-frame hysteresis and post-observation source matching are separate
raw-stream checks. Both D and E use the same candidate and arithmetic contract.
"""
    from mc2p.skills.navigation_strategy import GOAL_DIRECTED_EXPLORATION, JOINT_STRATEGIES, strategy_config
    if type(run) is not dict or run.get('group') not in JOINT_STRATEGIES:
        raise ValueError('joint planning requires a D/E run manifest')
    configuration = run.get('joint_configuration')
    if type(configuration) is not dict:
        raise ValueError('joint planning lacks configuration')
    common = {'planned', 'expansions', 'cells_checked', 'candidates', 'cell_rejections',
              'segment_rejections', 'elapsed_ns', 'planning_budget_ns', 'budget_exhausted',
              'summary_truncated', 'joint_revision', 'joint_candidates', 'needs',
              'selected_candidate_id', 'route_id', 'proposal', 'acquired_ns', 'feedback',
              'prelook_enabled', 'force_route_id', 'force_route_completed', 'force_route_completed_ns'}
    extra = {'route_count', 'original_route', 'need_id', 'required_by_ns', 'expected_acquired_ns'}
    staged=configuration.get('stage_goals_enabled',False)
    named=run['group']==GOAL_DIRECTED_EXPLORATION
    if named:
        common.add('navigation_strategy')
        if not staged or value.get('navigation_strategy')!=GOAL_DIRECTED_EXPLORATION:
            raise ValueError('navigation strategy identity differs')
    if type(staged) is not bool:raise ValueError('stage switch differs')
    static_history=configuration.get('terrain_history_revision')==1
    if 'terrain_history_revision' in configuration and (not named
        or type(configuration['terrain_history_revision']) is not int or not static_history):
        raise ValueError('static terrain configuration differs')
    execution=configuration.get('execution_recovery_revision')==1
    if 'execution_recovery_revision' in configuration and (not static_history
            or type(configuration['execution_recovery_revision']) is not int or not execution):
        raise ValueError('execution recovery configuration differs')
    execution_cost=configuration.get('execution_cost_revision')==1
    if {'execution_cost_revision','motion_review_revision'} & set(configuration):
        if (not execution or type(configuration.get('execution_cost_revision')) is not int or not execution_cost
                or type(configuration.get('motion_review_revision')) is not int or configuration['motion_review_revision']!=2):
            raise ValueError('execution cost/motion review revision differs')
    if execution:
        common.add('execution')
        execution_value=value.get('execution')
        if type(execution_value) is not dict:raise ValueError('execution diagnostic missing')
        started=execution_value.get('started_ns');elapsed=execution_value.get('task_elapsed_ns')
        now_ns=0 if started is None else _int(started,'execution start')+_int(elapsed,'execution elapsed')
        validate_execution_diagnostic(execution_value,now_ns)
    reuse=staged and value.get('joint_revision') in (3,4,5)
    if static_history:
        common.add('terrain_review')
        review=value.get('terrain_review')
        _keys(review,{'evaluated','path','state_sha256'},'terrain review')
        if type(review['evaluated']) is not bool:raise ValueError('terrain review evaluation flag differs')
        _list(review['path'],258,'terrain review path')
        for point in review['path']:_vector(point)
        if not review['evaluated'] and review['path']:raise ValueError('unevaluated terrain review has path')
        digest=review['state_sha256']
        if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('terrain review state digest differs')
    _keys(value, common | ({'stage_goal'} if staged else set()) | ({'stage_work'} if reuse else set()), 'joint planning', extra | {'submit_rejection'})
    if reuse:
        work=value['stage_work']
        _keys(work,{'mode','cell_hits','cell_misses'},'stage work')
        if work['mode'] not in ('idle','scan','built','reused','fallback','discarded'):raise ValueError('unknown stage work mode')
        for key in ('cell_hits','cell_misses'):
            if type(work[key]) is not int or not 0<=work[key]<=10000:raise ValueError('unbounded stage work count')
        if work['mode']=='reused' and (value['expansions'] or value['cells_checked']):raise ValueError('reused stage performed full search')
        if work['mode']=='discarded' and not value['budget_exhausted']:raise ValueError('discarded stage lacks timeout')
    if staged:
        stage=value['stage_goal']
        cost_fields={'entry_cost_revision','execution_cost_blocks','chosen_entry','score','base_score',
            'occlusion_cache_hits','occlusion_cache_misses'}
        has_cost=execution_cost and 'target' in stage and stage['target'] is not None and stage.get('reason') not in {'observe_stage','budget_observe_stage','retreat'}
        _keys(stage,{'scanning','scan_degrees'},'stage goal',{'reason','target','path','scores','attempts'} | (cost_fields if has_cost else set()))
        if type(stage['scanning']) is not bool:raise ValueError('stage scan flag differs')
        if not 0<=_number(stage['scan_degrees'],'observed scan')<=366:raise ValueError('stage scan exceeds one circle')
        if 'target' in stage:
            _keys(stage,{'reason','target','path','scores','attempts','scanning','scan_degrees'} | (cost_fields if has_cost else set()),'stage plan')
            _text(stage['reason'],'stage reason')
            if stage['target'] is not None:_vector(stage['target'])
            _list(stage['path'],256,'stage path')
            for point in stage['path']:_vector(point)
            _list(stage['scores'],16,'stage scores')
            for score in stage['scores']:
                _keys(score,{'cell','score','path_cost','goal_distance','information','known_occlusion','attempts'} |
                    ({'base_score','execution_cost_blocks'} if has_cost else set()),'stage score')
                _cell(score['cell'],2);_number(score['score'],'stage cost')
                for key in ('path_cost','goal_distance'):_number(score[key],key,0)
                if _int(score['information'],'stage information')>8 or _int(score['attempts'],'stage attempts')>8:
                    raise ValueError('stage score exceeds bounds')
                if type(score['known_occlusion']) is not bool:raise ValueError('stage occlusion differs')
                expected=(score['path_cost']+score['goal_distance']+6*score['known_occlusion']
                    -1.5*min(4,score['information'])+8*score['attempts'])
                if has_cost:
                    if not math.isclose(_number(score['base_score'],'base score'),expected,rel_tol=1e-12,abs_tol=1e-9):
                        raise ValueError('stage base score arithmetic differs')
                    expected+=_number(score['execution_cost_blocks'],'execution cost',0)
                if not math.isclose(score['score'],expected,rel_tol=1e-12,abs_tol=1e-9):
                    raise ValueError('stage score arithmetic differs')
            if _int(stage['attempts'],'stage attempts')>64:raise ValueError('stage attempt capacity differs')
            if has_cost:
                _int(stage['entry_cost_revision'],'entry cost revision');_vector(stage['chosen_entry'])
                for field in ('occlusion_cache_hits','occlusion_cache_misses'):
                    if _int(stage[field],field)>10000:raise ValueError('occlusion cache count exceeds local bounds')
                cost=_number(stage['execution_cost_blocks'],'chosen execution cost',0)
                base=_number(stage['base_score'],'chosen base score')
                if not math.isclose(_number(stage['score'],'chosen score'),base+cost,rel_tol=1e-12,abs_tol=1e-9):
                    raise ValueError('chosen stage score arithmetic differs')
                if stage['chosen_entry'] not in stage['path']:
                    raise ValueError('chosen entry lacks selected path binding')
    for field in ('planned', 'budget_exhausted', 'summary_truncated', 'prelook_enabled', 'force_route_completed'):
        if type(value[field]) is not bool:
            raise ValueError('joint planning flag is invalid')
    if value['joint_revision'] not in ((5,) if execution else (4,) if static_history else (3,) if named else (2,3) if staged else (1,)) or type(value['joint_revision']) is not int:
        raise ValueError('joint planning revision differs')
    for field in ('prelook_enabled', 'force_route_id'):
        if field not in configuration or value[field] != configuration[field]:
            raise ValueError('joint planning configuration differs')
    if value['force_route_id'] is not None:
        _text(value['force_route_id'], 'forced route', 128)
    completed = value['force_route_completed_ns']
    if value['force_route_completed'] != (completed is not None):
        raise ValueError('forced route completion flag differs')
    if completed is not None:
        _int(completed, 'force completion')
        if value['force_route_id'] is None:
            raise ValueError('completion has no forced route')
    if value['acquired_ns'] is not None:
        raise ValueError('planning prediction cannot claim actual acquisition')
    if 'submit_rejection' in value:
        _text(value['submit_rejection'], 'submission rejection', 80)
    config = strategy_config(run['group'])
    if (_int(value['expansions'], 'expansions') > config.max_expansions
            or _int(value['cells_checked'], 'checked cells') > config.max_terrain
            or _int(value['planning_budget_ns'], 'planning budget') != config.planning_budget_ns):
        raise ValueError('joint planning budget differs')
    elapsed = _int(value['elapsed_ns'], 'planning elapsed')
    if elapsed >= config.planning_budget_ns and not value['budget_exhausted']:
        raise ValueError('planning overrun was hidden')
    for key, limit in (('candidates', 16), ('cell_rejections', 64), ('segment_rejections', 64),
                       ('joint_candidates', 16), ('needs', 8)):
        _list(value[key], limit, key)
    for row in value['candidates']:
        if type(row) is not list or len(row) != 4:
            raise ValueError('NB route candidate shape differs')
        _cell(row[0], 2)
        for component in row[1:]:
            _number(component, 'NB route cost', 0)
    for row in value['cell_rejections']:
        if type(row) is not list or len(row) != 2:
            raise ValueError('cell rejection shape differs')
        _cell(row[0], 2); _text(row[1], 'cell rejection', 80)
    for row in value['segment_rejections']:
        _keys(row, {'key', 'reason'}, 'segment rejection')
        _text(row['key'], 'segment key'); _text(row['reason'], 'segment reason', 80)
    needs = {}
    for need in value['needs']:
        _keys(need, {'need_id', 'block', 'reason', 'priority', 'required_by_ns', 'evidence_after_ns'}, 'need')
        _text(need['need_id'], 'need id'); _cell(need['block'], 3)
        _text(need['reason'], 'need reason', 80)
        for key in ('priority', 'required_by_ns', 'evidence_after_ns'):
            _int(need[key], 'need '+key)
        if need['need_id'] in needs:
            raise ValueError('duplicate observation need')
        needs[need['need_id']] = need
    candidates = {}
    candidate_keys = {'candidate_id', 'route_id', 'endpoint', 'proposal', 'gaze_kind', 'need_id',
                      'need_priority', 'required_by_ns', 'expected_acquired_ns', 'intervals',
                      'union_duration_ns', 'progress_debt_seconds', 'recovery_seconds',
                      'uncertainty_penalty', 'gaze_debt', 'score', 'route_score', 'valid_until_ns',
                      'missing_measurements'}
    for row in value['joint_candidates']:
        _keys(row, candidate_keys, 'joint candidate')
        for key in ('candidate_id', 'route_id'):
            _text(row[key], key)
        if row['candidate_id'] in candidates:
            raise ValueError('duplicate joint candidate')
        candidates[row['candidate_id']] = row
        _vector(row['endpoint']); _proposal(row['proposal'])
        _int(row['valid_until_ns'], 'candidate expiry'); _int(row['need_priority'], 'need priority')
        stamp = row['proposal']['based_on']
        if not stamp['received_at_ns'] < row['valid_until_ns'] <= stamp['request_start_ns']+config.freshness_ns:
            raise ValueError('candidate expiry is outside evidence freshness')
        if row['gaze_kind'] not in {'hold', 'front', 'need', 'task', 'reposition'}:
            raise ValueError('unknown joint gaze kind')
        if row['need_id'] is None:
            if row['required_by_ns'] is not None or row['expected_acquired_ns'] is not None or row['gaze_kind'] in {'need', 'task', 'reposition'}:
                raise ValueError('candidate predicts evidence without a need')
        else:
            _text(row['need_id'], 'candidate need')
            need = needs.get(row['need_id'])
            if (need is None or row['required_by_ns'] != need['required_by_ns']
                    or row['need_priority'] != need['priority'] or not value['prelook_enabled']
                    or row['gaze_kind'] not in {'need', 'task', 'reposition'}):
                raise ValueError('candidate need/configuration binding differs')
            if _int(row['expected_acquired_ns'], 'predicted acquisition') < row['proposal']['based_on']['received_at_ns']:
                raise ValueError('predicted acquisition precedes input evidence')
        _list(row['intervals'], 16, 'candidate time intervals')
        intervals = []
        for interval in row['intervals']:
            if type(interval) is not list or len(interval) != 2:
                raise ValueError('candidate interval shape differs')
            start, end = (_int(x, 'interval endpoint') for x in interval)
            if start > end:
                raise ValueError('candidate interval reversed')
            intervals.append((start, end))
        # Independent recomputation; do not trust a saved score or call the
        # production scorer being evaluated.
        union, right = 0, 0
        for start, end in sorted(intervals):
            union += max(0, end-max(start, right)); right = max(right, end)
        if _int(row['union_duration_ns'], 'saved union') != union:
            raise ValueError('candidate interval union differs')
        for key in ('progress_debt_seconds', 'recovery_seconds', 'uncertainty_penalty',
                    'gaze_debt', 'score', 'route_score'):
            _number(row[key], 'candidate '+key, 0)
        expected = union/1e9 + 4*row['progress_debt_seconds']+row['recovery_seconds']+2*row['uncertainty_penalty']+4*row['gaze_debt']
        if not math.isclose(row['score'], expected, rel_tol=1e-12, abs_tol=1e-9):
            raise ValueError('candidate joint score differs')
        _list(row['missing_measurements'], 3, 'missing measurements')
        if any(type(x) is not str or x not in {'normal_speed_blocks_per_second', 'look_rate_degrees_per_second', 'sample_delivery_ns'} for x in row['missing_measurements']) or len(set(row['missing_measurements'])) != len(row['missing_measurements']):
            raise ValueError('missing measurement declaration differs')
    routes = {c['route_id'] for c in candidates.values()}
    if candidates:
        first = next(iter(candidates.values()))['proposal']
        frame_fields = ('scope_id', 'memory_generation', 'based_on', 'origin', 'velocity', 'yaw', 'pitch')
        if any(any(row['proposal'][key] != first[key] for key in frame_fields) for row in candidates.values()):
            raise ValueError('joint candidates mix input observation frames')
    if len(routes) > 4 or any(sum(c['route_id'] == route for c in candidates.values()) > 4 for route in routes):
        raise ValueError('joint route/gaze count exceeds bounds')
    if value['planned']:
        if not extra <= set(value) or _int(value['route_count'], 'route count') != len(routes):
            raise ValueError('planned diagnostic route summary differs')
    elif (any(value[k] for k in ('expansions', 'cells_checked', 'candidates', 'cell_rejections',
                                  'segment_rejections', 'elapsed_ns', 'budget_exhausted',
                                  'summary_truncated', 'joint_candidates', 'needs'))
          or value['selected_candidate_id'] is not None):
        raise ValueError('unplanned diagnostic republishes a plan')
    if not value['planned'] and _int(value.get('route_count', 0), 'unplanned route count') != 0:
        raise ValueError('unplanned diagnostic retains routes')
    selected = value['selected_candidate_id']
    if selected is None:
        if any(value.get(k) is not None for k in ('route_id', 'proposal', 'original_route', 'need_id', 'required_by_ns', 'expected_acquired_ns')):
            raise ValueError('unselected diagnostic retains selected fields')
    else:
        _text(selected, 'selected candidate')
        row = candidates.get(selected)
        if row is None or value['budget_exhausted']:
            raise ValueError('selected candidate missing or planning over budget')
        for key in ('route_id', 'proposal', 'need_id', 'required_by_ns', 'expected_acquired_ns'):
            if value[key] != row[key]:
                raise ValueError('selected candidate binding differs')
        original = value['original_route']
        if type(original) is not list or len(original) != 2 or original[1] != row['endpoint']:
            raise ValueError('selected original route differs')
        _vector(original[0]); _vector(original[1])
        if value['force_route_id'] is not None and not value['force_route_completed'] and row['route_id'] != value['force_route_id']:
            raise ValueError('selected candidate escaped forced prefix')
        _possible_selection(row, candidates, value, run['group'])
    _feedback(value['feedback'])
