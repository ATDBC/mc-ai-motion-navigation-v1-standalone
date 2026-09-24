"""Opt-in diagnostic admission, separate from actor knowledge and speed measurements."""
import json
from pathlib import Path

from scripts.navigation_motion_evidence import restore_snapshot
from scripts.formal_observation_v3_evidence import validate_formal_observations_v3
from scripts.visibility_fixture_world import _no_links

FIELDS={'schema_version','generation_id','field_profile','tick_before','tick_after',
    'legacy_first_hit_count','v3_first_hit_count','legacy_only_positions','v3_only_positions',
    'v3_unique_block_count','body_contact_count','current_target_count','multi_source_block_count',
    'legacy_scan_elapsed_ns'}
COUNTERS=FIELDS-{'schema_version','field_profile','legacy_only_positions','v3_only_positions'}


def evaluate_block_parity(rows: list[dict],observations: list[dict],*,require_profile_cycle: bool=False) -> dict:
    result=dict(schema_version='mc2p.block-parity-evidence.v1',passed=False,errors=[],sample_count=0,
                performance_claim=False)
    try:
        if type(rows) is not list or not 1<=len(rows)<=2048 or len(rows)!=len(observations):
            raise ValueError('missing, excess or unassociated parity samples')
        violations=validate_formal_observations_v3(observations)
        if violations: raise ValueError(violations[0])
        profiles=[];generations=set();scope=None;contacts=0
        for row,raw in zip(rows,observations):
            if type(row) is not dict or set(row)!=FIELDS or row['schema_version']!='mc2p.block-parity-diagnostic.v1':
                raise ValueError('wrong diagnostic schema/fields')
            if any(type(row[k]) is not int or row[k]<0 for k in COUNTERS):
                raise ValueError('invalid diagnostic counter')
            if row['legacy_only_positions']!=[] or row['v3_only_positions']!=[]:
                raise ValueError('old/new first-hit sets differ')
            obs=restore_snapshot(raw)
            identity=(obs.episode_id,obs.client_sample.clock_id,obs.controller_clock_id)
            if scope is None: scope=identity
            if scope!=identity or obs.sequence_id in generations:
                raise ValueError('foreign or duplicate parity generation')
            generations.add(obs.sequence_id)
            if (row['generation_id']!=obs.sequence_id or row['field_profile']!=obs.field_profile
                    or row['tick_before']!=row['tick_after'] or row['tick_after']!=obs.world_time_ticks.value):
                raise ValueError('parity is not the same formal sample/profile/tick')
            p=obs.perception.value
            if p is None: raise ValueError('missing actual block evidence')
            blocks=p.blocks
            actual=dict(v3_unique_block_count=len(blocks),
                v3_first_hit_count=sum('first_hit_ray' in b.sources for b in blocks),
                body_contact_count=sum('body_contact' in b.sources for b in blocks),
                current_target_count=sum('current_target' in b.sources for b in blocks),
                multi_source_block_count=sum(len(b.sources)>1 for b in blocks))
            if any(row[k]!=v for k,v in actual.items()) or row['legacy_first_hit_count']!=actual['v3_first_hit_count']:
                raise ValueError('parity counts do not match actual authorized blocks')
            if not profiles or profiles[-1]!=obs.field_profile: profiles.append(obs.field_profile)
            contacts+=actual['body_contact_count']
        if contacts==0: raise ValueError('no actual body-contact sample')
        if require_profile_cycle and not any(profiles[i:i+3]==['navigation_v1','interaction_v1','navigation_v1']
                for i in range(len(profiles)-2)):
            raise ValueError('missing navigation/interaction/navigation cycle')
        result.update(passed=True,sample_count=len(rows),profiles=profiles)
    except (TypeError,ValueError,KeyError,AttributeError) as error:
        result['errors'].append(str(error))
    return result


def read_parity_rows(path: Path) -> list[dict]:
    _no_links(path)
    rows=[]
    def pairs(items):
        value={}
        for key,item in items:
            if key in value: raise ValueError('duplicate parity field')
            value[key]=item
        return value
    with path.open('rb') as stream:
        while True:
            line=stream.readline(262145)
            if not line: break
            if len(rows)>=2048 or len(line)>262144 or not line.endswith(b'\n'):
                raise ValueError('parity sidecar exceeds bound or has unsealed tail')
            rows.append(json.loads(line.decode('utf-8'),object_pairs_hook=pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite parity field'))))
    return rows


def parity_environment(base: dict[str,str],enabled: bool) -> dict[str,str]:
    if type(enabled) is not bool: raise ValueError('parity selection must be boolean')
    result={key:value for key,value in base.items() if key.upper()!='MC2P_BLOCK_PARITY_DIAGNOSTICS'}
    if enabled: result['MC2P_BLOCK_PARITY_DIAGNOSTICS']='1'
    return result


def parity_file_check(path: Path,observations: list[dict],*,enabled: bool,require_profile_cycle: bool=False) -> dict:
    name='same_tick_block_parity' if enabled else 'no_block_parity_sidecar_when_disabled'
    try:
        _no_links(path.parent)
        if not enabled:
            try: path.lstat()
            except FileNotFoundError: return dict(name=name,passed=True)
            _no_links(path)
            return dict(name=name,passed=False)
        report=evaluate_block_parity(read_parity_rows(path),observations,require_profile_cycle=require_profile_cycle)
        return dict(name=name,passed=report['passed'],report=report)
    except (OSError,ValueError,TypeError) as error:
        return dict(name=name,passed=False,error=str(error))


def formal_samples_from_records(records: list[dict]) -> list[dict]:
    samples=[]
    for record in records:
        if record['record_type']=='reset': samples.append(record['payload']['result']['observation'])
        elif record['record_type'] in {'step','close_release'}:
            samples.append(record['payload']['backend_result']['observation'])
    return samples


def frozen_probe_sources() -> dict[str,str]:
    from scripts.follow_playground_session import CORE_SOURCES,ROOT,_hash
    return {name:_hash(ROOT/name) for name in CORE_SOURCES}
