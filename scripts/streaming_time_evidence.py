"""Bounded evidence readers and a shared, incremental raw-time attribution engine."""
from __future__ import annotations

from pathlib import Path
import hashlib
import importlib
from functools import lru_cache
import re
from typing import Callable, Iterable, Iterator

from mc2p.runtime.segmented_trace import MAX_RECORD_BYTES, _decode, _lines, _positive, _safe_path


_IMAGE_KEY_TOKENS = (
    "pov", "rgb", "image", "frame", "pixel", "texture", "screenshot",
)


@lru_cache(maxsize=512)
def _image_key(key: str) -> bool:
    normalized=re.sub(r'[^a-z0-9]+','_',key.casefold())
    return any(token in normalized for token in _IMAGE_KEY_TOKENS)


def require_image_free_json(value):
    """Same forbidden-key policy, once per parsed record, without repeated Mapping checks/copies."""
    if type(value) is dict:
        if 'byte_length' in value and 'sha256' in value: raise ValueError('binary trace projection')
        for key,child in value.items():
            if _image_key(key): raise ValueError('image-bearing key: '+key)
            require_image_free_json(child)
    elif type(value) is list:
        for child in value: require_image_free_json(child)


def iter_bounded_jsonl(path: Path, max_record_bytes: int = MAX_RECORD_BYTES) -> Iterator[dict]:
    maximum = min(_positive(max_record_bytes, 'max_record_bytes'), MAX_RECORD_BYTES)
    with _safe_path(path).open('rb') as stream:
        for line in _lines(stream, maximum):
            yield _decode(line)


class _Errors:
    def __init__(self):
        self.rows: list[str] = []
        self.count = 0

    def add(self, error):
        self.count += 1
        if len(self.rows) < 64:
            self.rows.append(str(error)[:2048])


def evaluate_time_stream(events: Iterable[dict], observations: Iterable[dict],
                         interval_sink: Callable[[dict], None]) -> dict:
    """Consume both streams fully; any broken association keeps the final verdict failed."""
    errors = _Errors()
    expected_sequence = 1
    session = active_world = last_event = previous_observation = cursor = None
    seen_worlds: set[str] = set()
    tick_calls = packet_delta = packet_count = observation_count = 0
    event_count = total_packets = interval_count = regressions = 0
    observation_iter = iter(observations)
    try:
        for event in events:
            event_count += 1
            try:
                if event['schema_version'] != 'mc2p.client-time-event.v1':
                    raise ValueError('unknown time event schema')
                kind = event['event']
                integer_fields = ['event_sequence', 'client_ticks', 'world_ticks']
                integer_fields += ['generation_id', 'world_time'] if kind == 'observation' else ['before', 'after']
                if kind == 'time_packet': integer_fields.append('packet_time')
                if any(type(event[key]) is not int or event[key] < 0 for key in integer_fields):
                    raise ValueError('time event counters/timestamps must be nonnegative integers')
                if any(type(event[key]) is not str or not event[key] for key in ('session_id', 'world_id')):
                    raise ValueError('time event identity missing')
                if event['world_id'] != active_world:
                    if event['world_id'] in seen_worlds:
                        raise ValueError('retired diagnostic world identity was reused')
                    if len(seen_worlds) >= 4096:
                        raise ValueError('diagnostic world identity capacity exceeded')
                    active_world = event['world_id']
                    seen_worlds.add(active_world)
                if session is None: session = event['session_id']
                if event['session_id'] != session or event['event_sequence'] != expected_sequence:
                    raise ValueError('time event session/sequence is discontinuous')
                expected_sequence += 1
                if last_event is not None:
                    if event['client_ticks'] < last_event['client_ticks']:
                        raise ValueError('client tick counter regressed')
                    expected_world = last_event['world_ticks'] + (kind == 'world_tick')
                    if event['world_ticks'] != expected_world:
                        raise ValueError('world tick counter does not match actual events')
                last_event = event
                if kind == 'observation':
                    if previous_observation is None and event['generation_id'] != 0:
                        raise ValueError('first observation must be a reset generation')
                    try:
                        observation = next(observation_iter)
                    except StopIteration:
                        raise ValueError('time event has no corresponding formal observation') from None
                    observation_count += 1
                    if (event['generation_id'] != observation['sequence_id']
                            or event['world_time'] != observation['world_time_ticks']['value']):
                        raise ValueError('diagnostic observation does not match formal observation')
                    # Optional independent diagnostic attachment is never part of the formal Observation.
                    if '_diagnostic_client_tick' in observation and event['client_ticks'] != observation['_diagnostic_client_tick']:
                        raise ValueError('time events disagree with independent client tick counters')
                    if previous_observation is not None and event['generation_id'] != 0:
                        if (event['world_id'] != previous_observation['world_id']
                                or event['generation_id'] != previous_observation['generation_id'] + 1):
                            raise ValueError('observation world/generation changed without reset')
                        if cursor != event['world_time']:
                            raise ValueError('unaccounted world-time change before observation')
                        interval = {'observation_sequence_id': event['generation_id'],
                            'before': previous_observation['world_time'], 'after': event['world_time'],
                            'client_tick_calls': event['client_ticks'] - previous_observation['client_ticks'],
                            'world_tick_calls': tick_calls, 'packet_correction_sum': packet_delta,
                            'packet_count': packet_count, 'observed_delta': event['world_time'] - previous_observation['world_time']}
                        interval_sink(interval)
                        interval_count += 1
                        regressions += interval['observed_delta'] < 0
                    previous_observation = event
                    cursor = event['world_time']
                    tick_calls = packet_delta = packet_count = 0
                elif kind in ('world_tick', 'time_packet'):
                    if kind == 'world_tick' and event['after'] - event['before'] != 1:
                        raise ValueError('native client tickTime did not advance by one')
                    if kind == 'time_packet':
                        total_packets += 1
                        if event['after'] != event['packet_time']:
                            raise ValueError('packet time was not applied as recorded')
                    if previous_observation is not None and event['world_id'] == previous_observation['world_id']:
                        if event['before'] != cursor:
                            raise ValueError('unaccounted world-time change before event')
                        cursor = event['after']
                        if kind == 'world_tick': tick_calls += 1
                        else:
                            packet_count += 1
                            packet_delta += event['after'] - event['before']
                else:
                    raise ValueError('unknown time event')
            except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
                errors.add(error)
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        errors.add(error)
    # Even after errors, complete structural validation of any remaining formal stream.
    extra = 0
    try:
        for _ in observation_iter:
            extra += 1
            observation_count += 1
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        errors.add(error)
    if extra or observation_count < 2 or not interval_count:
        errors.add('time events do not cover all formal observations')
    return {'schema_version': 'mc2p.client-time-stream-evidence.v1', 'status': 'failed' if errors.count else 'passed',
            'meaning': 'event attribution only; does not establish clock equivalence',
            'event_count': event_count, 'observation_count': observation_count, 'interval_count': interval_count,
            'packet_update_count': total_packets, 'regression_count': regressions,
            'errors': errors.rows, 'error_count': errors.count}


