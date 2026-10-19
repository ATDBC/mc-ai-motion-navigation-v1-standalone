"""Recheck the archived TP-0 input applications; does not run Minecraft."""
from pathlib import Path
import gzip
import hashlib
import json


ROOT = Path(__file__).resolve().parent


def read_bytes(path):
    raw = path.read_bytes()
    return gzip.decompress(raw) if path.suffix == '.gz' else raw


def rows(path):
    return [json.loads(line) for line in read_bytes(path).splitlines() if line.strip()]


def audit():
    manifest = json.loads((ROOT / 'source-byte-manifest.json').read_text('utf-8'))
    for item in manifest['files']:
        path = ROOT / item['archive_relative_path']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['archive_sha256'], path
        assert hashlib.sha256(read_bytes(path)).hexdigest() == item['raw_sha256'], path
    trials = rows(ROOT / 'raw/client-0/f2-trials.jsonl.gz')
    frames = rows(ROOT / 'raw/client-0/f2-frames.jsonl.gz')
    trace = rows(ROOT / 'raw/client-0/trace.jsonl.gz')
    applications = set()
    registered, released = [], []
    for item in trace:
        if item['record_type'] == 'step':
            receipt = item['payload']['backend_result']['receipt']
            for application in receipt.get('input_applications', []):
                applications.add((application['request_sequence_id'], application['movement_tick_id']))
        elif item['record_type'] == 'ordered_source_registered':
            registered.append(item['payload']['source']['source_id'])
        elif item['record_type'] == 'ordered_source_unregistered':
            released.append(item['payload']['source']['source_id'])
    assert len(trials) == 4 and {row['direction'] for row in trials} == {0, 1, 2, 3}
    results = []
    for trial in trials:
        selected = [frame for frame in frames if frame['trial'] == trial['id']]
        late = trial['late_input']
        assert trial['passed'] and trial['task_success'] and not trial['violations']
        assert late is not None and late['entry_gate'] is not None
        assert late['actual_offset'] == 2 and late['status'] == 'applied_outside_window'
        assert late['explicit_input_timing'] is False
        assert len(late['actual_ticks']) == 1
        assert late['actual_ticks'][0] - late['before_tick'] == 2
        assert late['actual_ticks'][0] - late['requested_first_tick'] == 1
        assert (late['request_sequence'], late['actual_ticks'][0]) in applications
        matching = [frame for frame in selected if frame['request_sequence'] == late['request_sequence']]
        assert len(matching) == 1 and matching[0]['input_status'] == 'applied_outside_window'
        misses = [frame for frame in selected if frame['explicit_input_timing']
                  and frame['actual_application_ticks']
                  and min(frame['actual_application_ticks']) > frame['latest_allowed_first_tick']]
        ordinary_delays = [frame for frame in selected if not frame['explicit_input_timing']
                           and frame['actual_application_ticks']
                           and min(frame['actual_application_ticks']) > frame['requested_first_tick']]
        assert not misses and len(ordinary_delays) == 1
        assert trial['unexpected_explicit_deadline_miss_count'] == 0
        assert trial['unwindowed_input_delay_count'] == 1
        assert trial['damage_points'] == 0 and all(frame['health_points'] == 20 for frame in selected)
        assert trial['final_body']['is_on_ground'] and trial['formal_goal_status'] == 'satisfied'
        jumps = [frame for frame in selected if frame['movement'] is not None and frame['movement']['jump']]
        assert jumps and all(frame['entry_yaw_error_degrees'] <= 2 for frame in jumps)
        assert all(frame['pre_input_speed_blocks_per_second'] <=
                   frame['maximum_entry_speed_blocks_per_second'] + 1e-9 for frame in jumps)
        results.append({
            'id': trial['id'], 'direction': trial['direction'], 'passed': trial['passed'],
            'task_success': trial['task_success'], 'raw_injection_applied': trial['injection_applied'],
            'late_input': late, 'raw_receipt_application_confirmed': True,
            'unexpected_explicit_deadline_miss_count': len(misses),
            'unwindowed_input_delay_count': len(ordinary_delays),
            'damage_points': trial['damage_points'], 'final_grounded': trial['final_body']['is_on_ground'],
        })
    assert sorted(registered) == sorted(released) and len(registered) == 4
    cleanup = json.loads((ROOT / 'raw/client-0/cleanup.json').read_text('utf-8'))
    assert cleanup['passed'] and not cleanup['errors'] and not cleanup['cleanup']['surviving']
    return {
        'schema_version': 'mc2p.tp0-entry-late-fabric-audit.v1',
        'frozen_control_commit': manifest['frozen_control_commit'],
        'production_commit': manifest['production_commit'],
        'cases': len(results), 'passed': True, 'trials': results,
        'registered_sources': registered, 'released_sources': released,
        'sources_all_released': True, 'client_cleanup_passed': True,
        'timing_boundary': 'Ordinary Walk delay is reported separately. This does not promise that every ordinary Walk tolerates a late tick.',
        'injection_boundary': 'late_input, entry_gate and actual receipt ticks prove this injection. injection_applied is only the separate external-force/world-change marker.',
        'historical_d093_results_rewritten': False,
    }


if __name__ == '__main__':
    print(json.dumps(audit(), ensure_ascii=False, indent=2))
