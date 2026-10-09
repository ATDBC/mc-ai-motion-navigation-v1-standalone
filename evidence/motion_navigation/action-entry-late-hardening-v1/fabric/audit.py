"""Read the preserved four direction probes; never change their pass flags."""
from pathlib import Path
import gzip
import json


def rows(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def audit(root):
    results = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / 'f2-trials.jsonl').is_file():
            continue
        trial = rows(directory / 'f2-trials.jsonl')[0]
        frames = rows(directory / 'f2-frames.jsonl.gz')
        trace = rows(directory / 'trace.jsonl.gz')
        late = trial['late_input']
        injected = ([] if late is None else [frame for frame in frames
                    if frame['requested_first_tick'] == late['requested_first_tick']])
        injected_sequences = {frame['request_sequence'] for frame in injected}
        outside = [frame for frame in frames
                   if frame['actual_application_ticks']
                   and frame['latest_allowed_first_tick'] is not None
                   and min(frame['actual_application_ticks']) > frame['latest_allowed_first_tick']]
        extra_outside = [frame for frame in outside
                         if frame['request_sequence'] not in injected_sequences]
        jumps = [frame for frame in frames
                 if frame['movement'] is not None and frame['movement']['jump']]
        airborne = [frame['tick'] for frame in frames if not frame['on_ground']]
        registrations = [row['payload']['source'] for row in trace
                         if row['record_type'] == 'ordered_source_registered']
        released = [row['payload']['source'] for row in trace
                    if row['record_type'] == 'ordered_source_unregistered']
        strict_sequences = set()
        for row in trace:
            if row['record_type'] != 'dispatch':
                continue
            decision = row['payload']['decision']
            if decision.get('movement_tick_window') is not None:
                strict_sequences.add(decision['action']['request_sequence_id'])
        strict_outside = [frame for frame in outside
                          if frame['request_sequence'] in strict_sequences]
        jump_rows = [{key: frame.get(key) for key in (
            'tick', 'pre_input_speed_blocks_per_second',
            'maximum_entry_speed_blocks_per_second', 'entry_yaw_error_degrees',
            'requested_first_tick', 'actual_application_ticks', 'input_status',
        )} for frame in jumps]
        results.append({
            'id': trial['id'], 'raw_passed': trial['passed'],
            'raw_violations': trial['violations'], 'task_success': trial['task_success'],
            'damage_points': trial['damage_points'], 'late_input': late,
            'late_entry_injection_confirmed': late is not None,
            'preplanned_ordinary_window_outside_frames': len(outside) - len(extra_outside),
            'additional_window_outside_frames': len(extra_outside),
            'strict_window_outside_frames': len(strict_outside),
            'jump_frames': jump_rows, 'airborne_ticks': airborne,
            'confirmed_landing_grounded': trial['final_body']['is_on_ground'],
            'registered_sources': registrations, 'released_sources': released,
            'all_sources_released': sorted(registrations, key=str) == sorted(released, key=str),
            'cleanup': json.loads((directory / 'cleanup.json').read_text('utf-8')),
        })
    return {
        'schema_version': 'mc2p.action-entry-late-fabric-audit.v1',
        'raw_trial_count': len(results),
        'task_completions': sum(row['task_success'] for row in results),
        'new_fabric_signature_gate_closed': False,
        'boundary': 'Ordinary Walk deliberately applies outside its current exact window; raw failed results remain failed. D092 strict-first-late evidence is unchanged.',
        'trials': results,
    }


if __name__ == '__main__':
    print(json.dumps(audit(Path(__file__).resolve().parent), indent=2))