def _parse_action(raw: dict):
    from dataclasses import fields
    from mc2p.contracts import action_v1 as actions

    def construct(cls, value):
        if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
            raise ValueError('invalid action fields')
        result = cls(**{f.name: value[f.name] for f in fields(cls) if f.init})
        for f in fields(cls):
            if not f.init and getattr(result, f.name) != value[f.name]:
                raise ValueError('invalid action schema/kind')
        return result

    value = dict(raw)
    value['movement'] = construct(actions.MovementV1, value['movement'])
    value['look'] = construct(actions.LookV1, value['look'])
    if value['operation'] is not None:
        types = {cls.__dataclass_fields__['kind'].default: cls for cls in actions._OPERATION_TYPES}
        value['operation'] = construct(types[value['operation']['kind']], value['operation'])
    return construct(actions.ActionSnapshotV1, value)


def _validated_observations(directory: Path, errors: _Errors) -> Iterator[dict]:
    from mc2p.contracts.action_receipt import ClientBehaviorReceiptV2
    from mc2p.runtime.segmented_trace import iter_segmented_jsonl
    from scripts.formal_observation_v3_trace import restore_observation_v3_trace

    diagnostics = iter(iter_segmented_jsonl(directory / 'diagnostics'))
    previous = None
    schema_version = None
    previous_input = previous_tick = None
    closed = False
    try:
        for record in iter_segmented_jsonl(directory / 'trace'):
            try:
                if (set(record) != {'schema_version', 'record_type', 'payload'}
                        or record['schema_version'] != 'mc2p.trace-record.v0'):
                    raise ValueError('invalid or image-bearing formal trace record')
                require_image_free_json(record)
                kind, payload = record['record_type'], record['payload']
                if kind not in {'reset', 'step', 'close_release'}:
                    continue
                observation = payload['result']['observation'] if kind == 'reset' else payload['backend_result']['observation']
                # Shared time analysis reads declared historical V2 or current V3,
                # never changes their knowledge model and never permits mixing.
                version=observation.get('schema_version')
                if version=='mc2p.observation.v3':
                    restored=restore_observation_v3_trace(observation)
                    if restored.privileged_fields_present:
                        raise ValueError('privileged V3 runtime timing observation')
                elif version=='mc2p.observation.v2':
                    try:
                        parser = importlib.import_module(
                            'scripts.smoke_test_player_runtime'
                        )._parse_observation_v2
                    except (ModuleNotFoundError, AttributeError) as error:
                        raise ValueError(
                            'historical V2 timing parser is not included in the current Fabric package'
                        ) from error
                    parser(observation)
                else:
                    raise ValueError('unsupported runtime timing observation schema')
                if schema_version is not None and version!=schema_version:
                    raise ValueError('knowledge schema changed within timing stream')
                schema_version=version
                if kind == 'reset':
                    if observation['sequence_id'] != 0 or (previous and observation['episode_id'] == previous['episode_id']):
                        raise ValueError('invalid reset episode/sequence')
                    closed = False
                    previous_input = None
                else:
                    if previous is None or closed:
                        raise ValueError('action sample outside an active reset episode')
                    action = _parse_action(payload['decision']['action'] if kind == 'step' else payload['action'])
                    receipt = ClientBehaviorReceiptV2.from_mapping(payload['backend_result']['receipt'])
                    if (observation['episode_id'] != previous['episode_id']
                            or observation['sequence_id'] != previous['sequence_id'] + 1
                            or action.episode_id != observation['episode_id']
                            or action.observation_sequence_id != previous['sequence_id']
                            or action.request_sequence_id != previous['sequence_id']
                            or observation['request_sequence_id'] != action.request_sequence_id
                            or receipt.episode_id != action.episode_id
                            or receipt.request_sequence_id != action.request_sequence_id
                            or receipt.generation_id != observation['sequence_id']
                            or receipt.world_tick != observation['world_time_ticks']['value']
                            or receipt.on_client_thread is not True or receipt.status == 'idle'
                            or receipt.action_keyboard_callbacks != 0 or receipt.action_mouse_callbacks != 0
                            or receipt.handled_screen_render_completions != 0):
                        raise ValueError('action/receipt/observation association or direct execution failed')
                    if (observation['controller_clock_id'] != previous['controller_clock_id']
                            or observation['source_backend'] != previous['source_backend']
                            or observation['client_sample']['clock_id'] != previous['client_sample']['clock_id']
                            or observation['client_sample']['started_at_monotonic_ns'] < previous['client_sample']['completed_at_monotonic_ns']):
                        raise ValueError('formal sample clock changed/regressed')
                    closed = kind == 'close_release'
                try:
                    row = next(diagnostics)
                except StopIteration:
                    raise ValueError('missing diagnostic sample') from None
                diag = row['diagnostics']
                if (row['episode_id'] != observation['episode_id'] or row['observation_sequence_id'] != observation['sequence_id']
                        or type(diag['client_tick']) is not int or diag['client_tick'] < 0
                        or diag['window_recorded'] is not True or diag['window_visible'] is not False
                        or diag['window_visible_at_creation'] is not False
                        or any(type(diag[k]) is not int or diag[k] != 0 for k in
                            ('framebuffer_capture_attempts', 'image_encode_attempts', 'world_render_completions', 'gui_render_completions'))):
                    raise ValueError('diagnostic association or zero-image/hidden invariant failed')
                if kind != 'reset':
                    actual_ticks = diag['client_tick'] - previous_tick
                    if actual_ticks < 1:
                        raise ValueError('reference action observation has no actual client tick')
                    if previous_input is None:
                        if observation['sequence_id'] != 1 or receipt.input_samples != 1:
                            raise ValueError('first input counter did not start cleanly')
                    elif receipt.input_samples - previous_input != actual_ticks:
                        raise ValueError('input sample delta disagrees with actual client ticks')
                    previous_input = receipt.input_samples
                previous_tick = diag['client_tick']
                previous = observation
                yield {'sequence_id': observation['sequence_id'], 'world_time_ticks': observation['world_time_ticks'],
                       '_diagnostic_client_tick': diag['client_tick']}
            except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
                errors.add(error)
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        errors.add(error)
    try:
        for _ in diagnostics:
            errors.add('unmatched diagnostic sample')
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        errors.add(error)


def export_segmented_runtime_time_evidence(directory: Path, output: Path) -> dict:
    """Create a new report; never overwrite a prior verdict or retain all intervals."""
    from mc2p.runtime.segmented_trace import SegmentedJsonlWriter, iter_segmented_jsonl
    from scripts.control_probe_core import write_json_atomic

    directory, output = _safe_path(directory), _safe_path(output)
    output.mkdir(parents=True, exist_ok=False)
    errors = _Errors()
    hashes = {}
    writer = SegmentedJsonlWriter(output / 'intervals')
    try:
        for name in ('trace', 'diagnostics', 'time-events'):
            digest = hashlib.sha256()
            with _safe_path(directory/name/'manifest.jsonl').open('rb') as stream:
                while chunk := stream.read(65536): digest.update(chunk)
            hashes[name] = digest.hexdigest()
        result = evaluate_time_stream(iter_segmented_jsonl(directory/'time-events'),
            _validated_observations(directory, errors), writer.write)
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        errors.add(error)
        result = {'schema_version': 'mc2p.client-time-stream-evidence.v1', 'status': 'failed',
                  'errors': [], 'error_count': 0}
    finally:
        try:
            writer.close()
        except (ValueError, OSError) as error:
            errors.add(error)
    result['errors'] = (result['errors'] + errors.rows)[:64]
    result['error_count'] += errors.count
    if result['error_count']: result['status'] = 'failed'
    result['input_manifest_hashes'] = hashes
    result['interval_directory'] = str(output/'intervals')
    write_json_atomic(output/'time-report.json', result)
    return result
